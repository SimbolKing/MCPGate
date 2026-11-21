from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import cast

from sqlalchemy.orm import Session

from app import repository
from app.auth import Principal
from app.clock import days_since, format_iso
from app.errors import ConflictError
from app.log import log_refund_created
from app.models import Order, Refund
from app.schemas import (
    AfterSaleTicketOut,
    CreateRefundRequest,
    CustomerLevel,
    CustomerSummary,
    OrderDetail,
    OrderItemOut,
    OrderListResponse,
    OrderRefundSummary,
    OrderStatus,
    OrderSummary,
    ProductCategory,
    ReasonCategory,
    RefundListResponse,
    RefundOut,
    RefundProgress,
    RefundStatus,
    Region,
    Role,
    TicketStatus,
    TicketType,
)

logger = logging.getLogger("mock_api.services")

_refund_lock = threading.Lock()


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def cents_to_yuan(cents: int) -> float:
    return round(cents / 100, 2)


def compute_refund_status(refunded_cents: int, total_cents: int) -> RefundProgress:
    if refunded_cents <= 0:
        return "none"
    if refunded_cents >= total_cents:
        return "full"
    return "partial"


def _to_refund_out(refund: Refund, tz_name: str) -> RefundOut:
    return RefundOut(
        refund_id=refund.refund_id,
        order_id=refund.order_id,
        region=cast(Region, refund.region),
        ticket_id=refund.ticket_id,
        amount=cents_to_yuan(refund.amount_cents),
        reason_category=cast(ReasonCategory, refund.reason_category),
        reason=refund.reason,
        status=cast(RefundStatus, refund.status),
        created_by=refund.created_by,
        created_by_role=cast(Role, refund.created_by_role),
        created_at=format_iso(refund.created_at, tz_name) or "",
        completed_at=format_iso(refund.completed_at, tz_name),
    )


def _to_order_detail(order: Order, *, now: datetime, tz_name: str) -> OrderDetail:
    processing_cents = sum(r.amount_cents for r in order.refunds if r.status == "processing")
    refundable_cents = max(
        0, order.total_amount_cents - order.refunded_amount_cents - processing_cents
    )
    delivered_days = (
        days_since(order.delivered_at, now, tz_name) if order.delivered_at is not None else None
    )

    return OrderDetail(
        order_id=order.order_id,
        region=cast(Region, order.region),
        status=cast(OrderStatus, order.status),
        created_at=format_iso(order.created_at, tz_name) or "",
        paid_at=format_iso(order.paid_at, tz_name),
        shipped_at=format_iso(order.shipped_at, tz_name),
        delivered_at=format_iso(order.delivered_at, tz_name),
        days_since_delivery=delivered_days,
        total_amount=cents_to_yuan(order.total_amount_cents),
        refunded_amount=cents_to_yuan(order.refunded_amount_cents),
        refundable_amount=cents_to_yuan(refundable_cents),
        refund_status=compute_refund_status(order.refunded_amount_cents, order.total_amount_cents),
        customer=CustomerSummary(
            customer_id=order.customer.customer_id,
            name=order.customer.name,
            phone_masked=mask_phone(order.customer.phone),
            level=cast(CustomerLevel, order.customer.level),
        ),
        items=[
            OrderItemOut(
                sku=item.sku,
                product_name=item.product_name,
                category=cast(ProductCategory, item.category),
                quantity=item.quantity,
                unit_price=cents_to_yuan(item.unit_price_cents),
            )
            for item in order.items
        ],
        after_sale_tickets=[
            AfterSaleTicketOut(
                ticket_id=ticket.ticket_id,
                type=cast(TicketType, ticket.type),
                reason_category=cast(ReasonCategory, ticket.reason_category),
                description=ticket.description,
                requested_amount=(
                    cents_to_yuan(ticket.requested_amount_cents)
                    if ticket.requested_amount_cents is not None
                    else None
                ),
                status=cast(TicketStatus, ticket.status),
                created_at=format_iso(ticket.created_at, tz_name) or "",
                resolved_at=format_iso(ticket.resolved_at, tz_name),
            )
            for ticket in order.after_sale_tickets
        ],
        refunds=[
            OrderRefundSummary(
                refund_id=r.refund_id,
                amount=cents_to_yuan(r.amount_cents),
                reason_category=cast(ReasonCategory, r.reason_category),
                status=cast(RefundStatus, r.status),
                created_at=format_iso(r.created_at, tz_name) or "",
            )
            for r in order.refunds
        ],
    )


def get_order_detail(
    session: Session,
    principal: Principal,
    order_id: str,
    *,
    now: datetime,
    tz_name: str,
) -> OrderDetail:
    order = repository.get_order(session, principal, order_id)
    return _to_order_detail(order, now=now, tz_name=tz_name)


