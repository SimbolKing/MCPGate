from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import approvals, audit
from app.auth import Principal, get_principal, get_settings, require_supervisor
from app.clock import Clock, get_clock, parse_iso
from app.config import Settings
from app.db import get_session
from app.errors import (
    InternalError,
    LoginFailedError,
    NotFoundError,
    UpstreamUnavailableError,
    ValidationAppError,
    get_request_id,
)
from app.json_utils import RawBody
from app.log import log_internal_error
from app.routers import read_raw_body
from app.schemas import (
    APPROVAL_STATUSES,
    AuditChannel,
    AuditEventType,
    AuditOutcome,
    LoginRequest,
    LoginResponse,
)
from app.upstream import UpstreamClient, encode_path_segment

logger = logging.getLogger("guard.console_api")

router = APIRouter(prefix="/guard/v1/console", tags=["console"])


def _parse_status_filter(raw_statuses: list[str] | None) -> list[str] | None:
    if not raw_statuses:
        return None
    parsed: list[str] = []
    for item in raw_statuses:
        for part in item.split(","):
            val = part.strip()
            if val:
                if val not in APPROVAL_STATUSES:
                    raise ValidationAppError(
                        [{"field": "status", "message": f"不支持的审批状态: {val}"}]
                    )
                parsed.append(val)
    return parsed or None


