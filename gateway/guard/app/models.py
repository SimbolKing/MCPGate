from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApprovalModel(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'executing', 'executed', "
            "'execution_failed', 'rejected', 'expired')",
            name="ck_approvals_status",
        ),
        CheckConstraint("amount_cents > 0", name="ck_approvals_amount_positive"),
        Index(
            "uq_approvals_active_order_id",
            "order_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'executing')"),
            postgresql_where=text("status IN ('pending', 'executing')"),
        ),
    )

    approval_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    region: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments: Mapped[str] = mapped_column(Text, nullable=False)
    order_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    client_idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    execution_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    applicant_sub: Mapped[str] = mapped_column(Text, nullable=False)
    applicant_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicant_role: Mapped[str] = mapped_column(Text, nullable=False)
    applicant_regions: Mapped[str | None] = mapped_column(Text, nullable=True)

    submit_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    decided_by_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    executed_by_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refund_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditEventModel(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('tool_call', 'approval_approve', 'approval_reject', "
            "'approval_execute', 'approval_expire')",
            name="ck_audit_events_type",
        ),
        CheckConstraint(
            "channel IN ('agent', 'console', 'system')",
            name="ck_audit_events_channel",
        ),
        CheckConstraint(
            "outcome IN ('success', 'pending_approval', 'access_denied', 'not_found', "
            "'conflict', 'invalid', 'auth_failed', 'error')",
            name="ck_audit_events_outcome",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    actor_sub: Mapped[str] = mapped_column(Text, nullable=False)
    actor_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_role: Mapped[str] = mapped_column(Text, nullable=False)
    actor_regions: Mapped[str] = mapped_column(Text, nullable=False)
    token_jti: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    http_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    arguments: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    resource_region: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approval_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    on_behalf_of_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
