from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app import audit
from app.auth import Principal
from app.clock import Clock, format_iso, to_utc_naive
from app.config import Settings
from app.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    InternalError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamInvalidResponseError,
    UpstreamUnavailableError,
    ValidationAppError,
    validation_details_from_pydantic,
)
from app.json_utils import (
    RawBody,
    extract_audit_json_dict,
    extract_valid_order_id,
    parse_strict_json_dict,
)
from app.log import log_approval_state, log_execution_result_conflict, log_internal_error
from app.models import ApprovalModel, AuditEventModel
from app.policy import build_order_snapshot
from app.schemas import (
    Approval,
    ApprovalListResponse,
    ApprovalStatus,
    ApproveRequest,
    AuditEventOut,
    AuditEventType,
    ConsoleApprovalDetail,
    ConsoleApprovalListResponse,
    ConsoleApprovalSummary,
    ReasonCategory,
    RefundArgs,
    Region,
    RejectRequest,
)
from app.upstream import UpstreamClient, UpstreamResponse

logger = logging.getLogger("guard.approvals")

WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ActionResult:
    status_code: int
    approval: Approval | None = None
    raw_response: UpstreamResponse | None = None
    headers: dict[str, str] | None = None


def cents_to_yuan(cents: int) -> float:
    return round(cents / 100, 2)


def build_approval_message(row: ApprovalModel, ttl_hours: int) -> str:
    if row.status == "pending":
        return f"已提交退款申请 {row.approval_id}，等待主管审批。退款尚未执行。"
    if row.status == "executing":
        return f"主管已同意退款申请 {row.approval_id}，退款正在执行，结果尚未确认。"
    if row.status == "executed":
        return f"主管已同意退款申请 {row.approval_id}，退款已执行，退款单号 {row.refund_id}。"
    if row.status == "execution_failed":
        detail = (row.failure_message or "未知原因").rstrip("。")
        return (
            f"主管已同意退款申请 {row.approval_id}，但订单系统拒绝执行：{detail}。"
            "退款未执行，如仍需退款请重新提交申请。"
        )
    if row.status == "rejected":
        comment = (row.decision_comment or "").rstrip("。")
        return f"主管已拒绝退款申请 {row.approval_id}：{comment}。退款未执行。"
    return f"退款申请 {row.approval_id} 超过 {ttl_hours} 小时未审批，已自动失效。退款未执行。"


def to_approval(row: ApprovalModel, settings: Settings) -> Approval:
    args = json.loads(row.arguments)
    return Approval(
        approval_id=row.approval_id,
        status=cast(ApprovalStatus, row.status),
        message=build_approval_message(row, settings.approval_ttl_hours),
        tool=row.tool,
        order_id=row.order_id,
        region=cast(Region, row.region),
        amount=cents_to_yuan(row.amount_cents),
        reason_category=cast(ReasonCategory, args["reason_category"]),
        reason=str(args["reason"]),
        ticket_id=args.get("ticket_id"),
        applicant_sub=row.applicant_sub,
        applicant_name=row.applicant_name,
        submitted_at=format_iso(row.submitted_at, settings.timezone) or "",
        expires_at=format_iso(row.expires_at, settings.timezone) or "",
        decided_by_sub=row.decided_by_sub,
        decided_by_name=row.decided_by_name,
        decided_at=format_iso(row.decided_at, settings.timezone),
        decision_comment=row.decision_comment,
        refund_id=row.refund_id,
        executed_at=format_iso(row.executed_at, settings.timezone),
        failure_code=row.failure_code,
        failure_message=row.failure_message,
    )


def to_console_summary(
    row: ApprovalModel,
    settings: Settings,
    *,
    has_late_execution_conflict: bool = False,
) -> ConsoleApprovalSummary:
    base = to_approval(row, settings)
    return ConsoleApprovalSummary(
        **base.model_dump(),
        applicant_username=row.applicant_username,
        applicant_role=row.applicant_role,
        has_late_execution_conflict=has_late_execution_conflict,
    )


def _find_late_execution_conflict(
    status: str,
    events: list[AuditEventOut],
) -> tuple[bool, str | None]:
    if status != "execution_failed":
        return False, None
    has_conflict = False
    for ev in reversed(events):
        res = ev.result
        if (
            ev.event_type == "approval_execute"
            and ev.status_code is not None
            and 200 <= ev.status_code < 300
            and res is not None
            and res.get("stale") is True
        ):
            has_conflict = True
            raw_refund_id = res.get("refund_id")
            if isinstance(raw_refund_id, str) and raw_refund_id:
                return True, raw_refund_id
    return has_conflict, None


