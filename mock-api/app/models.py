from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("level IN ('regular', 'vip')", name="ck_customers_level"),)

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("region IN ('east', 'north', 'south')", name="ck_orders_region"),
        CheckConstraint(
            "status IN ('pending_payment', 'paid', 'shipped', "
            "'delivered', 'completed', 'cancelled')",
            name="ck_orders_status",
        ),
        CheckConstraint("total_amount_cents > 0", name="ck_orders_total_positive"),
        CheckConstraint(
            "refunded_amount_cents >= 0 AND refunded_amount_cents <= total_amount_cents",
            name="ck_orders_refunded_range",
        ),
    )

    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", order_by="OrderItem.id", cascade="all, delete-orphan"
    )
    after_sale_tickets: Mapped[list[AfterSaleTicket]] = relationship(
        back_populates="order",
        order_by="AfterSaleTicket.created_at, AfterSaleTicket.ticket_id",
        cascade="all, delete-orphan",
    )
    refunds: Mapped[list[Refund]] = relationship(
        back_populates="order",
        order_by="Refund.created_at, Refund.refund_id",
        cascade="all, delete-orphan",
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint(
            "category IN ('electronics', 'clothing', 'home', 'fresh_food', 'custom_made')",
            name="ck_order_items_category",
        ),
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint("unit_price_cents >= 0", name="ck_order_items_unit_price"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        Text, ForeignKey("orders.order_id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class AfterSaleTicket(Base):
    __tablename__ = "after_sale_tickets"
    __table_args__ = (
        CheckConstraint("region IN ('east', 'north', 'south')", name="ck_tickets_region"),
        CheckConstraint("type IN ('return_refund', 'refund_only')", name="ck_tickets_type"),
        CheckConstraint(
            "reason_category IN ('quality_issue', 'no_reason', 'wrong_item', "
            "'damaged_in_transit', 'not_received', 'other')",
            name="ck_tickets_reason_category",
        ),
        CheckConstraint(
            "requested_amount_cents IS NULL OR requested_amount_cents > 0",
            name="ck_tickets_requested_amount",
        ),
        CheckConstraint("status IN ('open', 'resolved', 'rejected')", name="ck_tickets_status"),
    )

    ticket_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_id: Mapped[str] = mapped_column(
        Text, ForeignKey("orders.order_id"), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    reason_category: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requested_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    order: Mapped[Order] = relationship(back_populates="after_sale_tickets")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("region IN ('east', 'north', 'south')", name="ck_refunds_region"),
        CheckConstraint("amount_cents > 0", name="ck_refunds_amount_positive"),
        CheckConstraint(
            "reason_category IN ('quality_issue', 'no_reason', 'wrong_item', "
            "'damaged_in_transit', 'not_received', 'other')",
            name="ck_refunds_reason_category",
        ),
        CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')", name="ck_refunds_status"
        ),
        CheckConstraint(
            "created_by_role IN ('agent', 'supervisor')", name="ck_refunds_created_by_role"
        ),
    )

    refund_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_id: Mapped[str] = mapped_column(
        Text, ForeignKey("orders.order_id"), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    ticket_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("after_sale_tickets.ticket_id"), nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_category: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    order: Mapped[Order] = relationship(back_populates="refunds")
