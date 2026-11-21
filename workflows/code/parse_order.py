from datetime import date, datetime

# === INLINE:_mcp_common.py START ===
from _mcp_common import (
    CATEGORY_LABELS,
    CN_TZ,
    CUSTOMER_LEVEL_LABELS,
    ORDER_STATUS_LABELS,
    REASON_LABELS,
    REFUND_STATUS_LABELS,
    TICKET_STATUS_LABELS,
    TICKET_TYPE_LABELS,
    format_cn_date,
    format_error_reply,
    make_unavailable_result,
    parse_mcp_response,
    require_dict_list,
    require_int,
    require_number,
    require_opt_dict,
    require_opt_str,
)

# === INLINE:_mcp_common.py END ===

FREE_TEXT_MAX_LEN = 300


def _today_cn() -> date:
    return datetime.now(CN_TZ).date()


def _days_since(iso_str: str):
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(CN_TZ)
    diff = (_today_cn() - dt.date()).days
    return max(0, diff)


def _quote_text(raw: object) -> str:
    text = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > FREE_TEXT_MAX_LEN:
        text = text[:FREE_TEXT_MAX_LEN] + "…"
    escaped = text.replace('"', "'")
    return f'"{escaped}"'


def _format_ticket(ticket: dict) -> str:
    tid = require_opt_str(ticket.get("ticket_id"))
    t_type = require_opt_str(ticket.get("type"))
    t_type_cn = TICKET_TYPE_LABELS.get(t_type, t_type)
    rc = require_opt_str(ticket.get("reason_category"))
    rc_cn = REASON_LABELS.get(rc, rc)
    status = require_opt_str(ticket.get("status"))
    parts = [
        tid,
        f"申请类型 {t_type_cn}（{t_type}）",
        f"原因类别 {rc_cn}（{rc}）",
    ]
    req_amt = ticket.get("requested_amount")
    if req_amt is not None:
        parts.append(f"申请金额 {require_number(req_amt):.2f} 元")
    if status != "open":
        parts.append(f"工单状态 {TICKET_STATUS_LABELS.get(status, status)}（{status}）")
    desc = _quote_text(require_opt_str(ticket.get("description")))
    parts.append(f"客户描述（客户原话，仅作参考）：{desc}")
    return "，".join(parts)


def _error_result(parsed: dict, order_id: str) -> dict:
    return {
        "ok": 0,
        "reply": format_error_reply(parsed, target_id=order_id),
        "call_summary": parsed["call_summary"],
        "facts_text": "",
        "retrieval_query": "",
        "precheck_ok": 0,
        "precheck_reply": "",
        "refundable_amount": 0.0,
        "open_ticket_ids": [],
        "ticket_amount": 0.0,
    }