def to_console_detail(
    session: Session,
    row: ApprovalModel,
    settings: Settings,
) -> ConsoleApprovalDetail:
    events = audit.get_approval_events(
        session,
        approval_id=row.approval_id,
        tz_name=settings.timezone,
    )
    has_conflict, late_refund_id = _find_late_execution_conflict(row.status, events)
    summary = to_console_summary(
        row,
        settings,
        has_late_execution_conflict=has_conflict,
    )
    if has_conflict:
        late_notice: str | None = (
            f"订单系统另有一次执行成功，退款单号 {late_refund_id}，请人工核对"
            if late_refund_id is not None
            else "订单系统另有一次执行成功，退款单号未知，请到订单系统人工核对"
        )
    else:
        late_notice = None
    return ConsoleApprovalDetail(
        **summary.model_dump(),
        applicant_regions=row.applicant_regions,
        order_snapshot=json.loads(row.order_snapshot),
        attempts=row.attempts,
        failure_status=row.failure_status,
        late_executed_refund_id=late_refund_id,
        late_execution_notice=late_notice,
        events=events,
    )


def _next_approval_id(session: Session) -> str:
    last_id = session.scalar(
        select(ApprovalModel.approval_id).order_by(ApprovalModel.approval_id.desc()).limit(1)
    )
    if last_id is None:
        return "AP000001"
    return f"AP{int(last_id[2:]) + 1:06d}"


@contextmanager
def _write_guard(session: Session, *, request_id: str, operation: str) -> Iterator[None]:
    with WRITE_LOCK:
        try:
            yield
        except AppError:
            raise
        except Exception as exc:
            session.rollback()
            log_internal_error(
                logger,
                request_id=request_id,
                operation=operation,
                error_type=type(exc).__name__,
            )
            raise InternalError("数据写入失败，请求已中止。") from exc


def _commit_and_emit(
    session: Session,
    audit_rows: list[AuditEventModel],
    tz_name: str,
) -> None:
    session.commit()
    for row in audit_rows:
        audit.emit_audit_log(row, tz_name)


def expire_overdue_locked(
    session: Session,
    *,
    now: datetime,
    request_id: str,
    settings: Settings,
    retention_state: audit.AuditRetentionState | None = None,
) -> list[AuditEventModel]:
    now_naive = to_utc_naive(now)
    overdue = session.scalars(
        select(ApprovalModel)
        .where(ApprovalModel.status == "pending", ApprovalModel.expires_at <= now_naive)
        .order_by(ApprovalModel.approval_id.asc())
    ).all()
    created_audits: list[AuditEventModel] = []
    for row in overdue:
        row.status = "expired"
        ev = audit.record_event(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            event_type="approval_expire",
            channel="system",
            actor_sub="system",
            actor_username=None,
            actor_name=None,
            actor_role="system",
            actor_regions="",
            token_jti=None,
            tool=row.tool,
            http_method=None,
            http_path=None,
            arguments={},
            order_id=row.order_id,
            resource_region=row.region,
            outcome="success",
            status_code=None,
            error_code=None,
            duration_ms=0,
            approval_id=row.approval_id,
            result={"from": "pending", "to": "expired"},
            retention_state=retention_state,
        )
        created_audits.append(ev)
        log_approval_state(
            logger,
            approval_id=row.approval_id,
            from_status="pending",
            to_status="expired",
            by="system",
        )
    return created_audits


def expire_overdue_approvals(
    session: Session,
    *,
    now: datetime,
    request_id: str,
    settings: Settings,
    retention_state: audit.AuditRetentionState | None = None,
) -> int:
    with _write_guard(session, request_id=request_id, operation="expire_overdue_approvals"):
        audits = expire_overdue_locked(
            session,
            now=now,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )
        if audits:
            _commit_and_emit(session, audits, settings.timezone)
        return len(audits)


def _parse_json_dict(raw_body: bytes, *, allow_empty: bool = False) -> dict[str, Any]:
    return parse_strict_json_dict(raw_body, allow_empty=allow_empty)


def _record_submit_audit(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
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
    approval_id: str | None,
    pending_approval: bool = False,
    retention_state: audit.AuditRetentionState | None = None,
) -> AuditEventModel:
    return audit.record_event(
        session,
        settings=settings,
        now=now,
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
        outcome=audit.status_to_outcome(status_code, pending_approval=pending_approval),
        status_code=status_code,
        error_code=error_code,
        duration_ms=duration_ms,
        approval_id=approval_id,
        retention_state=retention_state,
    )


