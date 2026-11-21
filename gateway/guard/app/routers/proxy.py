from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.convertors import PathConvertor, register_url_convertor

from app import approvals, audit
from app.auth import Principal, get_principal, get_settings
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_session
from app.errors import (
    ForbiddenError,
    InternalError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamUnavailableError,
    ValidationAppError,
    get_request_id,
)
from app.json_utils import (
    RawBody,
    extract_audit_json_dict,
    extract_valid_order_id,
    parse_strict_json_dict,
)
from app.log import log_internal_error
from app.manifest import ToolManifest, extract_request_path
from app.policy import get_write_policy
from app.routers import read_raw_body
from app.upstream import UpstreamClient, validate_path_params

logger = logging.getLogger("guard.proxy")


class _AnyPathConvertor(PathConvertor):
    regex = r"[\s\S]*"


register_url_convertor("anypath", _AnyPathConvertor())

router = APIRouter()

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _record_and_commit_audit(
    session: Session,
    *,
    settings: Settings,
    now_dt: Any,
    request_id: str,
    principal: Principal,
    tool: str,
    http_method: str,
    http_path: str,
    arguments: dict[str, Any],
    order_id: str | None,
    resource_region: str | None,
    status_code: int,
    error_code: str | None,
    duration_ms: int,
    result: dict[str, Any] | None,
    retention_state: audit.AuditRetentionState | None,
) -> None:
    try:
        with approvals.WRITE_LOCK:
            row = audit.record_event(
                session,
                settings=settings,
                now=now_dt,
                request_id=request_id,
                event_type="tool_call",
                channel="agent",
                actor_sub=principal.sub,
                actor_username=principal.username,
                actor_name=principal.name,
                actor_role=principal.role,
                actor_regions=principal.regions_csv,
                token_jti=principal.jti,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments=arguments,
                order_id=order_id,
                resource_region=resource_region,
                outcome=audit.status_to_outcome(status_code),
                status_code=status_code,
                error_code=error_code,
                duration_ms=duration_ms,
                result=result,
                retention_state=retention_state,
            )
            session.commit()
    except Exception as exc:
        session.rollback()
        log_internal_error(
            logger,
            request_id=request_id,
            operation="proxy_audit_write",
            error_type=type(exc).__name__,
        )
        raise InternalError("审计记录写入失败，请求已中止。") from exc
    audit.emit_audit_log(row, settings.timezone)


