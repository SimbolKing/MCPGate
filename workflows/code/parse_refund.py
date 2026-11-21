# === INLINE:_mcp_common.py START ===
from _mcp_common import (
    REASON_LABELS,
    format_error_reply,
    make_unavailable_result,
    parse_mcp_response,
    require_number,
    require_opt_str,
    safe_approval_message,
)

# === INLINE:_mcp_common.py END ===


def main(
    status_code: int,
    body: str,
    basis: str = "",
    order_id: str = "",
) -> dict:
    parsed = parse_mcp_response("create_refund", status_code, body)
    if parsed["kind"] != "ok":
        return {
            "ok": 0,
            "reply": format_error_reply(parsed, target_id=order_id),
            "call_summary": parsed["call_summary"],
            "approval_id": "",
        }

    data = parsed["data"]
    raw_approval_id = data.get("approval_id")
    approval_id = (
        raw_approval_id.strip()
        if isinstance(raw_approval_id, str) and raw_approval_id.strip()
        else ""
    )

    try:
        if raw_approval_id is not None and not isinstance(raw_approval_id, str):
            raise ValueError("expected str approval_id")
        status = require_opt_str(data.get("status"))
        oid = require_opt_str(data.get("order_id")) or order_id
        amount = require_number(data.get("amount"))
        rc = require_opt_str(data.get("reason_category"))
        require_opt_str(data.get("message"))
    except ValueError:
        unavail = make_unavailable_result(
            "create_refund",
            parsed["http_status"],
            approval_id=approval_id,
        )
        return {
            "ok": 0,
            "reply": format_error_reply(unavail, target_id=order_id),
            "call_summary": unavail["call_summary"],
            "approval_id": approval_id,
        }

    rc_cn = REASON_LABELS.get(rc, rc)

    call_summary = dict(parsed["call_summary"])
    if status == "pending":
        call_summary["kind"] = "pending"
    if approval_id:
        call_summary["approval_id"] = approval_id

    clean_basis = basis.strip() if isinstance(basis, str) else ""
    if status != "executed":
        clean_basis = clean_basis.replace("已执行", "")

    lines = [safe_approval_message(data)]
    detail = f"订单号：{oid}；申请金额：{amount:.2f} 元；原因类别：{rc_cn}"
    if clean_basis:
        detail += f"；依据条款：{clean_basis}"
    lines.append(detail)
    if approval_id:
        lines.append(f"之后可以问我“{approval_id} 批了吗”。")

    return {
        "ok": 1,
        "reply": "\n".join(lines),
        "call_summary": call_summary,
        "approval_id": approval_id,
    }