def submit_refund_approval(
    session: Session,
    *,
    settings: Settings,
    upstream: UpstreamClient,
    now: datetime,
    request_id: str,
    principal: Principal,
    authorization: str,
    tool: str,
    http_method: str,
    http_path: str,
    raw_body: RawBody,
    retention_state: audit.AuditRetentionState | None = None,
) -> ActionResult:
    start = time.perf_counter()
    if raw_body.too_large:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        with _write_guard(
            session,
            request_id=request_id,
            operation="submit_payload_too_large_audit",
        ):
            ev = _record_submit_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments={},
                order_id=None,
                resource_region=None,
                status_code=413,
                error_code="payload_too_large",
                duration_ms=duration_ms,
                approval_id=None,
                retention_state=retention_state,
            )
            _commit_and_emit(session, [ev], settings.timezone)
        raise PayloadTooLargeError()

    raw_dict: dict[str, Any] = {}
    try:
        raw_dict = _parse_json_dict(raw_body.data, allow_empty=False)
        args = RefundArgs.model_validate(raw_dict)
    except (ValidationAppError, ValidationError) as exc:
        if isinstance(exc, ValidationAppError):
            err = exc
            invalid_args = extract_audit_json_dict(raw_body.data, exc)
        else:
            err = ValidationAppError(validation_details_from_pydantic(exc))
            invalid_args = raw_dict
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        oid = extract_valid_order_id(invalid_args)
        with _write_guard(session, request_id=request_id, operation="submit_validation_audit"):
            ev = _record_submit_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments=invalid_args,
                order_id=oid,
                resource_region=None,
                status_code=422,
                error_code="validation_error",
                duration_ms=duration_ms,
                approval_id=None,
                retention_state=retention_state,
            )
            _commit_and_emit(session, [ev], settings.timezone)
        raise err from exc

    audit_args = args.model_dump(exclude_none=True)

    # Precheck read outside lock and transaction
    try:
        precheck = upstream.precheck_order(
            order_id=args.order_id,
            authorization=authorization,
            request_id=request_id,
        )
    except UpstreamUnavailableError:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        with _write_guard(
            session,
            request_id=request_id,
            operation="submit_precheck_unavailable_audit",
        ):
            ev = _record_submit_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments=audit_args,
                order_id=args.order_id,
                resource_region=None,
                status_code=503,
                error_code="upstream_unavailable",
                duration_ms=duration_ms,
                approval_id=None,
                retention_state=retention_state,
            )
            _commit_and_emit(session, [ev], settings.timezone)
        raise

    if precheck.status_code != 200:
        err_code, _ = precheck.extract_error()
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        with _write_guard(
            session,
            request_id=request_id,
            operation="submit_precheck_rejected_audit",
        ):
            ev = _record_submit_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments=audit_args,
                order_id=args.order_id,
                resource_region=None,
                status_code=precheck.status_code,
                error_code=err_code,
                duration_ms=duration_ms,
                approval_id=None,
                retention_state=retention_state,
            )
            _commit_and_emit(session, [ev], settings.timezone)
        return ActionResult(status_code=precheck.status_code, raw_response=precheck)

    order_data = precheck.json_dict()
    if (
        order_data is None
        or order_data.get("order_id") != args.order_id
        or not isinstance(order_data.get("region"), str)
        or not order_data["region"].strip()
    ):
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        with _write_guard(
            session,
            request_id=request_id,
            operation="submit_precheck_invalid_response_audit",
        ):
            ev = _record_submit_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments=audit_args,
                order_id=args.order_id,
                resource_region=None,
                status_code=502,
                error_code="upstream_invalid_response",
                duration_ms=duration_ms,
                approval_id=None,
                retention_state=retention_state,
            )
            _commit_and_emit(session, [ev], settings.timezone)
        raise UpstreamInvalidResponseError()

    region = order_data["region"].strip()
    snapshot = build_order_snapshot(order_data)

    with _write_guard(session, request_id=request_id, operation="submit_refund_approval"):
        audits = expire_overdue_locked(
            session,
            now=now,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )

        # Step 4: client idempotency key deduplication
        if args.idempotency_key is not None:
            existing_idem = session.scalar(
                select(ApprovalModel).where(
                    ApprovalModel.client_idempotency_key == args.idempotency_key
                )
            )
            if existing_idem is not None:
                duration_ms = int(round((time.perf_counter() - start) * 1000))
                if (
                    existing_idem.order_id == args.order_id
                    and existing_idem.amount_cents == args.amount_cents
                ):
                    ev = _record_submit_audit(
                        session,
                        settings=settings,
                        now=now,
                        request_id=request_id,
                        principal=principal,
                        tool=tool,
                        http_method=http_method,
                        http_path=http_path,
                        arguments=audit_args,
                        order_id=existing_idem.order_id,
                        resource_region=existing_idem.region,
                        status_code=200,
                        error_code=None,
                        duration_ms=duration_ms,
                        approval_id=existing_idem.approval_id,
                        retention_state=retention_state,
                    )
                    audits.append(ev)
                    _commit_and_emit(session, audits, settings.timezone)
                    return ActionResult(
                        status_code=200,
                        approval=to_approval(existing_idem, settings),
                    )
                ev = _record_submit_audit(
                    session,
                    settings=settings,
                    now=now,
                    request_id=request_id,
                    principal=principal,
                    tool=tool,
                    http_method=http_method,
                    http_path=http_path,
                    arguments=audit_args,
                    order_id=args.order_id,
                    resource_region=None,
                    status_code=409,
                    error_code="idempotency_conflict",
                    duration_ms=duration_ms,
                    approval_id=None,
                    retention_state=retention_state,
                )
                audits.append(ev)
                _commit_and_emit(session, audits, settings.timezone)
                raise ConflictError(
                    "idempotency_conflict",
                    f"幂等键 {args.idempotency_key} 已被另一笔参数不同的退款申请使用。",
                )

        # Step 5: active approval uniqueness per order_id
        existing_active = session.scalar(
            select(ApprovalModel).where(
                ApprovalModel.order_id == args.order_id,
                ApprovalModel.status.in_(("pending", "executing")),
            )
        )
        if existing_active is not None:
            duration_ms = int(round((time.perf_counter() - start) * 1000))
            ev = _record_submit_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                tool=tool,
                http_method=http_method,
                http_path=http_path,
                arguments=audit_args,
                order_id=args.order_id,
                resource_region=None,
                status_code=409,
                error_code="approval_pending_exists",
                duration_ms=duration_ms,
                approval_id=None,
                retention_state=retention_state,
            )
            audits.append(ev)
            _commit_and_emit(session, audits, settings.timezone)
            submitted_str = format_iso(existing_active.submitted_at, settings.timezone)
            raise ConflictError(
                "approval_pending_exists",
                f"订单 {args.order_id} 已有待处理的退款申请 {existing_active.approval_id}"
                f"（金额 ¥{cents_to_yuan(existing_active.amount_cents):.2f}，"
                f"提交时间 {submitted_str}），请勿重复提交。",
            )

        # Step 6: create approval + audit in same transaction
        approval_id = _next_approval_id(session)
        execution_key = f"{approval_id}-{secrets.token_hex(4)}"
        expires_at = now + timedelta(hours=settings.approval_ttl_hours)

        row = ApprovalModel(
            approval_id=approval_id,
            tool=tool,
            status="pending",
            region=region,
            order_id=args.order_id,
            amount_cents=args.amount_cents,
            arguments=json.dumps(args.business_arguments(), ensure_ascii=False, allow_nan=False),
            order_snapshot=json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
            client_idempotency_key=args.idempotency_key,
            execution_key=execution_key,
            applicant_sub=principal.sub,
            applicant_username=principal.username,
            applicant_name=principal.name,
            applicant_role=principal.role,
            applicant_regions=principal.regions_csv,
            submit_request_id=request_id,
            submitted_at=to_utc_naive(now),
            expires_at=to_utc_naive(expires_at),
            attempts=0,
        )
        session.add(row)
        session.flush()

        duration_ms = int(round((time.perf_counter() - start) * 1000))
        ev = _record_submit_audit(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            tool=tool,
            http_method=http_method,
            http_path=http_path,
            arguments=audit_args,
            order_id=args.order_id,
            resource_region=region,
            status_code=202,
            error_code=None,
            duration_ms=duration_ms,
            approval_id=approval_id,
            pending_approval=True,
            retention_state=retention_state,
        )
        audits.append(ev)
        _commit_and_emit(session, audits, settings.timezone)

    log_approval_state(
        logger,
        approval_id=approval_id,
        from_status="none",
        to_status="pending",
        by=principal.sub,
    )
    return ActionResult(
        status_code=202,
        approval=to_approval(row, settings),
        headers={"Location": f"/guard/v1/approvals/{approval_id}"},
    )


