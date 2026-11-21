from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import approvals, audit
from app.auth import Principal, get_principal, get_settings
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_session
from app.errors import (
    ForbiddenError,
    InternalError,
    NotFoundError,
    ValidationAppError,
    get_request_id,
    validation_details_from_pydantic,
)
from app.log import log_internal_error
from app.models import ApprovalModel
from app.schemas import AgentListApprovalsQuery

logger = logging.getLogger("guard.agent_api")

router = APIRouter(prefix="/guard/v1/approvals", tags=["agent_approvals"])


def _commit_agent_audit(
    session: Session,
    *,
    settings: Settings,
    now_dt: Any,
    request_id: str,
    principal: Principal,
    tool: str,
    http_path: str,
    arguments: dict[str, Any],
    order_id: str | None,
    resource_region: str | None,
    approval_id: str | None,
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
                http_method="GET",
                http_path=http_path,
                arguments=arguments,
                order_id=order_id,
                resource_region=resource_region,
                outcome=audit.status_to_outcome(status_code),
                status_code=status_code,
                error_code=error_code,
                duration_ms=duration_ms,
                approval_id=approval_id,
                result=result,
                retention_state=retention_state,
            )
            session.commit()
    except Exception as exc:
        session.rollback()
        log_internal_error(
            logger,
            request_id=request_id,
            operation="agent_audit_write",
            error_type=type(exc).__name__,
        )
        raise InternalError("审计记录写入失败，请求已中止。") from exc
    audit.emit_audit_log(row, settings.timezone)


@router.get("")
def list_approvals(
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    start = time.perf_counter()
    request_id = get_request_id(request)
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    raw_params = dict(request.query_params)
    now_dt = clock()

    try:
        query = AgentListApprovalsQuery.model_validate(raw_params)
    except ValidationError as exc:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        _commit_agent_audit(
            session,
            settings=settings,
            now_dt=now_dt,
            request_id=request_id,
            principal=principal,
            tool="list_approvals",
            http_path="/guard/v1/approvals",
            arguments=raw_params,
            order_id=raw_params.get("order_id"),
            resource_region=None,
            approval_id=None,
            status_code=422,
            error_code="validation_error",
            duration_ms=duration_ms,
            result=None,
            retention_state=retention_state,
        )
        raise ValidationAppError(validation_details_from_pydantic(exc)) from exc

    statuses = [query.status] if query.status is not None else None
    rows, total = approvals.list_visible_approvals(
        session,
        principal=principal,
        now=now_dt,
        request_id=request_id,
        settings=settings,
        order_id=query.order_id,
        statuses=statuses,
        page=query.page,
        page_size=query.page_size,
        retention_state=retention_state,
    )
    payload = approvals.build_agent_list_response(
        rows,
        total=total,
        page=query.page,
        page_size=query.page_size,
        settings=settings,
    )
    duration_ms = int(round((time.perf_counter() - start) * 1000))
    _commit_agent_audit(
        session,
        settings=settings,
        now_dt=now_dt,
        request_id=request_id,
        principal=principal,
        tool="list_approvals",
        http_path="/guard/v1/approvals",
        arguments=query.model_dump(exclude_none=True),
        order_id=query.order_id,
        resource_region=None,
        approval_id=None,
        status_code=200,
        error_code=None,
        duration_ms=duration_ms,
        result={"total": total},
        retention_state=retention_state,
    )
    return JSONResponse(
        status_code=200,
        content=payload.model_dump(exclude_none=True),
        headers={"X-Request-Id": request_id},
    )


@router.get("/{approval_id}")
def get_approval(
    approval_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    start = time.perf_counter()
    request_id = get_request_id(request)
    retention_state: audit.AuditRetentionState = request.app.state.audit_retention_state
    http_path = f"/guard/v1/approvals/{approval_id}"
    now_dt = clock()

    try:
        row = approvals.get_visible_approval(
            session,
            approval_id=approval_id,
            principal=principal,
            now=now_dt,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )
    except NotFoundError:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        _commit_agent_audit(
            session,
            settings=settings,
            now_dt=now_dt,
            request_id=request_id,
            principal=principal,
            tool="get_approval",
            http_path=http_path,
            arguments={"approval_id": approval_id},
            order_id=None,
            resource_region=None,
            approval_id=approval_id,
            status_code=404,
            error_code="approval_not_found",
            duration_ms=duration_ms,
            result=None,
            retention_state=retention_state,
        )
        raise
    except ForbiddenError:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        existing = session.get(ApprovalModel, approval_id)
        _commit_agent_audit(
            session,
            settings=settings,
            now_dt=now_dt,
            request_id=request_id,
            principal=principal,
            tool="get_approval",
            http_path=http_path,
            arguments={"approval_id": approval_id},
            order_id=existing.order_id if existing is not None else None,
            resource_region=None,
            approval_id=approval_id,
            status_code=403,
            error_code="approval_access_denied",
            duration_ms=duration_ms,
            result=None,
            retention_state=retention_state,
        )
        raise

    out = approvals.to_approval(row, settings)
    duration_ms = int(round((time.perf_counter() - start) * 1000))
    _commit_agent_audit(
        session,
        settings=settings,
        now_dt=now_dt,
        request_id=request_id,
        principal=principal,
        tool="get_approval",
        http_path=http_path,
        arguments={"approval_id": approval_id},
        order_id=row.order_id,
        resource_region=row.region,
        approval_id=row.approval_id,
        status_code=200,
        error_code=None,
        duration_ms=duration_ms,
        result=None,
        retention_state=retention_state,
    )
    return JSONResponse(
        status_code=200,
        content=out.model_dump(exclude_none=True),
        headers={"X-Request-Id": request_id},
    )