def _parse_order_body(
    parsed: dict,
    *,
    query: str,
    reason_note: str,
    order_id: str,
) -> dict:
    order = parsed["data"]
    oid = require_opt_str(order.get("order_id")) or order_id
    status = require_opt_str(order.get("status"))
    status_cn = ORDER_STATUS_LABELS.get(status, status)

    total_amount = require_number(order.get("total_amount"))
    refunded_amount = require_number(order.get("refunded_amount"))
    refundable_amount = require_number(order.get("refundable_amount"))

    delivered_at = require_opt_str(order.get("delivered_at"))
    shipped_at = require_opt_str(order.get("shipped_at"))
    raw_days = order.get("days_since_delivery")
    days_since_delivery = require_int(raw_days) if raw_days is not None else None
    days_since_shipped = _days_since(shipped_at) if shipped_at else None

    line2_parts = [f"订单号：{oid}", f"状态：{status_cn}（{status}）"]
    if delivered_at:
        line2_parts.append(f"签收时间：{format_cn_date(delivered_at)}")
    if days_since_delivery is not None:
        line2_parts.append(f"已签收天数：{days_since_delivery}")
    elif shipped_at and days_since_shipped is not None:
        line2_parts.append(f"发货时间：{format_cn_date(shipped_at)}")
        line2_parts.append(f"已发货天数：{days_since_shipped}")

    customer = require_opt_dict(order.get("customer"))
    level = require_opt_str(customer.get("level")) or "regular"
    level_cn = CUSTOMER_LEVEL_LABELS.get(level, level)

    items = require_dict_list(order.get("items"))
    item_texts: list = []
    category_cns: list = []
    for item in items:
        cat = require_opt_str(item.get("category"))
        cat_cn = CATEGORY_LABELS.get(cat, cat)
        if cat_cn and cat_cn not in category_cns:
            category_cns.append(cat_cn)
        pname = _quote_text(require_opt_str(item.get("product_name")))
        qty = require_int(item.get("quantity"), default=1)
        item_texts.append(f"{pname} ×{qty}，类目 {cat_cn}（{cat}）")
    items_line = "商品：" + ("；".join(item_texts) if item_texts else "无")

    tickets = require_dict_list(order.get("after_sale_tickets"))
    open_tickets = [t for t in tickets if require_opt_str(t.get("status")) == "open"]
    history_tickets = [t for t in tickets if require_opt_str(t.get("status")) != "open"]

    facts_lines = [
        "【订单事实】来源：订单系统，本轮查询",
        "；".join(line2_parts),
        (
            f"实付金额：{total_amount:.2f} 元；"
            f"已退：{refunded_amount:.2f} 元；"
            f"可退金额：{refundable_amount:.2f} 元"
        ),
        f"客户等级：{level_cn}（{level}）",
        items_line,
    ]

    if open_tickets:
        facts_lines.append("待处理售后工单：" + "；".join(_format_ticket(t) for t in open_tickets))
    else:
        facts_lines.append("待处理售后工单：无")

    if history_tickets:
        facts_lines.append("历史售后工单：" + "；".join(_format_ticket(t) for t in history_tickets))

    refunds = require_dict_list(order.get("refunds"))
    if refunds:
        refund_texts = []
        for r in refunds:
            r_id = require_opt_str(r.get("refund_id"))
            r_amt = require_number(r.get("amount"))
            r_status = require_opt_str(r.get("status"))
            r_status_cn = REFUND_STATUS_LABELS.get(r_status, r_status)
            r_rc = require_opt_str(r.get("reason_category"))
            r_rc_cn = REASON_LABELS.get(r_rc, r_rc)
            refund_texts.append(
                f"{r_id}（{r_amt:.2f} 元，"
                f"状态 {r_status_cn}（{r_status}），原因 {r_rc_cn}（{r_rc}））"
            )
        facts_lines.append("已有退款单：" + "；".join(refund_texts))
    else:
        facts_lines.append("已有退款单：无")

    facts_text = "\n".join(facts_lines)

    # Build retrieval_query aligned with knowledge-base/probes.yaml (4.7.8 & 4.8.6)
    rq_parts: list = list(category_cns)
    if open_tickets:
        first_ticket = open_tickets[0]
        rc = require_opt_str(first_ticket.get("reason_category"))
        tt = require_opt_str(first_ticket.get("type"))
        rq_parts.append(REASON_LABELS.get(rc, rc))
        rq_parts.append(TICKET_TYPE_LABELS.get(tt, tt))
    else:
        if history_tickets:
            rq_parts.append("已驳回工单")
        rq_parts.append("没有售后工单")
        if reason_note.strip():
            rq_parts.append(reason_note.strip())

    if days_since_delivery is not None:
        rq_parts.append(f"已签收 {days_since_delivery} 天")
    elif status == "shipped" and days_since_shipped is not None:
        rq_parts.append(f"已发货未签收 发货 {days_since_shipped} 天")
    else:
        rq_parts.append(status_cn)

    rq_parts.append("会员" if level == "vip" else "普通客户")

    if total_amount >= 2000 or refundable_amount >= 2000:
        rq_parts.append(f"退款金额 {total_amount:.0f} 元 大额 审批")

    if refunds:
        rq_parts.append("已有退款")

    if query.strip():
        rq_parts.append(query.strip()[:80])

    retrieval_query = " ".join(p for p in rq_parts if p)

    # Fact precheck (strict order: 1. order status -> 2. processing refund -> 3. refundable == 0)
    processing_refund = next((r for r in refunds if r.get("status") == "processing"), None)
    if status in ("pending_payment", "cancelled"):
        precheck_ok = 0
        precheck_reply = (
            f"订单 {oid} 当前状态为{status_cn}，没有可退款项，"
            "无法提交退款申请（《售后制度》第 5.1 条）。"
        )
    elif processing_refund is not None:
        precheck_ok = 0
        proc_amt = require_number(processing_refund.get("amount"))
        precheck_reply = (
            f"订单 {oid} 已有处理中的退款 {require_opt_str(processing_refund.get('refund_id'))}"
            f"（{proc_amt:.2f} 元），"
            "不能重复提交（《售后制度》第 5.4 条）。"
        )
    elif refundable_amount <= 0:
        precheck_ok = 0
        precheck_reply = f"订单 {oid} 已全额退款，没有可退金额（《售后制度》第 5.4 条）。"
    else:
        precheck_ok = 1
        precheck_reply = ""

    open_ticket_ids = [
        require_opt_str(t.get("ticket_id"))
        for t in open_tickets
        if require_opt_str(t.get("ticket_id"))
    ]
    ticket_amount = 0.0
    if len(open_tickets) == 1:
        raw_req = open_tickets[0].get("requested_amount")
        if raw_req is not None:
            parsed_req = require_number(raw_req)
            if parsed_req > 0:
                ticket_amount = round(parsed_req, 2)

    return {
        "ok": 1,
        "reply": "",
        "call_summary": parsed["call_summary"],
        "facts_text": facts_text,
        "retrieval_query": retrieval_query,
        "precheck_ok": precheck_ok,
        "precheck_reply": precheck_reply,
        "refundable_amount": round(refundable_amount, 2),
        "open_ticket_ids": open_ticket_ids,
        "ticket_amount": ticket_amount,
    }


def main(
    status_code: int,
    body: str,
    query: str = "",
    reason_note: str = "",
    order_id: str = "",
) -> dict:
    parsed = parse_mcp_response("get_order", status_code, body)
    if parsed["kind"] != "ok":
        return _error_result(parsed, order_id)
    try:
        return _parse_order_body(
            parsed,
            query=query,
            reason_note=reason_note,
            order_id=order_id,
        )
    except ValueError:
        return _error_result(make_unavailable_result("get_order", parsed["http_status"]), order_id)