def _record_decision_audit(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    request_id: str,
    principal: Principal,
    event_type: AuditEventType,
    approval: ApprovalModel,
    http_path: str,
    arguments: dict[str, Any],
    status_code: int | None,
    error_code: str | None,
    duration_ms: int = 0,
    on_behalf_of_sub: str | None = None,
    result: dict[str, Any] | None = None,
    retention_state: audit.AuditRetentionState | None = None,
) -> AuditEventModel:
    outcome = audit.status_to_outcome(status_code)
    resource_region = None if outcome == "access_denied" else approval.region
    return audit.record_event(
        session,
        settings=settings,
        now=now,
        request_id=request_id,
        event_type=event_type,
        channel="console",
        actor_sub=principal.sub,
        actor_username=principal.username,
        actor_name=principal.name,
        actor_role=principal.role,
        actor_regions=principal.regions_csv,
        token_jti=principal.jti,
        tool=approval.tool,
        http_method="POST",
        http_path=http_path,
        arguments=arguments,
        order_id=approval.order_id,
        resource_region=resource_region,
        outcome=outcome,
        status_code=status_code,
        error_code=error_code,
        duration_ms=duration_ms,
        approval_id=approval.approval_id,
        on_behalf_of_sub=on_behalf_of_sub,
        result=result,
        retention_state=retention_state,
    )


def _audit_pre_lookup_failure(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    request_id: str,
    principal: Principal,
    approval_id: str,
    event_type: AuditEventType,
    http_path: str,
    arguments: dict[str, Any],
    status_code: int,
    error_code: str,
    include_on_behalf: bool = False,
    retention_state: audit.AuditRetentionState | None = None,
) -> None:
    with _write_guard(session, request_id=request_id, operation="decision_pre_lookup_audit"):
        audits = expire_overdue_locked(
            session,
            now=now,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )
        row = session.get(ApprovalModel, approval_id)
        if row is not None:
            ev = _record_decision_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                event_type=event_type,
                approval=row,
                http_path=http_path,
                arguments=arguments,
                status_code=status_code,
                error_code=error_code,
                on_behalf_of_sub=row.applicant_sub if include_on_behalf else None,
                retention_state=retention_state,
            )
            audits.append(ev)
        if audits:
            _commit_and_emit(session, audits, settings.timezone)


