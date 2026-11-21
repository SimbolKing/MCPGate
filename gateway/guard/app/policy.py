from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas import RefundArgs


@dataclass(frozen=True)
class WritePolicy:
    tool: str
    args_model: type[RefundArgs]
    precheck_path: str
    execute_method: str
    execute_path: str


def build_order_snapshot(order_data: dict[str, Any]) -> dict[str, Any]:
    customer_raw = order_data.get("customer") or {}
    snapshot: dict[str, Any] = {
        "status": order_data.get("status"),
        "total_amount": order_data.get("total_amount"),
        "refunded_amount": order_data.get("refunded_amount"),
        "refundable_amount": order_data.get("refundable_amount"),
        "customer": {
            "name": customer_raw.get("name"),
            "level": customer_raw.get("level"),
        },
        "items": [
            {
                "product_name": item.get("product_name"),
                "category": item.get("category"),
                "quantity": item.get("quantity"),
                "unit_price": item.get("unit_price"),
            }
            for item in (order_data.get("items") or [])
        ],
        "after_sale_tickets": [
            {
                "ticket_id": ticket.get("ticket_id"),
                "type": ticket.get("type"),
                "reason_category": ticket.get("reason_category"),
                "description": ticket.get("description"),
                "status": ticket.get("status"),
            }
            for ticket in (order_data.get("after_sale_tickets") or [])
        ],
    }
    if order_data.get("delivered_at") is not None:
        snapshot["delivered_at"] = order_data["delivered_at"]
    if order_data.get("days_since_delivery") is not None:
        snapshot["days_since_delivery"] = order_data["days_since_delivery"]
    return snapshot


WRITE_POLICIES: dict[str, WritePolicy] = {
    "create_refund": WritePolicy(
        tool="create_refund",
        args_model=RefundArgs,
        precheck_path="/api/v1/orders/{order_id}",
        execute_method="POST",
        execute_path="/api/v1/refunds",
    ),
}


def get_write_policy(tool_name: str) -> WritePolicy | None:
    return WRITE_POLICIES.get(tool_name)
