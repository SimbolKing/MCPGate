from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.clock import format_iso, to_utc_naive
from app.config import Settings
from app.json_utils import (
    MAX_AUDIT_DICT_KEYS,
    MAX_AUDIT_KEY_LEN,
    loads_strict_json,
    sanitize_json_value,
)
from app.log import log_audit_event
from app.models import AuditEventModel
from app.schemas import (
    AuditChannel,
    AuditEventOut,
    AuditEventType,
    AuditLogListResponse,
    AuditOutcome,
)

logger = logging.getLogger("guard.audit")

MAX_AUDIT_STR_LEN = 500
CORRUPTED_JSON_PLACEHOLDER: dict[str, Any] = {
    "_corrupted": True,
    "_error": "审计记录数据损坏，无法解析",
}


@dataclass
class AuditRetentionState:
    last_cleanup_at: datetime | None = None


def status_to_outcome(
    status_code: int | None,
    *,
    pending_approval: bool = False,
) -> AuditOutcome:
    if pending_approval:
        return "pending_approval"
    if status_code is None:
        return "error"
    if 200 <= status_code < 300:
        return "success"
    if status_code == 401:
        return "auth_failed"
    if status_code == 403:
        return "access_denied"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if 400 <= status_code < 500:
        return "invalid"
    return "error"


def sanitize_value(
    value: Any,
    redact_fields: frozenset[str],
    max_len: int = MAX_AUDIT_STR_LEN,
    max_key_len: int = MAX_AUDIT_KEY_LEN,
    max_dict_keys: int = MAX_AUDIT_DICT_KEYS,
) -> Any:
    return sanitize_json_value(
        value,
        redact_fields=redact_fields,
        max_len=max_len,
        max_key_len=max_key_len,
        max_dict_keys=max_dict_keys,
    )


def cleanup_expired_events(
    session: Session,
    *,
    now: datetime,
    retention_days: int,
) -> int:
    cutoff = to_utc_naive(now - timedelta(days=retention_days))
    stmt = delete(AuditEventModel).where(AuditEventModel.ts < cutoff)
    res = session.execute(stmt)
    return int(res.rowcount or 0)


def maybe_cleanup_expired_events(
    session: Session,
    *,
    now: datetime,
    settings: Settings,
    retention_state: AuditRetentionState | None,
) -> int:
    if retention_state is None:
        return 0
    if retention_state.last_cleanup_at is not None and (
        now - retention_state.last_cleanup_at < timedelta(hours=24)
    ):
        return 0
    deleted = cleanup_expired_events(
        session,
        now=now,
        retention_days=settings.audit_retention_days,
    )
    retention_state.last_cleanup_at = now
    return deleted


def record_event(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    request_id: str,
    event_type: AuditEventType,
    channel: AuditChannel,
    actor_sub: str,
    actor_username: str | None,
    actor_name: str | None,
    actor_role: str,
    actor_regions: str,
    token_jti: str | None,
    tool: str,
    http_method: str | None,
    http_path: str | None,
    arguments: dict[str, Any],
    outcome: AuditOutcome,
    order_id: str | None = None,
    resource_region: str | None = None,
    status_code: int | None = None,
    error_code: str | None = None,
    duration_ms: int = 0,
    approval_id: str | None = None,
    on_behalf_of_sub: str | None = None,
    result: dict[str, Any] | None = None,
    retention_state: AuditRetentionState | None = None,
) -> AuditEventModel:
    maybe_cleanup_expired_events(
        session,
        now=now,
        settings=settings,
        retention_state=retention_state,
    )
    sanitized_args = sanitize_value(arguments, settings.audit_redact_field_set)
    sanitized_result = sanitize_value(result, frozenset()) if result is not None else None
    row = AuditEventModel(
        ts=to_utc_naive(now),
        request_id=request_id,
        event_type=event_type,
        channel=channel,
        actor_sub=actor_sub,
        actor_username=actor_username,
        actor_name=actor_name,
        actor_role=actor_role,
        actor_regions=actor_regions,
        token_jti=token_jti,
        tool=tool,
        http_method=http_method,
        http_path=http_path,
        arguments=json.dumps(sanitized_args, ensure_ascii=False, allow_nan=False),
        order_id=order_id,
        resource_region=resource_region,
        outcome=outcome,
        status_code=status_code,
        error_code=error_code,
        duration_ms=duration_ms,
        approval_id=approval_id,
        on_behalf_of_sub=on_behalf_of_sub,
        result=(
            json.dumps(sanitized_result, ensure_ascii=False, allow_nan=False)
            if sanitized_result is not None
            else None
        ),
    )
    session.add(row)
    session.flush()
    return row


def _load_audit_json_dict(raw: str, *, row_id: int, field: str) -> dict[str, Any]:
    try:
        parsed = loads_strict_json(raw)
        if not isinstance(parsed, dict):
            raise ValueError("audit json field must be a JSON object")
    except Exception as exc:
        logger.error(
            "audit_row_corrupted",
            extra={
                "fields": {
                    "event": "audit_row_corrupted",
                    "audit_id": row_id,
                    "field": field,
                    "error_type": type(exc).__name__,
                }
            },
        )
        return dict(CORRUPTED_JSON_PLACEHOLDER)
    return parsed


