from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.auth import Principal
from app.errors import AccessDenied, NotFound
from app.models import AfterSaleTicket, Order, Refund


def get_order(session: Session, principal: Principal, order_id: str) -> Order:
    stmt = (
        select(Order)
        .options(
            joinedload(Order.customer),
            selectinload(Order.items),
            selectinload(Order.after_sale_tickets),
            selectinload(Order.refunds),
        )
        .where(Order.order_id == order_id)
    )
    order = session.scalars(stmt).unique().one_or_none()
    if order is None:
        raise NotFound("order_not_found", f"未找到订单 {order_id}。")
    if order.region not in principal.regions:
        raise AccessDenied(
            "order_access_denied",
            f"无权访问订单 {order_id}：该订单不在你的负责区域内。",
            resource_type="order",
            resource_id=order_id,
        )
    return order


def list_orders(
    session: Session,
    principal: Principal,
    *,
    customer_id: str | None = None,
    status: str | None = None,
    has_open_ticket: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Order], int]:
    stmt = select(Order).where(Order.region.in_(principal.regions))
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if has_open_ticket is not None:
        open_ticket_exists = exists(
            select(1).where(
                AfterSaleTicket.order_id == Order.order_id,
                AfterSaleTicket.status == "open",
            )
        )
        stmt = stmt.where(open_ticket_exists if has_open_ticket else ~open_ticket_exists)

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    offset = (page - 1) * page_size
    if offset >= total:
        return [], total
    paged_stmt = (
        stmt.options(
            joinedload(Order.customer),
            selectinload(Order.after_sale_tickets),
        )
        .order_by(Order.created_at.desc(), Order.order_id.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = list(session.scalars(paged_stmt).unique().all())
    return items, total


def get_refund(session: Session, principal: Principal, refund_id: str) -> Refund:
    refund = session.get(Refund, refund_id)
    if refund is None:
        raise NotFound("refund_not_found", f"未找到退款单 {refund_id}。")
    if refund.region not in principal.regions:
        raise AccessDenied(
            "refund_access_denied",
            f"无权访问退款单 {refund_id}：该退款单不在你的负责区域内。",
            resource_type="refund",
            resource_id=refund_id,
        )
    return refund


def list_refunds(
    session: Session,
    principal: Principal,
    *,
    order_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Refund], int]:
    stmt = select(Refund).where(Refund.region.in_(principal.regions))
    if order_id is not None:
        stmt = stmt.where(Refund.order_id == order_id)
    if status is not None:
        stmt = stmt.where(Refund.status == status)

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    offset = (page - 1) * page_size
    if offset >= total:
        return [], total
    paged_stmt = (
        stmt.order_by(Refund.created_at.desc(), Refund.refund_id.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = list(session.scalars(paged_stmt).all())
    return items, total


def find_refund_by_idempotency_key(
    session: Session,
    principal: Principal,
    idempotency_key: str,
) -> Refund | None:
    _ = principal
    stmt = select(Refund).where(Refund.idempotency_key == idempotency_key)
    return session.scalars(stmt).one_or_none()


def get_ticket_for_order(
    session: Session,
    principal: Principal,
    order_id: str,
    ticket_id: str,
) -> AfterSaleTicket | None:
    stmt = select(AfterSaleTicket).where(
        AfterSaleTicket.ticket_id == ticket_id,
        AfterSaleTicket.order_id == order_id,
        AfterSaleTicket.region.in_(principal.regions),
    )
    return session.scalars(stmt).one_or_none()


def next_refund_id(session: Session, principal: Principal) -> str:
    _ = principal
    existing_ids = session.scalars(select(Refund.refund_id)).all()
    max_num = max((int(rid[1:]) for rid in existing_ids), default=0)
    return f"R{max_num + 1:06d}"


def add_refund(session: Session, principal: Principal, refund: Refund) -> None:
    if refund.region not in principal.regions:
        raise AccessDenied(
            "order_access_denied",
            f"无权操作订单 {refund.order_id}：该订单不在你的负责区域内。",
            resource_type="order",
            resource_id=refund.order_id,
        )
    session.add(refund)