@router.api_route("/{full_path:anypath}", methods=PROXY_METHODS)
def proxy_upstream(
    full_path: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    raw_body: RawBody = Depends(read_raw_body),
) -> Response:
    _ = full_path
    manifest: ToolManifest = request.app.state.manifest
    upstream: UpstreamClient = request.app.state.upstream
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    request_id = get_request_id(request)
    path = extract_request_path(request)
    method = request.method.upper()
    authorization: str = request.state.authorization

    matched = manifest.match_upstream(method, path)
    if matched is None:
        raise NotFoundError("route_not_found", "请求的接口不存在或未对外开放。")

    # Read operation (G7: HTTP GET) -> transparent proxy + audit
    if matched.entry.method == "GET":
        if raw_body.too_large:
            _record_and_commit_audit(
                session,
                settings=settings,
                now_dt=clock(),
                request_id=request_id,
                principal=principal,
                tool=matched.entry.name,
                http_method=method,
                http_path=path,
                arguments={},
                order_id=extract_valid_order_id(matched.path_params),
                resource_region=None,
                status_code=413,
                error_code="payload_too_large",
                duration_ms=0,
                result=None,
                retention_state=retention_state,
            )
            raise PayloadTooLargeError()

        arguments: dict[str, Any] = dict(request.query_params.items())
        arguments.update(matched.path_params)
        order_id = extract_valid_order_id(arguments)

        try:
            parse_strict_json_dict(raw_body.data, allow_empty=True)
        except ValidationAppError as exc:
            body_audit_args = extract_audit_json_dict(raw_body.data, exc)
            merged_args: dict[str, Any] = {**body_audit_args, **arguments}
            merged_order_id = order_id or extract_valid_order_id(body_audit_args)
            _record_and_commit_audit(
                session,
                settings=settings,
                now_dt=clock(),
                request_id=request_id,
                principal=principal,
                tool=matched.entry.name,
                http_method=method,
                http_path=path,
                arguments=merged_args,
                order_id=merged_order_id,
                resource_region=None,
                status_code=422,
                error_code="validation_error",
                duration_ms=0,
                result=None,
                retention_state=retention_state,
            )
            raise

        invalid_details = validate_path_params(matched.path_params)
        if invalid_details:
            _record_and_commit_audit(
                session,
                settings=settings,
                now_dt=clock(),
                request_id=request_id,
                principal=principal,
                tool=matched.entry.name,
                http_method=method,
                http_path=path,
                arguments=arguments,
                order_id=order_id,
                resource_region=None,
                status_code=422,
                error_code="validation_error",
                duration_ms=0,
                result=None,
                retention_state=retention_state,
            )
            raise ValidationAppError(invalid_details)

        upstream_path = matched.render_upstream_path()
        start = time.perf_counter()

        try:
            resp = upstream.forward_read(
                method=method,
                path=upstream_path,
                query=request.url.query,
                inbound_headers=request.headers,
                authorization=authorization,
                request_id=request_id,
                purpose="proxy",
            )
        except UpstreamUnavailableError:
            duration_ms = int(round((time.perf_counter() - start) * 1000))
            _record_and_commit_audit(
                session,
                settings=settings,
                now_dt=clock(),
                request_id=request_id,
                principal=principal,
                tool=matched.entry.name,
                http_method=method,
                http_path=upstream_path,
                arguments=arguments,
                order_id=order_id,
                resource_region=None,
                status_code=503,
                error_code="upstream_unavailable",
                duration_ms=duration_ms,
                result=None,
                retention_state=retention_state,
            )
            raise

        duration_ms = int(round((time.perf_counter() - start) * 1000))
        body_json = resp.json_dict()
        is_list_tool = matched.entry.name.startswith("list_")
        resource_region: str | None = None
        error_code: str | None = None
        result_summary: dict[str, Any] | None = None

        if 200 <= resp.status_code < 300:
            if is_list_tool:
                if body_json is not None and "total" in body_json:
                    result_summary = {"total": body_json["total"]}
            elif body_json is not None and isinstance(body_json.get("region"), str):
                resource_region = body_json["region"]
        else:
            error_code, _ = resp.extract_error()

        _record_and_commit_audit(
            session,
            settings=settings,
            now_dt=clock(),
            request_id=request_id,
            principal=principal,
            tool=matched.entry.name,
            http_method=method,
            http_path=upstream_path,
            arguments=arguments,
            order_id=order_id,
            resource_region=resource_region,
            status_code=resp.status_code,
            error_code=error_code,
            duration_ms=duration_ms,
            result=result_summary,
            retention_state=retention_state,
        )

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp.filtered_response_headers(request_id),
        )

    # Write operation (G7: method != GET) -> check policy, intercept into approval
    policy = get_write_policy(matched.entry.name)
    if policy is None:
        _record_and_commit_audit(
            session,
            settings=settings,
            now_dt=clock(),
            request_id=request_id,
            principal=principal,
            tool=matched.entry.name,
            http_method=method,
            http_path=path,
            arguments=dict(matched.path_params),
            order_id=matched.path_params.get("order_id"),
            resource_region=None,
            status_code=403,
            error_code="write_not_permitted",
            duration_ms=0,
            result=None,
            retention_state=retention_state,
        )
        raise ForbiddenError(
            "write_not_permitted",
            f"写操作 {matched.entry.name} 未配置审批策略，已拒绝执行。",
        )

    outcome = approvals.submit_refund_approval(
        session,
        settings=settings,
        upstream=upstream,
        now=clock(),
        request_id=request_id,
        principal=principal,
        authorization=authorization,
        tool=matched.entry.name,
        http_method=method,
        http_path=path,
        raw_body=raw_body,
        retention_state=retention_state,
    )
    if outcome.raw_response is not None:
        return Response(
            content=outcome.raw_response.content,
            status_code=outcome.status_code,
            headers=outcome.raw_response.filtered_response_headers(request_id),
        )

    assert outcome.approval is not None
    resp_headers = {"X-Request-Id": request_id}
    if outcome.headers:
        resp_headers.update(outcome.headers)
    return JSONResponse(
        status_code=outcome.status_code,
        content=outcome.approval.model_dump(exclude_none=True),
        headers=resp_headers,
    )
