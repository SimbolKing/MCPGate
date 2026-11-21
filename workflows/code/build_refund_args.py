import json
import re

VALID_REASON_CATEGORIES = (
    "quality_issue",
    "no_reason",
    "wrong_item",
    "damaged_in_transit",
    "not_received",
    "other",
)

CLAUSE_HEADER_RE = re.compile(r"第\s*(\d+\.\d+)\s*条")
CLAUSE_NUM_RE = re.compile(r"(\d+\.\d+)")
FENCE_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
REASON_MAX_LEN = 200
LARGE_REFUND_THRESHOLD = 2000.0


def _parse_decision(structured_output: object, llm_text: str):
    if isinstance(structured_output, dict) and structured_output:
        return structured_output
    for candidate in (structured_output, llm_text):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        text = candidate.strip()
        fence = FENCE_JSON_RE.search(text)
        if fence:
            text = fence.group(1)
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_retrieved_clauses(retrieved_chunks: object) -> set:
    clauses: set = set()
    if not isinstance(retrieved_chunks, list):
        return clauses
    for chunk in retrieved_chunks:
        content = chunk.get("content", "") if isinstance(chunk, dict) else ""
        if not isinstance(content, str) or not content.strip():
            continue
        first_line = content.strip().splitlines()[0]
        match = CLAUSE_HEADER_RE.search(first_line)
        if match:
            clauses.add(match.group(1))
    return clauses


def _filter_clause_refs(raw_refs: object, allowed_clauses: set) -> list:
    if not isinstance(raw_refs, list):
        return []
    result: list = []
    for item in raw_refs:
        match = CLAUSE_NUM_RE.search(str(item))
        if match:
            num = match.group(1)
            if num in allowed_clauses and num not in result:
                result.append(num)
    return result


def _compose_reason(raw_reason: str, basis: str, amount: float) -> str:
    suffix = f"依据{basis}"
    if amount >= LARGE_REFUND_THRESHOLD:
        suffix += "；属大额退款（第 7.3 条）"
    base = raw_reason.replace("\r", " ").replace("\n", " ").strip()
    if not base:
        return suffix[:REASON_MAX_LEN]
    max_base_len = REASON_MAX_LEN - len(suffix) - 1
    if max_base_len <= 0:
        return suffix[:REASON_MAX_LEN]
    if len(base) > max_base_len:
        base = base[: max_base_len - 1] + "…"
    return f"{base}；{suffix}"


def main(
    structured_output: object = None,
    llm_text: str = "",
    order_id: str = "",
    user_amount: float = 0.0,
    refundable_amount: float = 0.0,
    open_ticket_ids: object = None,
    ticket_amount: float = 0.0,
    retrieved_chunks: object = None,
) -> dict:
    clean_order_id = order_id.strip().upper() if isinstance(order_id, str) else ""
    decision = _parse_decision(structured_output, llm_text)
    if decision is None:
        return {
            "go": 0,
            "reply": (
                "无法确认本次退款是否符合售后制度，未提交。"
                "如客户坚持，请按《售后制度》第 7.5 条报主管线下处理。"
            ),
            "arguments_json": "{}",
            "basis": "",
        }

    allowed_clauses = _extract_retrieved_clauses(retrieved_chunks)
    valid_refs = _filter_clause_refs(decision.get("clause_refs"), allowed_clauses)
    basis = "、".join(f"《售后制度》第 {ref} 条" for ref in valid_refs)

    if decision.get("compliant") is not True:
        explanation = str(decision.get("explanation") or "本次申请不符合售后制度规定。").strip()
        ref_part = f"（依据：{basis}）" if basis else ""
        return {
            "go": 0,
            "reply": (
                f"{explanation}{ref_part}\n如客户坚持，请按《售后制度》第 7.5 条报主管线下处理。"
            ),
            "arguments_json": "{}",
            "basis": basis,
        }

    if not valid_refs:
        return {
            "go": 0,
            "reply": "未能在制度中找到支持本次退款的条款，未提交。",
            "arguments_json": "{}",
            "basis": "",
        }

    max_refundable = round(float(refundable_amount), 2)
    if float(user_amount) > 0:
        chosen_amount = round(float(user_amount), 2)
    elif float(ticket_amount) > 0:
        chosen_amount = round(float(ticket_amount), 2)
    else:
        chosen_amount = max_refundable

    if chosen_amount <= 0 or chosen_amount > max_refundable:
        return {
            "go": 0,
            "reply": (
                f"申请退款金额 {chosen_amount:.2f} 元不合法或超过当前可退金额 "
                f"{max_refundable:.2f} 元，未提交退款申请（《售后制度》第 5.3 条）。"
            ),
            "arguments_json": "{}",
            "basis": basis,
        }

    raw_category = str(decision.get("reason_category") or "").strip()
    reason_category = raw_category if raw_category in VALID_REASON_CATEGORIES else "other"

    raw_reason = str(decision.get("reason") or decision.get("explanation") or "").strip()
    final_reason = _compose_reason(raw_reason, basis, chosen_amount)

    args: dict = {
        "order_id": clean_order_id,
        "amount": chosen_amount,
        "reason_category": reason_category,
        "reason": final_reason,
    }

    open_set = set(open_ticket_ids) if isinstance(open_ticket_ids, list) else set()
    cand_ticket = str(decision.get("ticket_id") or "").strip()
    if cand_ticket and cand_ticket in open_set:
        args["ticket_id"] = cand_ticket

    return {
        "go": 1,
        "reply": "",
        "arguments_json": json.dumps(args, ensure_ascii=False),
        "basis": basis,
    }