def _check_region_and_self_locked(
    session: Session,
    *,
    row: ApprovalModel,
    principal: Principal,
    settings: Settings,
    now: datetime,
    request_id: str,
    event_type: AuditEventType,
    http_path: str,
    arguments: dict[str, Any],
    audits: list[AuditEventModel],
    include_on_behalf: bool = False,
    retention_state: audit.AuditRetentionState | None = None,
) -> None:
    if row.region not in principal.regions:
        ev = _record_decision_audit(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            event_type=event_type,
            approval=row,
            http_path=http_path,
            arguments=arguments,
            status_code=403,
            error_code="approval_access_denied",
            on_behalf_of_sub=row.applicant_sub if include_on_behalf else None,
            retention_state=retention_state,
        )
        audits.append(ev)
        _commit_and_emit(session, audits, settings.timezone)
        raise ForbiddenError(
            "approval_access_denied",
            f"审批单 {row.approval_id} 不在你的负责区域内。",
        )

    if not settings.allow_self_approval and principal.sub == row.applicant_sub:
        ev = _record_decision_audit(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            event_type=event_type,
            approval=row,
            http_path=http_path,
            arguments=arguments,
            status_code=403,
            error_code="self_approval_forbidden",
            on_behalf_of_sub=row.applicant_sub if include_on_behalf else None,
            retention_state=retention_state,
        )
        audits.append(ev)
        _commit_and_emit(session, audits, settings.timezone)
        raise ForbiddenError(
            "self_approval_forbidden",
            "不能审批自己提交的退款申请，请由其他有权限的主管处理。",
        )


def _finalize_execution(
    session: Session,
    *,
    approval_id: str,
    settings: Settings,
    now: datetime,
    request_id: str,
    principal: Principal,
    exec_payload: dict[str, Any],
    resp: UpstreamResponse | None,
    duration_ms: int,
    is_retry: bool,
    retention_state: audit.AuditRetentionState | None = None,
) -> ActionResult:
    with _write_guard(session, request_id=request_id, operation="finalize_execution"):
        row = session.get(ApprovalModel, approval_id, populate_existing=True)
        assert row is not None

        status_code: int | None = None
        err_code: str | None = None
        to_status = "executing"
        extra_result: dict[str, Any] = {}
        late_success = False
        late_success_refund_id = ""
        late_success_created_by: str | None = None
        log_transition = False
        return_raw = False
        where_clauses: list[Any] = [
            ApprovalModel.approval_id == approval_id,
            ApprovalModel.status == "executing",
        ]
        values: dict[str, Any]

        if resp is None:
            values = {
                "failure_status": None,
                "failure_code": None,
                "failure_message": "订单系统无响应或超时，执行结果尚未确认。",
            }
        elif 200 <= resp.status_code < 300:
            data = resp.json_dict() or {}
            raw_refund_id = data.get("refund_id")
            refund_id = raw_refund_id.strip() if isinstance(raw_refund_id, str) else ""
            created_by = data.get("created_by")
            created_by_str = str(created_by) if created_by is not None else principal.sub
            status_code = resp.status_code
            to_status = "executed"
            extra_result = {"refund_id": refund_id}
            late_success = True
            late_success_refund_id = refund_id
            late_success_created_by = created_by_str
            log_transition = True
            values = {
                "status": "executed",
                "refund_id": refund_id,
                "executed_by_sub": created_by_str,
                "executed_at": to_utc_naive(now),
                "failure_status": None,
                "failure_code": None,
                "failure_message": None,
            }
        else:
            status_code = resp.status_code
            err_code, err_msg = resp.extract_error()
            if resp.status_code in (401, 403):
                return_raw = True
                if not is_retry and row.attempts == 1:
                    where_clauses.append(ApprovalModel.attempts == 1)
                    to_status = "pending"
                    log_transition = True
                    values = {
                        "status": "pending",
                        "decided_by_sub": None,
                        "decided_by_username": None,
                        "decided_by_name": None,
                        "decided_at": None,
                        "decision_comment": None,
                        "attempts": 0,
                        "failure_status": None,
                        "failure_code": None,
                        "failure_message": None,
                    }
                else:
                    values = {
                        "failure_status": resp.status_code,
                        "failure_code": err_code,
                        "failure_message": err_msg or f"订单系统返回 {resp.status_code}",
                    }
            elif 400 <= resp.status_code < 500:
                to_status = "execution_failed"
                log_transition = True
                values = {
                    "status": "execution_failed",
                    "failure_status": resp.status_code,
                    "failure_code": err_code,
                    "failure_message": err_msg or f"订单系统拒绝执行（HTTP {resp.status_code}）",
                }
            else:
                values = {
                    "failure_status": resp.status_code,
                    "failure_code": err_code,
                    "failure_message": err_msg or f"订单系统返回 {resp.status_code}",
                }

        res = session.execute(update(ApprovalModel).where(*where_clauses).values(**values))
        session.refresh(row)

        if res.rowcount == 0:
            stale_result: dict[str, Any] = {
                "from": row.status,
                "to": row.status,
                "stale": True,
                "note": "迟到的执行结果，未改变状态",
            }
            if late_success:
                stale_result["refund_id"] = late_success_refund_id
                stale_result["created_by"] = late_success_created_by
            ev = _record_decision_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                event_type="approval_execute",
                approval=row,
                http_path="/api/v1/refunds",
                arguments=exec_payload,
                status_code=status_code,
                error_code=err_code,
                duration_ms=duration_ms,
                on_behalf_of_sub=row.applicant_sub,
                result=stale_result,
                retention_state=retention_state,
            )
            _commit_and_emit(session, [ev], settings.timezone)
            if row.status == "execution_failed" and late_success:
                log_execution_result_conflict(
                    logger,
                    approval_id=approval_id,
                    refund_id=late_success_refund_id,
                    request_id=request_id,
                )
            return ActionResult(status_code=200, approval=to_approval(row, settings))

        result_payload: dict[str, Any] = {"from": "executing", "to": to_status, **extra_result}
        ev = _record_decision_audit(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            event_type="approval_execute",
            approval=row,
            http_path="/api/v1/refunds",
            arguments=exec_payload,
            status_code=status_code,
            error_code=err_code,
            duration_ms=duration_ms,
            on_behalf_of_sub=row.applicant_sub,
            result=result_payload,
            retention_state=retention_state,
        )
        _commit_and_emit(session, [ev], settings.timezone)
        if log_transition:
            log_approval_state(
                logger,
                approval_id=approval_id,
                from_status="executing",
                to_status=to_status,
                by=principal.sub,
            )
        if return_raw:
            assert resp is not None
            return ActionResult(status_code=resp.status_code, raw_response=resp)
        return ActionResult(status_code=200, approval=to_approval(row, settings))