def _parse_optional_iso(value: str | None, field: str, tz_name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        return parse_iso(value, default_tz=tz_name)
    except ValueError as exc:
        raise ValidationAppError(
            [{"field": field, "message": f"时间格式必须为 ISO 8601: {value}"}]
        ) from exc


def _action_result_to_response(result: approvals.ActionResult, request_id: str) -> Response:
    if result.raw_response is not None:
        return Response(
            content=result.raw_response.content,
            status_code=result.status_code,
            headers=result.raw_response.filtered_response_headers(request_id),
        )
    assert result.approval is not None
    return JSONResponse(
        status_code=result.status_code,
        content=result.approval.model_dump(exclude_none=True),
        headers={"X-Request-Id": request_id},
    )


@router.post("/login")
def console_login(
    body: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    request_id = get_request_id(request)
    login_url = settings.dev_login_url.strip()
    if not login_url:
        raise NotFoundError("login_not_configured", "未配置开发登录，请使用粘贴令牌方式登录。")

    upstream: UpstreamClient = request.app.state.upstream
    resp = upstream.dev_login(
        login_url=login_url,
        username=body.username,
        password=body.password,
        request_id=request_id,
    )
    if resp.status_code == 401:
        raise LoginFailedError("用户名或密码错误。")
    if resp.status_code != 200:
        raise UpstreamUnavailableError("身份认证服务暂不可用，请稍后重试。")

    data = resp.json_dict()
    if data is None or not isinstance(data.get("access_token"), str):
        raise UpstreamUnavailableError("身份认证服务返回异常。")

    out = LoginResponse(
        access_token=data["access_token"],
        token_type=str(data.get("token_type") or "Bearer"),
        expires_in=int(data.get("expires_in") or 0),
    )
    return JSONResponse(
        status_code=200,
        content=out.model_dump(),
        headers={"X-Request-Id": request_id},
    )


@router.get("/approvals")
def console_list_approvals(
    request: Request,
    status: list[str] | None = Query(default=None),
    order_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    principal: Principal = Depends(require_supervisor),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    request_id = get_request_id(request)
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    statuses = _parse_status_filter(status)
    rows, total = approvals.list_visible_approvals(
        session,
        principal=principal,
        now=clock(),
        request_id=request_id,
        settings=settings,
        order_id=order_id.strip() if order_id and order_id.strip() else None,
        statuses=statuses,
        page=page,
        page_size=page_size,
        retention_state=retention_state,
    )
    payload = approvals.build_console_list_response(
        session,
        rows,
        total=total,
        page=page,
        page_size=page_size,
        settings=settings,
    )
    return JSONResponse(
        status_code=200,
        content=payload.model_dump(exclude_none=True),
        headers={"X-Request-Id": request_id},
    )


@router.get("/approvals/{approval_id}")
def console_get_approval(
    approval_id: str,
    request: Request,
    principal: Principal = Depends(require_supervisor),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    request_id = get_request_id(request)
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    row = approvals.get_visible_approval(
        session,
        approval_id=approval_id,
        principal=principal,
        now=clock(),
        request_id=request_id,
        settings=settings,
        retention_state=retention_state,
    )
    detail = approvals.to_console_detail(session, row, settings)
    return JSONResponse(
        status_code=200,
        content=detail.model_dump(exclude_none=True),
        headers={"X-Request-Id": request_id},
    )


@router.get("/approvals/{approval_id}/order")
def console_get_approval_order(
    approval_id: str,
    request: Request,
    principal: Principal = Depends(require_supervisor),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    request_id = get_request_id(request)
    upstream: UpstreamClient = request.app.state.upstream
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    now_dt = clock()

    row = approvals.get_visible_approval(
        session,
        approval_id=approval_id,
        principal=principal,
        now=now_dt,
        request_id=request_id,
        settings=settings,
        retention_state=retention_state,
    )

    order_path = f"/api/v1/orders/{encode_path_segment(row.order_id)}"
    authorization: str = request.state.authorization
    start = time.perf_counter()

    def _record_console_order_audit(
        status_code: int,
        error_code: str | None,
        resource_region: str | None,
        duration_ms: int,
    ) -> None:
        try:
            with approvals.WRITE_LOCK:
                ev = audit.record_event(
                    session,
                    settings=settings,
                    now=clock(),
                    request_id=request_id,
                    event_type="tool_call",
                    channel="console",
                    actor_sub=principal.sub,
                    actor_username=principal.username,
                    actor_name=principal.name,
                    actor_role=principal.role,
                    actor_regions=principal.regions_csv,
                    token_jti=principal.jti,
                    tool="get_order",
                    http_method="GET",
                    http_path=order_path,
                    arguments={"order_id": row.order_id},
                    order_id=row.order_id,
                    resource_region=resource_region,
                    outcome=audit.status_to_outcome(status_code),
                    status_code=status_code,
                    error_code=error_code,
                    duration_ms=duration_ms,
                    approval_id=row.approval_id,
                    retention_state=retention_state,
                )
                session.commit()
        except Exception as exc:
            session.rollback()
            log_internal_error(
                logger,
                request_id=request_id,
                operation="console_order_audit_write",
                error_type=type(exc).__name__,
            )
            raise InternalError("审计记录写入失败，请求已中止。") from exc
        audit.emit_audit_log(ev, settings.timezone)

    try:
        resp = upstream.forward_read(
            method="GET",
            path=order_path,
            query="",
            inbound_headers=request.headers,
            authorization=authorization,
            request_id=request_id,
            purpose="proxy",
        )
    except UpstreamUnavailableError:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        _record_console_order_audit(503, "upstream_unavailable", None, duration_ms)
        raise

    duration_ms = int(round((time.perf_counter() - start) * 1000))
    body_json = resp.json_dict()
    resource_region: str | None = None
    error_code: str | None = None
    if 200 <= resp.status_code < 300:
        if body_json is not None and isinstance(body_json.get("region"), str):
            resource_region = body_json["region"]
    else:
        error_code, _ = resp.extract_error()

    _record_console_order_audit(resp.status_code, error_code, resource_region, duration_ms)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp.filtered_response_headers(request_id),
    )


@router.post("/approvals/{approval_id}/approve")
def console_approve(
    approval_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    raw_body: RawBody = Depends(read_raw_body),
) -> Response:
    request_id = get_request_id(request)
    upstream: UpstreamClient = request.app.state.upstream
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    authorization: str = request.state.authorization
    result = approvals.approve_approval(
        session,
        approval_id=approval_id,
        settings=settings,
        upstream=upstream,
        clock=clock,
        request_id=request_id,
        principal=principal,
        authorization=authorization,
        raw_body=raw_body,
        retention_state=retention_state,
    )
    return _action_result_to_response(result, request_id)


@router.post("/approvals/{approval_id}/reject")
def console_reject(
    approval_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    raw_body: RawBody = Depends(read_raw_body),
) -> Response:
    request_id = get_request_id(request)
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    result = approvals.reject_approval(
        session,
        approval_id=approval_id,
        settings=settings,
        now=clock(),
        request_id=request_id,
        principal=principal,
        raw_body=raw_body,
        retention_state=retention_state,
    )
    return _action_result_to_response(result, request_id)


@router.post("/approvals/{approval_id}/retry")
def console_retry(
    approval_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    request_id = get_request_id(request)
    upstream: UpstreamClient = request.app.state.upstream
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    authorization: str = request.state.authorization
    result = approvals.retry_approval(
        session,
        approval_id=approval_id,
        settings=settings,
        upstream=upstream,
        clock=clock,
        request_id=request_id,
        principal=principal,
        authorization=authorization,
        retention_state=retention_state,
    )
    return _action_result_to_response(result, request_id)


@router.get("/audit-logs")
def console_audit_logs(
    request: Request,
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    actor: str | None = Query(default=None),
    event_type: AuditEventType | None = Query(default=None),
    channel: AuditChannel | None = Query(default=None),
    tool: str | None = Query(default=None),
    outcome: AuditOutcome | None = Query(default=None),
    order_id: str | None = Query(default=None),
    approval_id: str | None = Query(default=None),
    request_id_filter: str | None = Query(default=None, alias="request_id"),
    after_id: int | None = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_supervisor),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> JSONResponse:
    request_id = get_request_id(request)
    from_dt = _parse_optional_iso(from_ts, "from", settings.timezone)
    to_dt = _parse_optional_iso(to_ts, "to", settings.timezone)

    def _clean(val: str | None) -> str | None:
        return val.strip() if val and val.strip() else None

    result: Any = audit.query_events(
        session,
        principal=principal,
        tz_name=settings.timezone,
        from_dt=from_dt,
        to_dt=to_dt,
        actor=_clean(actor),
        event_type=event_type,
        channel=channel,
        tool=_clean(tool),
        outcome=outcome,
        order_id=_clean(order_id),
        approval_id=_clean(approval_id),
        request_id=_clean(request_id_filter),
        after_id=after_id,
        page=page,
        page_size=page_size,
    )
    items_out: list[dict[str, Any]] = []
    for item in result.items:
        dumped = item.model_dump(exclude_none=True)
        try:
            json.dumps(dumped, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except Exception as exc:
            logger.error(
                "audit_row_serialize_failed",
                extra={
                    "fields": {
                        "event": "audit_row_serialize_failed",
                        "request_id": request_id,
                        "audit_id": item.id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            dumped["arguments"] = dict(audit.CORRUPTED_JSON_PLACEHOLDER)
            if "result" in dumped:
                dumped["result"] = dict(audit.CORRUPTED_JSON_PLACEHOLDER)
        items_out.append(dumped)

    return JSONResponse(
        status_code=200,
        content={
            "items": items_out,
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        },
        headers={"X-Request-Id": request_id},
    )
