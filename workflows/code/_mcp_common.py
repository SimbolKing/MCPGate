import json
import re
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

CATEGORY_LABELS = {
    "electronics": "数码家电",
    "clothing": "服饰鞋包",
    "home": "家居日用",
    "fresh_food": "生鲜食品",
    "custom_made": "定制商品",
}

REASON_LABELS = {
    "quality_issue": "质量问题",
    "no_reason": "无理由",
    "wrong_item": "发错货",
    "damaged_in_transit": "运输破损",
    "not_received": "未收到货",
    "other": "其他",
}

CUSTOMER_LEVEL_LABELS = {
    "regular": "普通",
    "vip": "会员",
}

ORDER_STATUS_LABELS = {
    "pending_payment": "待支付",
    "paid": "已支付未发货",
    "shipped": "已发货未签收",
    "delivered": "已签收",
    "completed": "交易完成",
    "cancelled": "已取消",
}

TICKET_TYPE_LABELS = {
    "return_refund": "退货退款",
    "refund_only": "仅退款",
}

TICKET_STATUS_LABELS = {
    "open": "待处理",
    "resolved": "已处理",
    "rejected": "已驳回",
}

REFUND_STATUS_LABELS = {
    "processing": "处理中",
    "succeeded": "已成功",
    "failed": "失败",
}

MCP_ERROR_RE = re.compile(r"^call failed, status: (\d{3}), response: (.*)$", re.DOTALL)

_STATUS_KIND_MAP = {
    401: "auth_expired",
    403: "denied",
    404: "not_found",
    409: "conflict",
    422: "invalid",
}


def _build_call_summary(
    tool: str,
    kind: str,
    http_status: object = None,
    error_code: str = "",
    request_id: str = "",
) -> dict:
    summary: dict = {"tool": tool, "kind": kind}
    if http_status is not None:
        summary["http_status"] = http_status
    if error_code:
        summary["error_code"] = error_code
    if request_id:
        summary["request_id"] = request_id
    return summary


def _extract_error_text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return ""


def parse_mcp_response(tool: str, status_code: int, body: str) -> dict:
    if status_code == 401:
        return {
            "kind": "auth_expired",
            "http_status": 401,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": {},
            "call_summary": _build_call_summary(tool, "auth_expired", http_status=401),
        }

    if status_code != 200:
        return {
            "kind": "unavailable",
            "http_status": status_code,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": {},
            "call_summary": _build_call_summary(tool, "unavailable", http_status=status_code),
        }

    try:
        payload = json.loads(body) if isinstance(body, str) else None
    except ValueError:
        payload = None

    if not isinstance(payload, dict):
        return {
            "kind": "unavailable",
            "http_status": 200,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": {},
            "call_summary": _build_call_summary(tool, "unavailable", http_status=200),
        }

    if payload.get("error"):
        return {
            "kind": "internal",
            "http_status": 200,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": {},
            "call_summary": _build_call_summary(tool, "internal", http_status=200),
        }

    result = payload.get("result")
    if not isinstance(result, dict):
        return {
            "kind": "unavailable",
            "http_status": 200,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": {},
            "call_summary": _build_call_summary(tool, "unavailable", http_status=200),
        }

    if not result.get("isError"):
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            return {
                "kind": "unavailable",
                "http_status": 200,
                "error_code": "",
                "error_message": "",
                "request_id": "",
                "details": [],
                "data": {},
                "call_summary": _build_call_summary(tool, "unavailable", http_status=200),
            }
        return {
            "kind": "ok",
            "http_status": 200,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": structured,
            "call_summary": _build_call_summary(tool, "ok", http_status=200),
        }

    text = _extract_error_text(result)
    match = MCP_ERROR_RE.match(text)
    if not match:
        return {
            "kind": "unavailable",
            "http_status": 200,
            "error_code": "",
            "error_message": "",
            "request_id": "",
            "details": [],
            "data": {},
            "call_summary": _build_call_summary(tool, "unavailable", http_status=200),
        }

    upstream_status = int(match.group(1))
    raw_response = match.group(2)
    if upstream_status in _STATUS_KIND_MAP:
        kind = _STATUS_KIND_MAP[upstream_status]
    elif 500 <= upstream_status <= 599:
        kind = "unavailable"
    else:
        kind = "internal"

    error_code = ""
    error_message = ""
    request_id = ""
    details: list = []
    try:
        err_body = json.loads(raw_response)
    except ValueError:
        err_body = None

    if isinstance(err_body, dict):
        error_code = str(err_body.get("code") or "")
        error_message = str(err_body.get("message") or "")
        request_id = str(err_body.get("request_id") or "")
        raw_details = err_body.get("details")
        if isinstance(raw_details, list):
            details = raw_details

    return {
        "kind": kind,
        "http_status": upstream_status,
        "error_code": error_code,
        "error_message": error_message,
        "request_id": request_id,
        "details": details,
        "data": {},
        "call_summary": _build_call_summary(
            tool,
            kind,
            http_status=upstream_status,
            error_code=error_code,
            request_id=request_id,
        ),
    }