def list_order_summaries(
    session: Session,
    principal: Principal,
    *,
    customer_id: str | None,
    status: str | None,
    has_open_ticket: bool | None,
    page: int,
    page_size: int,
    now: datetime,
    tz_name: str,
) -> OrderListResponse:
    orders, total = repository.list_orders(
        session,
        principal,
        customer_id=customer_id,
        status=status,
        has_open_ticket=has_open_ticket,
        page=page,
        page_size=page_size,
    )
    items = [
        OrderSummary(
            order_id=order.order_id,
            region=cast(Region, order.region),
            status=cast(OrderStatus, order.status),
            created_at=format_iso(order.created_at, tz_name) or "",
            delivered_at=format_iso(order.delivered_at, tz_name),
            days_since_delivery=(
                days_since(order.delivered_at, now, tz_name)
                if order.delivered_at is not None
                else None
            ),
            total_amount=cents_to_yuan(order.total_amount_cents),
            refund_status=compute_refund_status(
                order.refunded_amount_cents, order.total_amount_cents
            ),
            customer_id=order.customer.customer_id,
            customer_name=order.customer.name,
            has_open_ticket=any(t.status == "open" for t in order.after_sale_tickets),
        )
        for order in orders
    ]
    return OrderListResponse(items=items, total=total, page=page, page_size=page_size)


def get_refund_detail(
    session: Session,
    principal: Principal,
    refund_id: str,
    *,
    tz_name: str,
) -> RefundOut:
    refund = repository.get_refund(session, principal, refund_id)
    return _to_refund_out(refund, tz_name)


def list_refund_records(
    session: Session,
    principal: Principal,
    *,
    order_id: str | None,
    status: str | None,
    page: int,
    page_size: int,
    tz_name: str,
) -> RefundListResponse:
    refunds, total = repository.list_refunds(
        session,
        principal,
        order_id=order_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return RefundListResponse(
        items=[_to_refund_out(r, tz_name) for r in refunds],
        total=total,
        page=page,
        page_size=page_size,
    )


def create_refund(
    session: Session,
    principal: Principal,
    req: CreateRefundRequest,
    *,
    now: datetime,
    tz_name: str,
    request_id: str,
) -> RefundOut:
    amount_cents = req.amount_cents

    with _refund_lock:
        session.expire_all()

        # 1. 加载订单并做行级权限判定（404 / 403）
        order = repository.get_order(session, principal, req.order_id)

        # 2. 幂等键检查
        if req.idempotency_key is not None:
            existing = repository.find_refund_by_idempotency_key(
                session, principal, req.idempotency_key
            )
            if existing is not None:
                if existing.order_id == order.order_id and existing.amount_cents == amount_cents:
                    return _to_refund_out(existing, tz_name)
                raise ConflictError(
                    "idempotency_conflict",
                    "该幂等键已被另一笔参数不同的退款使用。",
                )

        # 3. 订单状态检查
        if order.status in ("pending_payment", "cancelled"):
            raise ConflictError(
                "order_not_refundable",
                f"订单 {order.order_id} 当前状态为 {order.status}，不允许发起退款。",
            )

        # 4. 处理中退款检查
        if any(r.status == "processing" for r in order.refunds):
            raise ConflictError(
                "refund_in_progress",
                f"订单 {order.order_id} 已有处理中的退款，请等待处理完成。",
            )

        # 5. 可退金额上限检查
        refundable_cents = order.total_amount_cents - order.refunded_amount_cents
        if amount_cents > refundable_cents:
            refundable_yuan = cents_to_yuan(refundable_cents)
            raise ConflictError(
                "refund_amount_exceeded",
                f"退款金额超过订单 {order.order_id} 的可退金额"
                f"（当前可退 {refundable_yuan:.2f} 元）。",
            )

        # 6. 关联工单检查
        ticket = None
        if req.ticket_id is not None:
            ticket = repository.get_ticket_for_order(
                session, principal, order.order_id, req.ticket_id
            )
            if ticket is None or ticket.status != "open":
                raise ConflictError(
                    "ticket_not_applicable",
                    f"售后工单 {req.ticket_id} 不存在、"
                    f"不属于订单 {order.order_id} 或不是待处理状态。",
                )

        # 7. 事务内原子写入
        refund_id = repository.next_refund_id(session, principal)
        refund = Refund(
            refund_id=refund_id,
            order_id=order.order_id,
            region=order.region,
            ticket_id=req.ticket_id,
            amount_cents=amount_cents,
            reason_category=req.reason_category,
            reason=req.reason,
            status="succeeded",
            idempotency_key=req.idempotency_key,
            created_by=principal.sub,
            created_by_role=principal.role,
            created_at=now,
            completed_at=now,
        )
        repository.add_refund(session, principal, refund)
        order.refunded_amount_cents += amount_cents
        if ticket is not None:
            ticket.status = "resolved"
            ticket.resolved_at = now

        session.commit()
        session.refresh(refund)

    log_refund_created(
        logger,
        request_id=request_id,
        refund_id=refund.refund_id,
        order_id=refund.order_id,
        amount=cents_to_yuan(refund.amount_cents),
        created_by=refund.created_by,
        created_by_role=refund.created_by_role,
    )
    return _to_refund_out(refund, tz_name)
