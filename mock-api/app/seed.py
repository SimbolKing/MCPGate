from __future__ import annotations

import json
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.clock import utc_now
from app.config import Settings
from app.db import create_db_engine, create_session_factory, init_db, reset_db
from app.models import AfterSaleTicket, Customer, Order, OrderItem, Refund

VALID_REGIONS = {"east", "north", "south"}
VALID_CUSTOMER_LEVELS = {"regular", "vip"}
VALID_ORDER_STATUSES = {
    "pending_payment",
    "paid",
    "shipped",
    "delivered",
    "completed",
    "cancelled",
}
VALID_CATEGORIES = {"electronics", "clothing", "home", "fresh_food", "custom_made"}
VALID_TICKET_TYPES = {"return_refund", "refund_only"}
VALID_REASON_CATEGORIES = {
    "quality_issue",
    "no_reason",
    "wrong_item",
    "damaged_in_transit",
    "not_received",
    "other",
}
VALID_TICKET_STATUSES = {"open", "resolved", "rejected"}
VALID_REFUND_STATUSES = {"processing", "succeeded", "failed"}
VALID_ROLES = {"agent", "supervisor"}

ORDER_ID_RE = re.compile(r"^[A-Z][0-9]{4}$")
REFUND_ID_RE = re.compile(r"^R[0-9]{6}$")


class SeedValidationError(ValueError):
    """种子数据校验失败。"""