def to_audit_out(row: AuditEventModel, tz_name: str) -> AuditEventOut:
    return AuditEventOut(
        id=row.id,
        ts=format_iso(row.ts, tz_name) or "",
        request_id=row.request_id,
        event_type=cast(AuditEventType, row.event_type),
        channel=cast(AuditChannel, row.channel),
        actor_sub=row.actor_sub,
        actor_username=row.actor_username,
        actor_name=row.actor_name,
        actor_role=row.actor_role,
        actor_regions=row.actor_regions,
        token_jti=row.token_jti,
        tool=row.tool,
        http_method=row.http_method,
        http_path=row.http_path,
        arguments=_load_audit_json_dict(row.arguments, row_id=row.id, field="arguments"),
        order_id=row.order_id,
        resource_region=row.resource_region,
        outcome=cast(AuditOutcome, row.outcome),
        status_code=row.status_code,
        error_code=row.error_code,
        duration_ms=row.duration_ms,
        approval_id=row.approval_id,
        on_behalf_of_sub=row.on_behalf_of_sub,
        result=(
            _load_audit_json_dict(row.result, row_id=row.id, field="result")
            if row.result is not None
            else None
        ),
    )


def emit_audit_log(row: AuditEventModel, tz_name: str) -> None:
    out = to_audit_out(row, tz_name)
    log_audit_event(logger, out.model_dump(exclude_none=True))


def query_events(
    session: Session,
    *,
    principal: Principal,
    tz_name: str,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    actor: str | None = None,
    event_type: str | None = None,
    channel: str | None = None,
    tool: str | None = None,
    outcome: str | None = None,
    order_id: str | None = None,
    approval_id: str | None = None,
    request_id: str | None = None,
    after_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> AuditLogListResponse:
    regions_sorted = sorted(principal.regions)
    region_checks = [
        AuditEventModel.resource_region.in_(regions_sorted),
        *[
            ("," + AuditEventModel.actor_regions + ",").like(f"%,{region},%")
            for region in regions_sorted
        ],
    ]
    conditions: list[Any] = [or_(*region_checks)]

    if from_dt is not None:
        conditions.append(AuditEventModel.ts >= to_utc_naive(from_dt))
    if to_dt is not None:
        conditions.append(AuditEventModel.ts <= to_utc_naive(to_dt))
    if actor is not None:
        conditions.append(
            or_(
                AuditEventModel.actor_sub == actor,
                AuditEventModel.actor_username == actor,
            )
        )
    if event_type is not None:
        conditions.append(AuditEventModel.event_type == event_type)
    if channel is not None:
        conditions.append(AuditEventModel.channel == channel)
    if tool is not None:
        conditions.append(AuditEventModel.tool == tool)
    if outcome is not None:
        conditions.append(AuditEventModel.outcome == outcome)
    if order_id is not None:
        conditions.append(AuditEventModel.order_id == order_id)
    if approval_id is not None:
        conditions.append(AuditEventModel.approval_id == approval_id)
    if request_id is not None:
        conditions.append(AuditEventModel.request_id == request_id)
    if after_id is not None:
        conditions.append(AuditEventModel.id > after_id)

    where_clause = and_(*conditions)
    total = int(
        session.scalar(select(func.count()).select_from(AuditEventModel).where(where_clause)) or 0
    )
    rows = session.scalars(
        select(AuditEventModel)
        .where(where_clause)
        .order_by(AuditEventModel.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return AuditLogListResponse(
        items=[to_audit_out(row, tz_name) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


def get_approval_events(
    session: Session,
    *,
    approval_id: str,
    tz_name: str,
) -> list[AuditEventOut]:
    rows = session.scalars(
        select(AuditEventModel)
        .where(
            AuditEventModel.approval_id == approval_id,
            or_(
                and_(
                    AuditEventModel.event_type == "tool_call",
                    AuditEventModel.outcome == "pending_approval",
                ),
                AuditEventModel.event_type.like("approval_%"),
            ),
        )
        .order_by(AuditEventModel.id.asc())
    ).all()
    return [to_audit_out(row, tz_name) for row in rows]


def find_late_execution_conflict_approval_ids(
    session: Session,
    *,
    approval_ids: list[str],
) -> set[str]:
    if not approval_ids:
        return set()
    rows = session.scalars(
        select(AuditEventModel).where(
            AuditEventModel.approval_id.in_(approval_ids),
            AuditEventModel.event_type == "approval_execute",
            AuditEventModel.status_code >= 200,
            AuditEventModel.status_code < 300,
            AuditEventModel.result.is_not(None),
        )
    ).all()
    conflicted: set[str] = set()
    for row in rows:
        assert row.approval_id is not None
        assert row.result is not None
        parsed = _load_audit_json_dict(row.result, row_id=row.id, field="result")
        if parsed.get("stale") is True:
            conflicted.add(row.approval_id)
    return conflicted
