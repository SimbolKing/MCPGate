import json

# === INLINE:_mcp_common.py START ===
from _mcp_common import (
    ORDER_STATUS_LABELS,
    REFUND_STATUS_LABELS,
    format_error_reply,
    make_unavailable_result,
    parse_mcp_response,
    require_dict_list,
    require_int,
    require_number,
    require_opt_bool,
    require_opt_str,
    safe_approval_message,
)

# === INLINE:_mcp_common.py END ===


def _extract_target_id(query_args_json: str) -> str:
    try:
        args = json.loads(query_args_json) if isinstance(query_args_json, str) else {}
    except ValueError:
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("approval_id", "refund_id", "order_id"):
        if args.get(key):
            return str(args[key])
    return ""


def _format_get_approval(data: dict) -> str:
    status = require_opt_str(data.get("status"))
    oid = require_opt_str(data.get("order_id"))
    amount = require_number(data.get("amount"))
    require_opt_str(data.get("message"))
    applicant = require_opt_str(data.get("applicant_name")) or require_opt_str(
        data.get("applicant_sub")
    )
    decided_by = require_opt_str(data.get("decided_by_name")) or require_opt_str(
        data.get("decided_by_sub")
    )
    comment = require_opt_str(data.get("decision_comment")).strip()

    if status != "executed":
        applicant = applicant.replace("已执行", "")
        decided_by = decided_by.replace("已执行", "")
        comment = comment.replace("已执行", "")

    detail_parts = [
        f"订单号：{oid}",
        f"申请金额：{amount:.2f} 元",
        f"申请人：{applicant}",
    ]
    if decided_by:
        detail_parts.append(f"审批人：{decided_by}")
    if comment:
        detail_parts.append(f"审批意见：{comment}")

    return f"{safe_approval_message(data)}\n{'；'.join(detail_parts)}"


def _format_list_approvals(data: dict, target_id: str) -> str:
    items = require_dict_list(data.get("items"))
    total = require_int(data.get("total"), default=len(items))
    if not items:
        if target_id:
            return f"订单 {target_id} 没有退款申请记录。"
        return "没有符合条件的退款申请记录。"

    header = (
        f"共 {total} 条退款申请（仅显示前 {len(items)} 条）："
        if total > len(items)
        else f"共 {total} 条退款申请："
    )
    lines = [header]
    for item in items:
        aid = require_opt_str(item.get("approval_id"))
        amt = require_number(item.get("amount"))
        require_opt_str(item.get("status"))
        require_opt_str(item.get("message"))
        lines.append(f"{aid}｜{amt:.2f} 元｜{safe_approval_message(item)}")
    return "\n".join(lines)


def _format_get_refund(data: dict) -> str:
    rid = require_opt_str(data.get("refund_id"))
    oid = require_opt_str(data.get("order_id"))
    amount = require_number(data.get("amount"))
    st = require_opt_str(data.get("status"))
    st_cn = REFUND_STATUS_LABELS.get(st, st)
    created_at = require_opt_str(data.get("created_at"))
    return (
        f"退款单 {rid}：订单 {oid}，金额 {amount:.2f} 元，状态 {st_cn}，创建时间 {created_at}。\n"
        "说明：退款单的操作人是审批主管，申请人见对应的退款申请。"
    )


def _format_list_refunds(data: dict) -> str:
    items = require_dict_list(data.get("items"))
    total = require_int(data.get("total"), default=len(items))
    if not items:
        return "没有符合条件的记录（结果仅包含你负责区域的数据）。"

    header = (
        f"共 {total} 条退款记录（仅显示前 {len(items)} 条，仅包含你负责区域的数据）："
        if total > len(items)
        else f"共 {total} 条退款记录（仅包含你负责区域的数据）："
    )
    lines = [header]
    for item in items:
        rid = require_opt_str(item.get("refund_id"))
        oid = require_opt_str(item.get("order_id"))
        amt = require_number(item.get("amount"))
        st = require_opt_str(item.get("status"))
        st_cn = REFUND_STATUS_LABELS.get(st, st)
        lines.append(f"{rid}｜订单 {oid}｜{amt:.2f} 元｜{st_cn}")
    return "\n".join(lines)


def _format_list_orders(data: dict) -> str:
    items = require_dict_list(data.get("items"))
    total = require_int(data.get("total"), default=len(items))
    if not items:
        return "没有符合条件的记录（结果仅包含你负责区域的数据）。"

    header = (
        f"共 {total} 条订单记录（仅显示前 {len(items)} 条，仅包含你负责区域的数据）："
        if total > len(items)
        else f"共 {total} 条订单记录（仅包含你负责区域的数据）："
    )
    lines = [header]
    for item in items:
        oid = require_opt_str(item.get("order_id"))
        st = require_opt_str(item.get("status"))
        st_cn = ORDER_STATUS_LABELS.get(st, st)
        raw_days = item.get("days_since_delivery")
        days_text = f"已签收 {require_int(raw_days)} 天" if raw_days is not None else "未签收"
        amt = require_number(item.get("total_amount"))
        has_open_ticket = require_opt_bool(item.get("has_open_ticket"))
        ticket_text = "有待处理工单" if has_open_ticket else "无待处理工单"
        lines.append(f"{oid}｜{st_cn}｜{days_text}｜{amt:.2f} 元｜{ticket_text}")
    return "\n".join(lines)


def main(
    status_code: int,
    body: str,
    query_tool: str = "",
    query_args_json: str = "{}",
) -> dict:
    parsed = parse_mcp_response(query_tool, status_code, body)
    target_id = _extract_target_id(query_args_json)
    if parsed["kind"] != "ok":
        return {
            "ok": 0,
            "reply": format_error_reply(parsed, target_id=target_id),
            "call_summary": parsed["call_summary"],
        }

    data = parsed["data"]
    call_summary = dict(parsed["call_summary"])

    try:
        if query_tool == "get_approval":
            aid = require_opt_str(data.get("approval_id"))
            reply = _format_get_approval(data)
            if aid:
                call_summary["approval_id"] = aid
        elif query_tool == "list_approvals":
            reply = _format_list_approvals(data, target_id)
        elif query_tool == "get_refund":
            reply = _format_get_refund(data)
        elif query_tool == "list_refunds":
            reply = _format_list_refunds(data)
        else:
            reply = _format_list_orders(data)
    except ValueError:
        unavail = make_unavailable_result(query_tool, parsed["http_status"])
        return {
            "ok": 0,
            "reply": format_error_reply(unavail, target_id=target_id),
            "call_summary": unavail["call_summary"],
        }

    return {
        "ok": 1,
        "reply": reply,
        "call_summary": call_summary,
    }