def approve_approval(
    session: Session,
    *,
    approval_id: str,
    settings: Settings,
    upstream: UpstreamClient,
    clock: Clock,
    request_id: str,
    principal: Principal,
    authorization: str,
    raw_body: RawBody,
    retention_state: audit.AuditRetentionState | None = None,
) -> ActionResult:
    now = clock()
    http_path = f"/guard/v1/console/approvals/{approval_id}/approve"

    if principal.role != settings.approver_role:
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_approve",
            http_path=http_path,
            arguments={},
            status_code=403,
            error_code="insufficient_role",
            retention_state=retention_state,
        )
        raise ForbiddenError("insufficient_role", "当前角色无权审批，仅主管可用。")

    if raw_body.too_large:
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_approve",
            http_path=http_path,
            arguments={},
            status_code=413,
            error_code="payload_too_large",
            retention_state=retention_state,
        )
        raise PayloadTooLargeError()

    raw_dict: dict[str, Any] = {}
    try:
        raw_dict = _parse_json_dict(raw_body.data, allow_empty=True)
        req = ApproveRequest.model_validate(raw_dict)
    except (ValidationAppError, ValidationError) as exc:
        if isinstance(exc, ValidationAppError):
            err = exc
            invalid_args = extract_audit_json_dict(raw_body.data, exc)
        else:
            err = ValidationAppError(validation_details_from_pydantic(exc))
            invalid_args = raw_dict
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_approve",
            http_path=http_path,
            arguments=invalid_args,
            status_code=422,
            error_code="validation_error",
            retention_state=retention_state,
        )
        raise err from exc

    action_args = {"comment": req.comment} if req.comment is not None else {}

    with _write_guard(session, request_id=request_id, operation="approve_approval"):
        audits = expire_overdue_locked(
            session,
            now=now,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )
        row = session.get(ApprovalModel, approval_id)
        if row is None:
            if audits:
                _commit_and_emit(session, audits, settings.timezone)
            raise NotFoundError("approval_not_found", f"审批单 {approval_id} 不存在。")

        _check_region_and_self_locked(
            session,
            row=row,
            principal=principal,
            settings=settings,
            now=now,
            request_id=request_id,
            event_type="approval_approve",
            http_path=http_path,
            arguments=action_args,
            audits=audits,
            retention_state=retention_state,
        )

        res = session.execute(
            update(ApprovalModel)
            .where(ApprovalModel.approval_id == approval_id, ApprovalModel.status == "pending")
            .values(
                status="executing",
                decided_by_sub=principal.sub,
                decided_by_username=principal.username,
                decided_by_name=principal.name,
                decided_at=to_utc_naive(now),
                decision_comment=req.comment,
                attempts=1,
            )
        )
        if res.rowcount == 0:
            session.refresh(row)
            ev = _record_decision_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                event_type="approval_approve",
                approval=row,
                http_path=http_path,
                arguments=action_args,
                status_code=409,
                error_code="approval_not_pending",
                retention_state=retention_state,
            )
            audits.append(ev)
            _commit_and_emit(session, audits, settings.timezone)
            raise ConflictError(
                "approval_not_pending",
                f"审批单 {approval_id} 当前状态为 {row.status}，无法执行同意操作。",
            )

        session.refresh(row)
        exec_payload = dict(json.loads(row.arguments))
        exec_payload["idempotency_key"] = row.execution_key
        ev = _record_decision_audit(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            event_type="approval_approve",
            approval=row,
            http_path=http_path,
            arguments=action_args,
            status_code=200,
            error_code=None,
            result={"from": "pending", "to": "executing"},
            retention_state=retention_state,
        )
        audits.append(ev)
        _commit_and_emit(session, audits, settings.timezone)

    log_approval_state(
        logger,
        approval_id=approval_id,
        from_status="pending",
        to_status="executing",
        by=principal.sub,
    )

    resp, duration_ms = upstream.execute_refund(
        payload=exec_payload,
        authorization=authorization,
        request_id=request_id,
    )
    return _finalize_execution(
        session,
        approval_id=approval_id,
        settings=settings,
        now=clock(),
        request_id=request_id,
        principal=principal,
        exec_payload=exec_payload,
        resp=resp,
        duration_ms=duration_ms,
        is_retry=False,
        retention_state=retention_state,
    )


