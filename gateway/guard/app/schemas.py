from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Region = Literal["east", "north", "south"]
ReasonCategory = Literal[
    "quality_issue",
    "no_reason",
    "wrong_item",
    "damaged_in_transit",
    "not_received",
    "other",
]
ApprovalStatus = Literal[
    "pending",
    "executing",
    "executed",
    "execution_failed",
    "rejected",
    "expired",
]
AuditEventType = Literal[
    "tool_call",
    "approval_approve",
    "approval_reject",
    "approval_execute",
    "approval_expire",
]
AuditChannel = Literal["agent", "console", "system"]
AuditOutcome = Literal[
    "success",
    "pending_approval",
    "access_denied",
    "not_found",
    "conflict",
    "invalid",
    "auth_failed",
    "error",
]

APPROVAL_STATUSES: tuple[str, ...] = (
    "pending",
    "executing",
    "executed",
    "execution_failed",
    "rejected",
    "expired",
)


MAX_REFUND_AMOUNT = 10_000_000.0


def is_invalid_path_segment(value: str) -> bool:
    return value in (".", "..") or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


class RefundArgs(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=32, description="订单号，如 A1023")
    amount: float = Field(
        ...,
        gt=0,
        le=MAX_REFUND_AMOUNT,
        description="退款金额（元），须大于 0 且最多两位小数，不得超过订单可退金额",
    )
    reason_category: ReasonCategory = Field(..., description="退款原因类别")
    reason: str = Field(..., min_length=1, max_length=200, description="退款说明，写明判断依据")
    ticket_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        description="关联的售后工单号（可选）；提供后退款成功时该工单置为已处理",
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="幂等键（可选，8–64 位字母数字下划线或连字符）；相同幂等键重复提交不会重复退款",
    )

    @field_validator("order_id")
    @classmethod
    def _validate_order_id_path_segment(cls, value: str) -> str:
        if is_invalid_path_segment(value):
            raise ValueError("订单号不能为 '.'、'..' 或包含控制字符")
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def _reject_bool_amount(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("退款金额必须为数字")
        return value

    @field_validator("amount")
    @classmethod
    def _validate_amount_precision(cls, value: float) -> float:
        try:
            dec = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("退款金额格式无效") from exc
        if not dec.is_finite() or dec <= 0:
            raise ValueError("退款金额必须大于 0")
        exponent = dec.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError("退款金额最多保留两位小数")
        if int(dec * 100) <= 0:
            raise ValueError("退款金额必须大于 0")
        return value

    @field_validator("reason")
    @classmethod
    def _validate_reason_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("退款说明不能为空白")
        return stripped

    @property
    def amount_cents(self) -> int:
        return int(Decimal(str(self.amount)) * 100)

    def business_arguments(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "order_id": self.order_id,
            "amount": self.amount,
            "reason_category": self.reason_category,
            "reason": self.reason,
        }
        if self.ticket_id is not None:
            payload["ticket_id"] = self.ticket_id
        return payload


class Approval(BaseModel):
    approval_id: str = Field(..., description="审批单号，如 AP000001")
    status: ApprovalStatus = Field(..., description="审批状态")
    message: str = Field(..., description="当前状态的中文说明，可直接转述给用户")
    tool: str = Field(..., description="申请对应的写操作，当前为 create_refund")
    order_id: str = Field(..., description="订单号")
    region: Region = Field(..., description="订单所属区域")
    amount: float = Field(..., description="申请退款金额（元）")
    reason_category: ReasonCategory = Field(..., description="退款原因类别")
    reason: str = Field(..., description="退款说明")
    ticket_id: str | None = Field(default=None, description="关联的售后工单号")
    applicant_sub: str = Field(..., description="申请人的用户标识")
    applicant_name: str | None = Field(default=None, description="申请人姓名")
    submitted_at: str = Field(..., description="提交时间")
    expires_at: str = Field(..., description="审批截止时间")
    decided_by_sub: str | None = Field(default=None, description="审批主管的用户标识")
    decided_by_name: str | None = Field(default=None, description="审批主管姓名")
    decided_at: str | None = Field(default=None, description="审批时间")
    decision_comment: str | None = Field(default=None, description="审批意见")
    refund_id: str | None = Field(default=None, description="执行成功后的退款单号")
    executed_at: str | None = Field(default=None, description="执行成功的时间")
    failure_code: str | None = Field(default=None, description="执行失败时订单系统的错误码")
    failure_message: str | None = Field(default=None, description="执行失败时订单系统的说明")


class ApprovalListResponse(BaseModel):
    items: list[Approval]
    total: int
    page: int
    page_size: int


class AuditEventOut(BaseModel):
    id: int
    ts: str
    request_id: str
    event_type: AuditEventType
    channel: AuditChannel
    actor_sub: str
    actor_username: str | None = None
    actor_name: str | None = None
    actor_role: str
    actor_regions: str
    token_jti: str | None = None
    tool: str
    http_method: str | None = None
    http_path: str | None = None
    arguments: dict[str, Any]
    order_id: str | None = None
    resource_region: str | None = None
    outcome: AuditOutcome
    status_code: int | None = None
    error_code: str | None = None
    duration_ms: int
    approval_id: str | None = None
    on_behalf_of_sub: str | None = None
    result: dict[str, Any] | None = None


class AuditLogListResponse(BaseModel):
    items: list[AuditEventOut]
    total: int
    page: int
    page_size: int


class ConsoleApprovalSummary(Approval):
    applicant_username: str | None = None
    applicant_role: str
    has_late_execution_conflict: bool = False


class ConsoleApprovalListResponse(BaseModel):
    items: list[ConsoleApprovalSummary]
    total: int
    page: int
    page_size: int


class ConsoleApprovalDetail(ConsoleApprovalSummary):
    applicant_regions: str | None = None
    order_snapshot: dict[str, Any]
    attempts: int
    failure_status: int | None = None
    late_executed_refund_id: str | None = None
    late_execution_notice: str | None = None
    events: list[AuditEventOut]


class ApproveRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=200)

    @field_validator("comment")
    @classmethod
    def _normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped if stripped else None


class RejectRequest(BaseModel):
    comment: str = Field(..., min_length=1, max_length=200)

    @field_validator("comment")
    @classmethod
    def _validate_comment_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("拒绝意见不能为空")
        return stripped


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class AgentListApprovalsQuery(BaseModel):
    order_id: str | None = None
    status: ApprovalStatus | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)