def _yuan_to_cents(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SeedValidationError(f"{field_name} 必须为数字")
    dec = Decimal(str(value))
    if not dec.is_finite():
        raise SeedValidationError(f"{field_name} 必须为有限数字")
    exponent = dec.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise SeedValidationError(f"{field_name} 最多保留两位小数")
    return int(dec * 100)


def _anchor_time(
    days_ago: int,
    at: time,
    *,
    anchor_date: datetime,
    tz: ZoneInfo,
    field_name: str,
) -> datetime:
    if isinstance(days_ago, bool) or not isinstance(days_ago, int) or days_ago < 1:
        raise SeedValidationError(f"{field_name} 必须为 >= 1 的整数，当前值：{days_ago!r}")
    local_day = anchor_date.astimezone(tz).date() - timedelta(days=days_ago)
    local_dt = datetime.combine(local_day, at, tzinfo=tz)
    return local_dt.astimezone(UTC)


def load_seed_data(seed_path: Path) -> dict[str, Any]:
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "customers" not in data or "orders" not in data:
        raise SeedValidationError("seed.json 必须包含 customers 与 orders 字段")
    return data


def seed_session(
    session: Session,
    seed_path: Path,
    *,
    now: datetime | None = None,
    tz_name: str = "Asia/Shanghai",
) -> None:
    data = load_seed_data(seed_path)
    anchor = now or utc_now()
    tz = ZoneInfo(tz_name)

    customer_ids: set[str] = set()
    for raw_c in data["customers"]:
        cid = raw_c["customer_id"]
        if cid in customer_ids:
            raise SeedValidationError(f"重复的 customer_id: {cid}")
        if raw_c["level"] not in VALID_CUSTOMER_LEVELS:
            raise SeedValidationError(f"客户 {cid} 的 level 无效: {raw_c['level']}")
        customer_ids.add(cid)
        session.add(
            Customer(
                customer_id=cid,
                name=raw_c["name"],
                phone=raw_c["phone"],
                level=raw_c["level"],
                created_at=_anchor_time(
                    raw_c["created_days_ago"],
                    time(9, 0, 0),
                    anchor_date=anchor,
                    tz=tz,
                    field_name=f"customer.{cid}.created_days_ago",
                ),
            )
        )

    order_ids: set[str] = set()
    ticket_ids: set[str] = set()
    refund_ids: set[str] = set()

    for raw_o in data["orders"]:
        oid = raw_o["order_id"]
        if not ORDER_ID_RE.match(oid):
            raise SeedValidationError(f"订单号格式不合法: {oid}")
        if oid in order_ids:
            raise SeedValidationError(f"重复的 order_id: {oid}")
        order_ids.add(oid)

        cid = raw_o["customer_id"]
        if cid not in customer_ids:
            raise SeedValidationError(f"订单 {oid} 引用了不存在的客户: {cid}")

        region = raw_o["region"]
        if region not in VALID_REGIONS:
            raise SeedValidationError(f"订单 {oid} 的 region 无效: {region}")

        status = raw_o["status"]
        if status not in VALID_ORDER_STATUSES:
            raise SeedValidationError(f"订单 {oid} 的 status 无效: {status}")

        total_cents = _yuan_to_cents(raw_o["total_amount"], f"order.{oid}.total_amount")
        if total_cents <= 0:
            raise SeedValidationError(f"订单 {oid} 的实付金额必须大于 0")

        raw_items = raw_o.get("items", [])
        if not raw_items:
            raise SeedValidationError(f"订单 {oid} 缺少商品明细")

        items_sum_cents = 0
        orm_items: list[OrderItem] = []
        for idx, raw_item in enumerate(raw_items):
            if raw_item["category"] not in VALID_CATEGORIES:
                raise SeedValidationError(
                    f"订单 {oid} 商品[{idx}] 类目无效: {raw_item['category']}"
                )
            qty = raw_item["quantity"]
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                raise SeedValidationError(f"订单 {oid} 商品[{idx}] 数量必须 > 0")
            unit_cents = _yuan_to_cents(
                raw_item["unit_price"], f"order.{oid}.items[{idx}].unit_price"
            )
            if unit_cents < 0:
                raise SeedValidationError(f"订单 {oid} 商品[{idx}] 单价不能为负")
            items_sum_cents += qty * unit_cents
            orm_items.append(
                OrderItem(
                    order_id=oid,
                    sku=raw_item["sku"],
                    product_name=raw_item["product_name"],
                    category=raw_item["category"],
                    quantity=qty,
                    unit_price_cents=unit_cents,
                )
            )

        if items_sum_cents != total_cents:
            raise SeedValidationError(
                f"订单 {oid} 明细合计 ({items_sum_cents}) 与实付金额 ({total_cents}) 不一致"
            )

        order_ticket_ids: set[str] = set()
        orm_tickets: list[AfterSaleTicket] = []
        for raw_t in raw_o.get("tickets", []):
            tid = raw_t["ticket_id"]
            if tid in ticket_ids:
                raise SeedValidationError(f"重复的 ticket_id: {tid}")
            ticket_ids.add(tid)
            order_ticket_ids.add(tid)

            if raw_t["type"] not in VALID_TICKET_TYPES:
                raise SeedValidationError(f"工单 {tid} 的 type 无效: {raw_t['type']}")
            if raw_t["reason_category"] not in VALID_REASON_CATEGORIES:
                raise SeedValidationError(
                    f"工单 {tid} 的 reason_category 无效: {raw_t['reason_category']}"
                )
            if raw_t["status"] not in VALID_TICKET_STATUSES:
                raise SeedValidationError(f"工单 {tid} 的 status 无效: {raw_t['status']}")

            req_cents: int | None = None
            if raw_t.get("requested_amount") is not None:
                req_cents = _yuan_to_cents(
                    raw_t["requested_amount"], f"ticket.{tid}.requested_amount"
                )
                if req_cents <= 0:
                    raise SeedValidationError(f"工单 {tid} 申请金额必须 > 0")

            resolved_at = (
                _anchor_time(
                    raw_t["resolved_days_ago"],
                    time(17, 0, 0),
                    anchor_date=anchor,
                    tz=tz,
                    field_name=f"ticket.{tid}.resolved_days_ago",
                )
                if raw_t.get("resolved_days_ago") is not None
                else None
            )
            orm_tickets.append(
                AfterSaleTicket(
                    ticket_id=tid,
                    order_id=oid,
                    region=region,
                    type=raw_t["type"],
                    reason_category=raw_t["reason_category"],
                    description=raw_t["description"],
                    requested_amount_cents=req_cents,
                    status=raw_t["status"],
                    created_at=_anchor_time(
                        raw_t["created_days_ago"],
                        time(16, 20, 0),
                        anchor_date=anchor,
                        tz=tz,
                        field_name=f"ticket.{tid}.created_days_ago",
                    ),
                    resolved_at=resolved_at,
                )
            )

        succeeded_refunds_cents = 0
        orm_refunds: list[Refund] = []
        for raw_r in raw_o.get("refunds", []):
            rid = raw_r["refund_id"]
            if not REFUND_ID_RE.match(rid):
                raise SeedValidationError(f"退款单号格式不合法: {rid}")
            if rid in refund_ids:
                raise SeedValidationError(f"重复的 refund_id: {rid}")
            refund_ids.add(rid)

            ref_ticket_id = raw_r.get("ticket_id")
            if ref_ticket_id is not None and ref_ticket_id not in order_ticket_ids:
                raise SeedValidationError(
                    f"退款单 {rid} 关联的工单 {ref_ticket_id} 不属于订单 {oid}"
                )

            if raw_r["reason_category"] not in VALID_REASON_CATEGORIES:
                raise SeedValidationError(
                    f"退款单 {rid} 的 reason_category 无效: {raw_r['reason_category']}"
                )
            if raw_r["status"] not in VALID_REFUND_STATUSES:
                raise SeedValidationError(f"退款单 {rid} 的 status 无效: {raw_r['status']}")
            if raw_r["created_by_role"] not in VALID_ROLES:
                raise SeedValidationError(
                    f"退款单 {rid} 的 created_by_role 无效: {raw_r['created_by_role']}"
                )

            ref_cents = _yuan_to_cents(raw_r["amount"], f"refund.{rid}.amount")
            if ref_cents <= 0:
                raise SeedValidationError(f"退款单 {rid} 金额必须 > 0")
            if raw_r["status"] == "succeeded":
                succeeded_refunds_cents += ref_cents

            completed_at = (
                _anchor_time(
                    raw_r["completed_days_ago"],
                    time(16, 45, 0),
                    anchor_date=anchor,
                    tz=tz,
                    field_name=f"refund.{rid}.completed_days_ago",
                )
                if raw_r.get("completed_days_ago") is not None
                else None
            )
            orm_refunds.append(
                Refund(
                    refund_id=rid,
                    order_id=oid,
                    region=region,
                    ticket_id=ref_ticket_id,
                    amount_cents=ref_cents,
                    reason_category=raw_r["reason_category"],
                    reason=raw_r["reason"],
                    status=raw_r["status"],
                    idempotency_key=raw_r.get("idempotency_key"),
                    created_by=raw_r["created_by"],
                    created_by_role=raw_r["created_by_role"],
                    created_at=_anchor_time(
                        raw_r["created_days_ago"],
                        time(16, 40, 0),
                        anchor_date=anchor,
                        tz=tz,
                        field_name=f"refund.{rid}.created_days_ago",
                    ),
                    completed_at=completed_at,
                )
            )

        if succeeded_refunds_cents > total_cents:
            raise SeedValidationError(
                f"订单 {oid} 已成功退款合计 ({succeeded_refunds_cents}) "
                f"超过实付金额 ({total_cents})"
            )

        # 注意：scenario 仅用于种子文件说明，严禁入库
        order = Order(
            order_id=oid,
            customer_id=cid,
            region=region,
            status=status,
            total_amount_cents=total_cents,
            refunded_amount_cents=succeeded_refunds_cents,
            created_at=_anchor_time(
                raw_o["created_days_ago"],
                time(10, 12, 0),
                anchor_date=anchor,
                tz=tz,
                field_name=f"order.{oid}.created_days_ago",
            ),
            paid_at=(
                _anchor_time(
                    raw_o["paid_days_ago"],
                    time(10, 15, 0),
                    anchor_date=anchor,
                    tz=tz,
                    field_name=f"order.{oid}.paid_days_ago",
                )
                if raw_o.get("paid_days_ago") is not None
                else None
            ),
            shipped_at=(
                _anchor_time(
                    raw_o["shipped_days_ago"],
                    time(9, 0, 0),
                    anchor_date=anchor,
                    tz=tz,
                    field_name=f"order.{oid}.shipped_days_ago",
                )
                if raw_o.get("shipped_days_ago") is not None
                else None
            ),
            delivered_at=(
                _anchor_time(
                    raw_o["delivered_days_ago"],
                    time(14, 30, 0),
                    anchor_date=anchor,
                    tz=tz,
                    field_name=f"order.{oid}.delivered_days_ago",
                )
                if raw_o.get("delivered_days_ago") is not None
                else None
            ),
        )
        session.add(order)
        session.flush()
        session.add_all(orm_items)
        session.add_all(orm_tickets)
        session.flush()
        session.add_all(orm_refunds)

    session.commit()


def seed_database(
    engine: Engine,
    seed_path: Path,
    *,
    reset: bool = True,
    now: datetime | None = None,
    tz_name: str = "Asia/Shanghai",
) -> bool:
    session_factory = create_session_factory(engine)
    if reset:
        reset_db(engine)
    else:
        init_db(engine)
        with session_factory() as check_session:
            count = check_session.scalar(select(func.count()).select_from(Order)) or 0
            if count > 0:
                return False

    with session_factory() as session:
        seed_session(session, seed_path, now=now, tz_name=tz_name)
    return True


def main() -> int:
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    try:
        seed_database(
            engine,
            settings.resolve_seed_path(),
            reset=True,
            tz_name=settings.timezone,
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