def reject_approval(
    session: Session,
    *,
    approval_id: str,
    settings: Settings,
    now: datetime,
    request_id: str,
    principal: Principal,
    raw_body: RawBody,
    retention_state: audit.AuditRetentionState | None = None,
) -> ActionResult:
    http_path = f"/guard/v1/console/approvals/{approval_id}/reject"

    if principal.role != settings.approver_role:
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_reject",
            http_path=http_path,
            arguments={},
            status_code=403,
            error_code="insufficient_role",
            retention_state=retention_state,
        )
        raise ForbiddenError("insufficient_role", "当前角色无权审批，仅主管可用。")

    if raw_body.too_large:
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_reject",
            http_path=http_path,
            arguments={},
            status_code=413,
            error_code="payload_too_large",
            retention_state=retention_state,
        )
        raise PayloadTooLargeError()

    raw_dict: dict[str, Any] = {}
    try:
        raw_dict = _parse_json_dict(raw_body.data, allow_empty=False)
        req = RejectRequest.model_validate(raw_dict)
    except (ValidationAppError, ValidationError) as exc:
        if isinstance(exc, ValidationAppError):
            err = exc
            invalid_args = extract_audit_json_dict(raw_body.data, exc)
        else:
            err = ValidationAppError(validation_details_from_pydantic(exc))
            invalid_args = raw_dict
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_reject",
            http_path=http_path,
            arguments=invalid_args,
            status_code=422,
            error_code="validation_error",
            retention_state=retention_state,
        )
        raise err from exc

    action_args = {"comment": req.comment}

    with _write_guard(session, request_id=request_id, operation="reject_approval"):
        audits = expire_overdue_locked(
            session,
            now=now,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )
        row = session.get(ApprovalModel, approval_id)
        if row is None:
            if audits:
                _commit_and_emit(session, audits, settings.timezone)
            raise NotFoundError("approval_not_found", f"审批单 {approval_id} 不存在。")

        _check_region_and_self_locked(
            session,
            row=row,
            principal=principal,
            settings=settings,
            now=now,
            request_id=request_id,
            event_type="approval_reject",
            http_path=http_path,
            arguments=action_args,
            audits=audits,
            retention_state=retention_state,
        )

        res = session.execute(
            update(ApprovalModel)
            .where(ApprovalModel.approval_id == approval_id, ApprovalModel.status == "pending")
            .values(
                status="rejected",
                decided_by_sub=principal.sub,
                decided_by_username=principal.username,
                decided_by_name=principal.name,
                decided_at=to_utc_naive(now),
                decision_comment=req.comment,
            )
        )
        if res.rowcount == 0:
            session.refresh(row)
            ev = _record_decision_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                event_type="approval_reject",
                approval=row,
                http_path=http_path,
                arguments=action_args,
                status_code=409,
                error_code="approval_not_pending",
                retention_state=retention_state,
            )
            audits.append(ev)
            _commit_and_emit(session, audits, settings.timezone)
            raise ConflictError(
                "approval_not_pending",
                f"审批单 {approval_id} 当前状态为 {row.status}，无法执行拒绝操作。",
            )

        session.refresh(row)
        ev = _record_decision_audit(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            event_type="approval_reject",
            approval=row,
            http_path=http_path,
            arguments=action_args,
            status_code=200,
            error_code=None,
            result={"from": "pending", "to": "rejected"},
            retention_state=retention_state,
        )
        audits.append(ev)
        _commit_and_emit(session, audits, settings.timezone)

    log_approval_state(
        logger,
        approval_id=approval_id,
        from_status="pending",
        to_status="rejected",
        by=principal.sub,
    )
    return ActionResult(status_code=200, approval=to_approval(row, settings))


