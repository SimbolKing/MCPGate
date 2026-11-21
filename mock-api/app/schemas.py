from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Region = Literal["east", "north", "south"]
Role = Literal["agent", "supervisor"]
CustomerLevel = Literal["regular", "vip"]
OrderStatus = Literal[
    "pending_payment",
    "paid",
    "shipped",
    "delivered",
    "completed",
    "cancelled",
]
RefundProgress = Literal["none", "partial", "full"]
ProductCategory = Literal[
    "electronics",
    "clothing",
    "home",
    "fresh_food",
    "custom_made",
]
TicketType = Literal["return_refund", "refund_only"]
ReasonCategory = Literal[
    "quality_issue",
    "no_reason",
    "wrong_item",
    "damaged_in_transit",
    "not_received",
    "other",
]
TicketStatus = Literal["open", "resolved", "rejected"]
RefundStatus = Literal["processing", "succeeded", "failed"]


class ErrorResponse(BaseModel):
    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="可直接转述给用户的中文说明")
    request_id: str = Field(..., description="请求追踪 ID")


class ErrorDetail(BaseModel):
    field: str = Field(..., description="出错字段")
    message: str = Field(..., description="字段校验失败说明")


class ValidationErrorResponse(BaseModel):
    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="中文错误说明")
    request_id: str = Field(..., description="请求追踪 ID")
    details: list[ErrorDetail] = Field(..., description="字段级校验错误明细")


class CustomerSummary(BaseModel):
    customer_id: str = Field(..., description="客户编号")
    name: str = Field(..., description="客户姓名")
    phone_masked: str = Field(..., description="脱敏手机号")
    level: CustomerLevel = Field(..., description="客户等级（regular 普通 / vip 会员）")


class OrderItemOut(BaseModel):
    sku: str = Field(..., description="商品编码")
    product_name: str = Field(..., description="商品名称")
    category: ProductCategory = Field(..., description="商品类目")
    quantity: int = Field(..., description="商品数量")
    unit_price: float = Field(..., description="成交单价（元）")


class AfterSaleTicketOut(BaseModel):
    ticket_id: str = Field(..., description="售后工单号")
    type: TicketType = Field(..., description="售后申请类型")
    reason_category: ReasonCategory = Field(..., description="售后申请原因类别")
    description: str = Field(..., description="客户描述")
    requested_amount: float | None = Field(default=None, description="客户申请金额（元）")
    status: TicketStatus = Field(..., description="工单状态")
    created_at: str = Field(..., description="工单提交时间")
    resolved_at: str | None = Field(default=None, description="工单处理完成时间")


class OrderRefundSummary(BaseModel):
    refund_id: str = Field(..., description="退款单号")
    amount: float = Field(..., description="退款金额（元）")
    reason_category: ReasonCategory = Field(..., description="退款原因类别")
    status: RefundStatus = Field(..., description="退款状态")
    created_at: str = Field(..., description="退款创建时间")


class OrderDetail(BaseModel):
    order_id: str = Field(..., description="订单号")
    region: Region = Field(..., description="订单所属区域")
    status: OrderStatus = Field(..., description="订单状态")
    created_at: str = Field(..., description="下单时间")
    paid_at: str | None = Field(default=None, description="支付时间")
    shipped_at: str | None = Field(default=None, description="发货时间")
    delivered_at: str | None = Field(default=None, description="签收时间")
    days_since_delivery: int | None = Field(
        default=None, description="已签收的自然日天数，签收当天为 0"
    )
    total_amount: float = Field(..., description="订单实付金额（元）")
    refunded_amount: float = Field(..., description="已成功退款累计金额（元）")
    refundable_amount: float = Field(..., description="当前还可退金额（元）")
    refund_status: RefundProgress = Field(..., description="退款进度（none / partial / full）")
    customer: CustomerSummary = Field(..., description="客户信息")
    items: list[OrderItemOut] = Field(..., description="商品明细")
    after_sale_tickets: list[AfterSaleTicketOut] = Field(..., description="售后工单列表")
    refunds: list[OrderRefundSummary] = Field(..., description="订单关联退款单列表")


class OrderSummary(BaseModel):
    order_id: str = Field(..., description="订单号")
    region: Region = Field(..., description="订单所属区域")
    status: OrderStatus = Field(..., description="订单状态")
    created_at: str = Field(..., description="下单时间")
    delivered_at: str | None = Field(default=None, description="签收时间")
    days_since_delivery: int | None = Field(default=None, description="已签收的自然日天数")
    total_amount: float = Field(..., description="订单实付金额（元）")
    refund_status: RefundProgress = Field(..., description="退款进度")
    customer_id: str = Field(..., description="客户编号")
    customer_name: str = Field(..., description="客户姓名")
    has_open_ticket: bool = Field(..., description="是否有待处理的售后工单")


class OrderListResponse(BaseModel):
    items: list[OrderSummary] = Field(..., description="订单摘要列表")
    total: int = Field(..., description="符合条件且可见的订单总数")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页条数")


class RefundOut(BaseModel):
    refund_id: str = Field(..., description="退款单号")
    order_id: str = Field(..., description="所属订单号")
    region: Region = Field(..., description="所属区域")
    ticket_id: str | None = Field(default=None, description="关联的售后工单号")
    amount: float = Field(..., description="退款金额（元）")
    reason_category: ReasonCategory = Field(..., description="退款原因类别")
    reason: str = Field(..., description="退款说明")
    status: RefundStatus = Field(..., description="退款状态")
    created_by: str = Field(..., description="操作人的用户标识")
    created_by_role: Role = Field(..., description="操作人角色")
    created_at: str = Field(..., description="创建时间")
    completed_at: str | None = Field(default=None, description="完成时间")


class RefundListResponse(BaseModel):
    items: list[RefundOut] = Field(..., description="退款单列表")
    total: int = Field(..., description="符合条件且可见的退款单总数")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页条数")


class CreateRefundRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=32, description="订单号，如 A1023")
    amount: float = Field(
        ...,
        gt=0,
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
