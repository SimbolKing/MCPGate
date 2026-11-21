import json
import re

ID_WHITELIST_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")

QUERY_TOOL_WHITELIST = (
    "get_refund",
    "get_approval",
    "list_refunds",
    "list_orders",
    "list_approvals",
)

VALID_ORDER_STATUSES = (
    "pending_payment",
    "paid",
    "shipped",
    "delivered",
    "completed",
    "cancelled",
)

VALID_REFUND_STATUSES = (
    "processing",
    "succeeded",
    "failed",
)

MAX_AMOUNT = 10_000_000.0


def _clean_identifier(raw: object, query_upper: str, must_in_query: bool) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or not ID_WHITELIST_RE.match(value):
        return ""
    upper = value.upper()
    if must_in_query and upper not in query_upper:
        return ""
    return upper


def _clean_amount(raw: object) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if val != val or abs(val) == float("inf") or val <= 0 or val > MAX_AMOUNT:
        return 0.0
    rounded = round(val, 2)
    return rounded if rounded > 0 else 0.0


def main(
    query: str = "",
    order_id: str = "",
    approval_id: str = "",
    refund_id: str = "",
    customer_id: str = "",
    amount: object = None,
    reason_note: str = "",
    list_target: str = "none",
    order_status: str = "none",
    refund_status: str = "none",
    open_ticket_only: str = "none",
    standalone_question: str = "",
    current_order_id: str = "",
    last_approval_id: str = "",
) -> dict:
    clean_query = query.strip() if isinstance(query, str) else ""
    query_upper = clean_query.upper()

    turn_order_id = _clean_identifier(order_id, query_upper, must_in_query=True)
    turn_approval_id = _clean_identifier(approval_id, query_upper, must_in_query=True)
    turn_refund_id = _clean_identifier(refund_id, query_upper, must_in_query=True)
    turn_customer_id = _clean_identifier(customer_id, query_upper, must_in_query=True)

    conv_order_id = _clean_identifier(current_order_id, "", must_in_query=False)
    conv_approval_id = _clean_identifier(last_approval_id, "", must_in_query=False)

    resolved_order_id = turn_order_id or conv_order_id
    resolved_amount = _clean_amount(amount)
    clean_reason_note = reason_note.strip() if isinstance(reason_note, str) else ""
    clean_standalone = standalone_question.strip() if isinstance(standalone_question, str) else ""
    kb_query = clean_standalone or clean_query

    norm_list_target = list_target.strip().lower() if isinstance(list_target, str) else ""
    norm_order_status = order_status.strip().lower() if isinstance(order_status, str) else ""
    norm_refund_status = refund_status.strip().lower() if isinstance(refund_status, str) else ""
    norm_open_ticket = open_ticket_only.strip().lower() if isinstance(open_ticket_only, str) else ""

    valid_order_status = norm_order_status if norm_order_status in VALID_ORDER_STATUSES else ""
    valid_refund_status = norm_refund_status if norm_refund_status in VALID_REFUND_STATUSES else ""
    has_open_ticket_filter = norm_open_ticket in ("yes", "no")

    query_tool = ""
    query_args: dict = {}

    if turn_refund_id:
        query_tool = "get_refund"
        query_args = {"refund_id": turn_refund_id}
    elif turn_approval_id:
        query_tool = "get_approval"
        query_args = {"approval_id": turn_approval_id}
    elif norm_list_target == "refunds":
        query_tool = "list_refunds"
        query_args = {"page_size": 20}
        if turn_order_id:
            query_args["order_id"] = turn_order_id
        if valid_refund_status:
            query_args["status"] = valid_refund_status
    elif (
        norm_list_target == "orders"
        or turn_customer_id
        or valid_order_status
        or has_open_ticket_filter
    ):
        query_tool = "list_orders"
        query_args = {"page_size": 20}
        if turn_customer_id:
            query_args["customer_id"] = turn_customer_id
        if valid_order_status:
            query_args["status"] = valid_order_status
        if has_open_ticket_filter:
            query_args["has_open_ticket"] = norm_open_ticket == "yes"
    elif turn_order_id:
        query_tool = "list_approvals"
        query_args = {"order_id": turn_order_id, "page_size": 5}
    elif conv_approval_id:
        query_tool = "get_approval"
        query_args = {"approval_id": conv_approval_id}
    elif conv_order_id:
        query_tool = "list_approvals"
        query_args = {"order_id": conv_order_id, "page_size": 5}

    if query_tool and query_tool not in QUERY_TOOL_WHITELIST:
        query_tool = ""
        query_args = {}

    return {
        "order_id": resolved_order_id,
        "amount": resolved_amount,
        "reason_note": clean_reason_note,
        "kb_query": kb_query,
        "query_tool": query_tool,
        "query_args_json": json.dumps(query_args, ensure_ascii=False),
    }