def retry_approval(
    session: Session,
    *,
    approval_id: str,
    settings: Settings,
    upstream: UpstreamClient,
    clock: Clock,
    request_id: str,
    principal: Principal,
    authorization: str,
    retention_state: audit.AuditRetentionState | None = None,
) -> ActionResult:
    now = clock()
    http_path = f"/guard/v1/console/approvals/{approval_id}/retry"

    if principal.role != settings.approver_role:
        _audit_pre_lookup_failure(
            session,
            settings=settings,
            now=now,
            request_id=request_id,
            principal=principal,
            approval_id=approval_id,
            event_type="approval_execute",
            http_path=http_path,
            arguments={},
            status_code=403,
            error_code="insufficient_role",
            include_on_behalf=True,
            retention_state=retention_state,
        )
        raise ForbiddenError("insufficient_role", "当前角色无权重试执行，仅主管可用。")

    with _write_guard(session, request_id=request_id, operation="retry_approval"):
        audits = expire_overdue_locked(
            session,
            now=now,
            request_id=request_id,
            settings=settings,
            retention_state=retention_state,
        )
        row = session.get(ApprovalModel, approval_id)
        if row is None:
            if audits:
                _commit_and_emit(session, audits, settings.timezone)
            raise NotFoundError("approval_not_found", f"审批单 {approval_id} 不存在。")

        _check_region_and_self_locked(
            session,
            row=row,
            principal=principal,
            settings=settings,
            now=now,
            request_id=request_id,
            event_type="approval_execute",
            http_path=http_path,
            arguments={},
            audits=audits,
            include_on_behalf=True,
            retention_state=retention_state,
        )

        res = session.execute(
            update(ApprovalModel)
            .where(ApprovalModel.approval_id == approval_id, ApprovalModel.status == "executing")
            .values(attempts=ApprovalModel.attempts + 1)
        )
        if res.rowcount == 0:
            session.refresh(row)
            ev = _record_decision_audit(
                session,
                settings=settings,
                now=now,
                request_id=request_id,
                principal=principal,
                event_type="approval_execute",
                approval=row,
                http_path=http_path,
                arguments={},
                status_code=409,
                error_code="approval_not_pending",
                on_behalf_of_sub=row.applicant_sub,
                retention_state=retention_state,
            )
            audits.append(ev)
            _commit_and_emit(session, audits, settings.timezone)
            raise ConflictError(
                "approval_not_pending",
                f"审批单 {approval_id} 当前状态为 {row.status}，仅执行中状态允许重试。",
            )

        session.refresh(row)
        exec_payload = dict(json.loads(row.arguments))
        exec_payload["idempotency_key"] = row.execution_key
        _commit_and_emit(session, audits, settings.timezone)

    resp, duration_ms = upstream.execute_refund(
        payload=exec_payload,
        authorization=authorization,
        request_id=request_id,
    )
    return _finalize_execution(
        session,
        approval_id=approval_id,
        settings=settings,
        now=clock(),
        request_id=request_id,
        principal=principal,
        exec_payload=exec_payload,
        resp=resp,
        duration_ms=duration_ms,
        is_retry=True,
        retention_state=retention_state,
    )


def get_visible_approval(
    session: Session,
    *,
    approval_id: str,
    principal: Principal,
    now: datetime,
    request_id: str,
    settings: Settings,
    retention_state: audit.AuditRetentionState | None = None,
) -> ApprovalModel:
    expire_overdue_approvals(
        session,
        now=now,
        request_id=request_id,
        settings=settings,
        retention_state=retention_state,
    )
    row = session.get(ApprovalModel, approval_id)
    if row is None:
        raise NotFoundError("approval_not_found", f"审批单 {approval_id} 不存在。")
    if row.region not in principal.regions:
        raise ForbiddenError(
            "approval_access_denied",
            f"审批单 {approval_id} 不在你的负责区域内。",
        )
    return row


def list_visible_approvals(
    session: Session,
    *,
    principal: Principal,
    now: datetime,
    request_id: str,
    settings: Settings,
    order_id: str | None = None,
    statuses: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
    retention_state: audit.AuditRetentionState | None = None,
) -> tuple[list[ApprovalModel], int]:
    expire_overdue_approvals(
        session,
        now=now,
        request_id=request_id,
        settings=settings,
        retention_state=retention_state,
    )
    conditions: list[Any] = [ApprovalModel.region.in_(sorted(principal.regions))]
    if order_id is not None:
        conditions.append(ApprovalModel.order_id == order_id)
    if statuses:
        conditions.append(ApprovalModel.status.in_(statuses))
    where_clause = and_(*conditions)
    total = int(
        session.scalar(select(func.count()).select_from(ApprovalModel).where(where_clause)) or 0
    )
    rows = session.scalars(
        select(ApprovalModel)
        .where(where_clause)
        .order_by(ApprovalModel.submitted_at.desc(), ApprovalModel.approval_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def build_agent_list_response(
    rows: list[ApprovalModel],
    *,
    total: int,
    page: int,
    page_size: int,
    settings: Settings,
) -> ApprovalListResponse:
    return ApprovalListResponse(
        items=[to_approval(row, settings) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


def build_console_list_response(
    session: Session,
    rows: list[ApprovalModel],
    *,
    total: int,
    page: int,
    page_size: int,
    settings: Settings,
) -> ConsoleApprovalListResponse:
    failed_ids = [row.approval_id for row in rows if row.status == "execution_failed"]
    conflicted_ids = audit.find_late_execution_conflict_approval_ids(
        session,
        approval_ids=failed_ids,
    )
    return ConsoleApprovalListResponse(
        items=[
            to_console_summary(
                row,
                settings,
                has_late_execution_conflict=row.approval_id in conflicted_ids,
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