def format_error_reply(parsed: dict, target_id: str = "") -> str:
    kind = parsed["kind"]
    code = parsed["error_code"]
    message = parsed["error_message"]

    if kind == "auth_expired":
        return "登录已过期或无效，请重新登录后再试。"

    if kind == "denied":
        if code == "order_access_denied":
            return "你没有权限查看该订单。"
        if code == "refund_access_denied":
            return "你没有权限查看该退款单。"
        if code == "approval_access_denied":
            return "你没有权限查看该退款申请。"
        if code == "insufficient_role":
            return "该功能仅主管可用。"
        if code == "invalid_claims":
            return "当前账号的角色或区域信息无效，请联系管理员。"
        return "你没有权限执行该操作。"

    if kind == "not_found":
        if code == "order_not_found":
            label = f"订单 {target_id}" if target_id else "该订单"
            return f"没有找到{label}，请核对单号。"
        if code == "refund_not_found":
            label = f"退款单 {target_id}" if target_id else "该退款单"
            return f"没有找到{label}，请核对单号。"
        if code == "approval_not_found":
            label = f"审批单 {target_id}" if target_id else "该审批单"
            return f"没有找到{label}，请核对单号。"
        return message or "没有找到对应记录，请核对单号。"

    if kind == "conflict":
        if code == "approval_pending_exists":
            base = message or "该订单已有待审批的退款申请。"
            return f"{base} 无需重复提交，可以问我该申请的进度。"
        return message or "当前操作与业务状态冲突，无法继续。"

    if kind == "invalid":
        details = parsed["details"]
        if details and isinstance(details[0], dict) and details[0].get("message"):
            return f"请求参数不合法：{details[0]['message']}"
        return f"请求参数不合法：{message or '请检查输入参数。'}"

    if kind == "internal":
        return "助手与网关的工具清单不一致，请联系管理员。"

    return "售后系统暂时不可用，请稍后再试。"


def safe_approval_message(approval: dict) -> str:
    status = str(approval.get("status") or "")
    aid = str(approval.get("approval_id") or "")
    msg = str(approval.get("message") or "").strip()

    if status == "executed":
        return msg or f"主管已同意退款申请 {aid}，退款已执行。"

    if msg and "已执行" not in msg:
        return msg

    fallback = {
        "pending": f"已提交退款申请 {aid}，等待主管审批。退款尚未执行。",
        "executing": f"主管已同意退款申请 {aid}，退款正在执行，结果尚未确认。",
        "execution_failed": f"主管已同意退款申请 {aid}，但订单系统拒绝执行，退款未执行。",
        "rejected": f"主管已拒绝退款申请 {aid}，退款未执行。",
        "expired": f"退款申请 {aid} 审批已超时失效，退款未执行。",
    }
    return fallback.get(status, f"退款申请 {aid} 当前尚未完成执行。")


def format_cn_date(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return str(iso_str or "")
    if dt.tzinfo is not None:
        dt = dt.astimezone(CN_TZ)
    return f"{dt.month} 月 {dt.day} 日"


def make_unavailable_result(
    tool: str,
    http_status: int = 200,
    approval_id: str = "",
) -> dict:
    summary = _build_call_summary(tool, "unavailable", http_status=http_status)
    if approval_id:
        summary["approval_id"] = approval_id
    return {
        "kind": "unavailable",
        "http_status": http_status,
        "error_code": "",
        "error_message": "",
        "request_id": "",
        "details": [],
        "data": {},
        "call_summary": summary,
    }


def require_opt_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("expected str")
    return value


def require_opt_bool(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError("expected bool")
    return value


def require_number(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected number")
    num = float(value)
    if num != num or abs(num) == float("inf"):
        raise ValueError("non-finite number")
    return num


def require_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected int")
    return value


def require_opt_dict(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected dict")
    return value


def require_dict_list(value: object) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("expected list of dict")
    return value
