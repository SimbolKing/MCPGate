"""Deterministic assertions and rule-based verdict classification (5.5.1–5.5.4, 5.3.2)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import jmespath

from harness.schema import (
    ChatAssertionBlock,
    ChatStep,
    GuardStep,
    McpStep,
    TraceCallExpect,
    TraceExpect,
    TraceForbidItem,
)

G8_KEYS = frozenset({"code", "message", "request_id"})
REDACTED_TOKEN = "[REDACTED_TOKEN]"

JWT_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
BLANK_LINE_RE = re.compile(r"\n\s*\n")

# 5.5.3 verdict rule keyword table (longest-first within each group)
ESCALATE_KEYWORDS: tuple[str, ...] = ("请示主管",)
NEED_INFO_KEYWORDS: tuple[str, ...] = ("需补充信息", "需要补充", "先确认", "需确认")
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "没有可退款项",
    "不符合退货",
    "不符合退款",
    "不可以退",
    "不同意退",
    "不支持退",
    "不满足退",
    "不予受理",
    "不可退",
    "不能退",
    "不得退",
    "不应退",
    "无法退",
)
POSITIVE_KEYWORDS: tuple[str, ...] = ("可以退", "支持退", "同意退", "可退")


@dataclass
class JudgeVerdictResult:
    """Result returned by the optional LLM judge callback."""

    status: str  # "ok" | "invalid" | "unavailable" | "disabled"
    label: str  # one of JUDGE_LABELS or "unknown"
    evidence: str = ""
    cached: bool = False
    raw_output: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


JudgeCallback = Callable[[str, str], JudgeVerdictResult]


@dataclass
class AssertionResult:
    name: str
    level: str  # "must" | "should"
    passed: bool
    expected: Any
    actual: Any
    attribution: str
    needs_manual_review: bool = False
    verdict_source: str | None = None  # "rule" | "judge" | "judge_invalid" | "unknown"
    judge_record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "level": self.level,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "attribution": self.attribution,
        }
        if self.needs_manual_review:
            out["needs_manual_review"] = True
        if self.verdict_source is not None:
            out["verdict_source"] = self.verdict_source
        if self.judge_record is not None:
            out["judge_record"] = self.judge_record
        return out


def extract_conclusion_segment(answer: str) -> str:
    """Extract the conclusion segment per 5.5.3 step 1."""
    if "结论" in answer:
        start = answer.index("结论")
        tail = answer[start:]
        end = len(tail)
        basis_pos = tail.find("依据")
        if basis_pos != -1:
            end = min(end, basis_pos)
        blank_match = BLANK_LINE_RE.search(tail)
        if blank_match:
            end = min(end, blank_match.start())
        return tail[:end].strip()

    period_pos = answer.find("。")
    if period_pos != -1:
        return answer[: period_pos + 1].strip()
    return answer.strip()


def classify_verdict_by_rules(answer: str) -> str:
    """Classify answer conclusion direction by 5.5.3 deterministic rule table."""
    segment = extract_conclusion_segment(answer)
    matched: set[str] = set()

    if any(kw in segment for kw in ESCALATE_KEYWORDS):
        matched.add("escalate")
    if any(kw in segment for kw in NEED_INFO_KEYWORDS):
        matched.add("need_info")

    cleaned_for_positive = segment
    has_negative = False
    for kw in NEGATIVE_KEYWORDS:
        if kw in cleaned_for_positive:
            has_negative = True
            cleaned_for_positive = cleaned_for_positive.replace(kw, "")
    if has_negative:
        matched.add("not_refundable")

    if any(kw in cleaned_for_positive for kw in POSITIVE_KEYWORDS):
        matched.add("refundable")

    if len(matched) == 1:
        return next(iter(matched))
    return "unknown"


def match_clause(answer: str, doc: str, clause: str) -> bool:
    """Match '第 X.Y 条' in answer; non-policy docs also require doc name (5.5.2)."""
    pattern = rf"第\s*{re.escape(clause)}(?!\d)\s*条"
    if not re.search(pattern, answer):
        return False
    if doc != "售后制度" and doc not in answer:
        return False
    return True


def match_citation(citations: Sequence[dict[str, Any]], doc: str, clause: str) -> bool:
    """Check whether citations contain an item with matching document and clause (5.5.2)."""
    for item in citations:
        if item.get("document") == doc and str(item.get("clause", "")) == clause:
            return True
    return False


def contains_jwt_like(text: str) -> bool:
    return bool(JWT_LIKE_RE.search(text))


def find_leaked_tokens(payload: Any, tokens: Sequence[str]) -> list[str]:
    """Check whether any issued token or its signature segment appears in payload."""
    serialized = json.dumps(payload, ensure_ascii=False)
    hits: list[str] = []
    for idx, token in enumerate(tokens, start=1):
        if not token:
            continue
        parts = token.split(".")
        sig = parts[2] if len(parts) == 3 and len(parts[2]) >= 16 else ""
        if token in serialized or (sig and sig in serialized):
            hits.append(f"token#{idx}")
    return hits


def redact_tokens(obj: Any, tokens: Sequence[str] = ()) -> Any:
    """Recursively replace issued tokens, signatures, and JWT-shaped strings with REDACTED_TOKEN."""
    if isinstance(obj, str):
        out = obj
        for token in tokens:
            if not token:
                continue
            if token in out:
                out = out.replace(token, REDACTED_TOKEN)
            parts = token.split(".")
            sig = parts[2] if len(parts) == 3 and len(parts[2]) >= 16 else ""
            if sig and sig in out:
                out = out.replace(sig, REDACTED_TOKEN)
        return JWT_LIKE_RE.sub(REDACTED_TOKEN, out)
    if isinstance(obj, list):
        return [redact_tokens(item, tokens) for item in obj]
    if isinstance(obj, dict):
        return {redact_tokens(k, tokens): redact_tokens(v, tokens) for k, v in obj.items()}
    return obj


def check_g8_only_keys(raw_error_body_keys: Sequence[str] | None) -> bool:
    """Check that error body has only the 3 G8 keys: code, message, request_id (5.2.2)."""
    return raw_error_body_keys is not None and frozenset(raw_error_body_keys) == G8_KEYS


def _value_equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return bool(actual == expected)


def _trace_call_matches(actual: dict[str, Any], expected: TraceCallExpect) -> bool:
    if actual.get("tool") != expected.tool or actual.get("kind") != expected.kind:
        return False
    if expected.error_code is not None and actual.get("error_code") != expected.error_code:
        return False
    if expected.order_id is not None and actual.get("order_id") != expected.order_id:
        return False
    return True


def evaluate_trace_expect(actual_trace: Sequence[dict[str, Any]], expect: TraceExpect) -> bool:
    """Evaluate trace assertion in 'exact' or 'contains' mode (5.5.2)."""
    if expect.mode == "exact":
        if len(actual_trace) != len(expect.calls):
            return False
        return all(
            _trace_call_matches(act, exp)
            for act, exp in zip(actual_trace, expect.calls, strict=True)
        )

    # contains mode: ordered subsequence match
    idx = 0
    for exp in expect.calls:
        found = False
        while idx < len(actual_trace):
            matched = _trace_call_matches(actual_trace[idx], exp)
            idx += 1
            if matched:
                found = True
                break
        if not found:
            return False
    return True


def evaluate_trace_forbid(
    actual_trace: Sequence[dict[str, Any]],
    forbid_items: Sequence[TraceForbidItem],
) -> list[dict[str, Any]]:
    """Return trace calls that violated any trace_forbid rule."""
    violations: list[dict[str, Any]] = []
    for call in actual_trace:
        for rule in forbid_items:
            if call.get("tool") == rule.tool and (
                rule.kind is None or call.get("kind") == rule.kind
            ):
                violations.append(call)
                break
    return violations


def _normalize_str_list(val: list[str] | str) -> list[str]:
    return [val] if isinstance(val, str) else list(val)


def _evaluate_data_map(
    data: Any,
    expected_map: dict[str, Any],
    *,
    level: str,
    attribution: str,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    for expr, exp_val in expected_map.items():
        actual_val = jmespath.search(expr, data)
        results.append(
            AssertionResult(
                name=f"data:{expr}",
                level=level,
                passed=_value_equals(actual_val, exp_val),
                expected=exp_val,
                actual=actual_val,
                attribution=attribution,
            )
        )
    return results


def _step_level(layer: str, severity: str | None, *, skipped: bool = False) -> str:
    if layer in ("tool", "guard"):
        return "must"
    if skipped:
        return "should"
    return severity or "should"


def _tool_guard_attribution(layer: str, category: str, authz: str | None) -> str:
    if authz is not None:
        return "权限"
    if layer == "guard":
        return "审计" if category == "audit" else "审批"
    return "审批" if category == "write-gate" else "权限"


def evaluate_mcp_step(
    step: McpStep,
    response: dict[str, Any],
    *,
    layer: str,
    category: str,
    sentinels: Mapping[str, Sequence[str]],
    expected_data: dict[str, Any] | None = None,
    expected_message_contains: list[str] | str | None = None,
) -> list[AssertionResult]:
    """Evaluate all explicit + automatic assertions on an MCP step."""
    level = _step_level(layer, step.severity)
    attr = _tool_guard_attribution(layer, category, step.authz)
    results: list[AssertionResult] = []

    results.append(
        AssertionResult(
            name="result",
            level=level,
            passed=response.get("result") == step.expect.result,
            expected=step.expect.result,
            actual=response.get("result"),
            attribution=attr,
        )
    )

    if step.expect.status is not None:
        results.append(
            AssertionResult(
                name="status",
                level=level,
                passed=response.get("status_code") == step.expect.status,
                expected=step.expect.status,
                actual=response.get("status_code"),
                attribution=attr,
            )
        )

    if step.expect.error_code is not None:
        results.append(
            AssertionResult(
                name="error_code",
                level=level,
                passed=response.get("error_code") == step.expect.error_code,
                expected=step.expect.error_code,
                actual=response.get("error_code"),
                attribution=attr,
            )
        )

    data_map = expected_data if expected_data is not None else step.expect.data
    if data_map:
        results.extend(
            _evaluate_data_map(response.get("data", {}), data_map, level=level, attribution=attr)
        )

    msg_contains = (
        expected_message_contains
        if expected_message_contains is not None
        else step.expect.message_contains
    )
    message = str(response.get("message") or "")
    if msg_contains is not None:
        substrings = _normalize_str_list(msg_contains)
        missing = [sub for sub in substrings if sub not in message]
        results.append(
            AssertionResult(
                name="message_contains",
                level=level,
                passed=len(missing) == 0,
                expected=substrings,
                actual=message,
                attribution=attr,
            )
        )

    if step.expect.not_contains:
        found = [sub for sub in step.expect.not_contains if sub in message]
        results.append(
            AssertionResult(
                name="not_contains",
                level=level,
                passed=len(found) == 0,
                expected=f"不含 {step.expect.not_contains}",
                actual=found,
                attribution=attr,
            )
        )

    # Automatic security assertions for authz: deny (5.2.2, 5.5.2)
    if step.authz == "deny":
        g8_ok = check_g8_only_keys(response.get("raw_error_body_keys")) and not response.get(
            "has_structured_content"
        )
        results.append(
            AssertionResult(
                name="authz_g8_only_keys",
                level="must",
                passed=g8_ok,
                expected="仅含 G8 三键且无 structuredContent",
                actual={
                    "keys": response.get("raw_error_body_keys"),
                    "has_structured_content": response.get("has_structured_content"),
                },
                attribution="权限",
            )
        )
        if step.target and step.target in sentinels:
            words = list(sentinels[step.target])
            leaked = [w for w in words if w in message]
            results.append(
                AssertionResult(
                    name=f"sentinel:{step.target}",
                    level="must",
                    passed=len(leaked) == 0,
                    expected=f"不含敏感字段 {words}",
                    actual=leaked,
                    attribution="权限",
                )
            )

    return results


def evaluate_guard_step(
    step: GuardStep,
    response: dict[str, Any],
    *,
    layer: str,
    category: str,
    sentinels: Mapping[str, Sequence[str]],
    expected_data: dict[str, Any] | None = None,
    expected_message_contains: list[str] | str | None = None,
) -> list[AssertionResult]:
    """Evaluate all explicit + automatic assertions on a guard console step."""
    level = _step_level(layer, step.severity)
    attr = _tool_guard_attribution(layer, category, step.authz)
    results: list[AssertionResult] = []

    results.append(
        AssertionResult(
            name="status",
            level=level,
            passed=response.get("http_status") == step.expect.status,
            expected=step.expect.status,
            actual=response.get("http_status"),
            attribution=attr,
        )
    )

    if step.expect.error_code is not None:
        results.append(
            AssertionResult(
                name="error_code",
                level=level,
                passed=response.get("error_code") == step.expect.error_code,
                expected=step.expect.error_code,
                actual=response.get("error_code"),
                attribution=attr,
            )
        )

    data_map = expected_data if expected_data is not None else step.expect.data
    if data_map:
        results.extend(
            _evaluate_data_map(response.get("data", {}), data_map, level=level, attribution=attr)
        )

    msg_contains = (
        expected_message_contains
        if expected_message_contains is not None
        else step.expect.message_contains
    )
    message = str(response.get("message") or "")
    if msg_contains is not None:
        substrings = _normalize_str_list(msg_contains)
        missing = [sub for sub in substrings if sub not in message]
        results.append(
            AssertionResult(
                name="message_contains",
                level=level,
                passed=len(missing) == 0,
                expected=substrings,
                actual=message,
                attribution=attr,
            )
        )

    if step.expect.not_contains:
        found = [sub for sub in step.expect.not_contains if sub in message]
        results.append(
            AssertionResult(
                name="not_contains",
                level=level,
                passed=len(found) == 0,
                expected=f"不含 {step.expect.not_contains}",
                actual=found,
                attribution=attr,
            )
        )

    if step.authz == "deny":
        results.append(
            AssertionResult(
                name="authz_g8_only_keys",
                level="must",
                passed=check_g8_only_keys(response.get("raw_error_body_keys")),
                expected="仅含 G8 三键",
                actual=response.get("raw_error_body_keys"),
                attribution="权限",
            )
        )
        if step.target and step.target in sentinels:
            words = list(sentinels[step.target])
            leaked = [w for w in words if w in message]
            results.append(
                AssertionResult(
                    name=f"sentinel:{step.target}",
                    level="must",
                    passed=len(leaked) == 0,
                    expected=f"不含敏感字段 {words}",
                    actual=leaked,
                    attribution="权限",
                )
            )

    return results


def _evaluate_chat_block(
    block: ChatAssertionBlock,
    *,
    level: str,
    step: ChatStep,
    category: str,
    response: dict[str, Any],
    prev_conversation_id: str | None,
    judge_fn: JudgeCallback | None,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    answer = str(response.get("answer") or "")
    trace = response.get("trace") or []
    citations = response.get("citations") or []

    if block.trace is not None:
        ok = evaluate_trace_expect(trace, block.trace)
        results.append(
            AssertionResult(
                name="trace",
                level=level,
                passed=ok,
                expected=block.trace.model_dump(exclude_none=True),
                actual=trace,
                attribution="路由",
            )
        )

    if block.trace_forbid is not None:
        violations = evaluate_trace_forbid(trace, block.trace_forbid)
        attr = "权限" if step.authz is not None else "路由"
        results.append(
            AssertionResult(
                name="trace_forbid",
                level=level,
                passed=len(violations) == 0,
                expected=[item.model_dump(exclude_none=True) for item in block.trace_forbid],
                actual=violations,
                attribution=attr,
            )
        )

    if block.answer_equals is not None:
        stripped = answer.strip()
        results.append(
            AssertionResult(
                name="answer_equals",
                level=level,
                passed=stripped == block.answer_equals.strip(),
                expected=block.answer_equals.strip(),
                actual=stripped,
                attribution="模板",
            )
        )

    if block.answer_in is not None:
        stripped = answer.strip()
        allowed = [item.strip() for item in block.answer_in]
        results.append(
            AssertionResult(
                name="answer_in",
                level=level,
                passed=stripped in allowed,
                expected=allowed,
                actual=stripped,
                attribution="模板",
            )
        )

    text_attr = "生成" if category in ("policy", "kbqa") else "模板"

    if block.contains_all is not None:
        missing = [sub for sub in block.contains_all if sub not in answer]
        results.append(
            AssertionResult(
                name="contains_all",
                level=level,
                passed=len(missing) == 0,
                expected=block.contains_all,
                actual=missing if missing else "全部命中",
                attribution=text_attr,
            )
        )

    if block.contains_any is not None:
        hit = any(sub in answer for sub in block.contains_any)
        results.append(
            AssertionResult(
                name="contains_any",
                level=level,
                passed=hit,
                expected=block.contains_any,
                actual=answer if not hit else "已命中",
                attribution=text_attr,
            )
        )

    if block.not_contains is not None:
        found = [sub for sub in block.not_contains if sub in answer]
        attr = "权限" if step.authz is not None else "模板"
        results.append(
            AssertionResult(
                name="not_contains",
                level=level,
                passed=len(found) == 0,
                expected=f"不含 {block.not_contains}",
                actual=found,
                attribution=attr,
            )
        )

    if block.no_jwt:
        has_jwt = contains_jwt_like(answer) or REDACTED_TOKEN in answer
        results.append(
            AssertionResult(
                name="no_jwt",
                level=level,
                passed=not has_jwt,
                expected="回答不含 JWT 形态字符串",
                actual="检测到 JWT 形态字符串" if has_jwt else "未检测到",
                attribution="权限",
            )
        )

    if block.clauses is not None:
        missing_refs = [
            f"{ref.doc} 第 {ref.clause} 条"
            for ref in block.clauses
            if not match_clause(answer, ref.doc, ref.clause)
        ]
        # Attribution per 5.3.2: missing in citations -> 检索, else 生成
        if missing_refs:
            all_cited = all(
                match_citation(citations, ref.doc, ref.clause)
                for ref in block.clauses
                if not match_clause(answer, ref.doc, ref.clause)
            )
            clause_attr = "生成" if all_cited else "检索"
        else:
            clause_attr = "生成"
        results.append(
            AssertionResult(
                name="clauses",
                level=level,
                passed=len(missing_refs) == 0,
                expected=[f"{ref.doc} 第 {ref.clause} 条" for ref in block.clauses],
                actual=missing_refs if missing_refs else "全部命中",
                attribution=clause_attr,
            )
        )

    if block.clauses_any is not None:
        matched_any = any(match_clause(answer, ref.doc, ref.clause) for ref in block.clauses_any)
        results.append(
            AssertionResult(
                name="clauses_any",
                level=level,
                passed=matched_any,
                expected=[f"{ref.doc} 第 {ref.clause} 条" for ref in block.clauses_any],
                actual="命中" if matched_any else "未命中任一条款",
                attribution="生成",
            )
        )

    if block.citations is not None:
        missing_cites = [
            f"{ref.doc} 第 {ref.clause} 条"
            for ref in block.citations
            if not match_citation(citations, ref.doc, ref.clause)
        ]
        results.append(
            AssertionResult(
                name="citations",
                level=level,
                passed=len(missing_cites) == 0,
                expected=[f"{ref.doc} 第 {ref.clause} 条" for ref in block.citations],
                actual=missing_cites if missing_cites else "全部命中",
                attribution="检索",
            )
        )

    if block.verdict is not None:
        rule_label = classify_verdict_by_rules(answer)
        if rule_label != "unknown":
            results.append(
                AssertionResult(
                    name="verdict",
                    level=level,
                    passed=rule_label == block.verdict,
                    expected=block.verdict,
                    actual=rule_label,
                    attribution="生成",
                    verdict_source="rule",
                )
            )
        else:
            judge_res = judge_fn(step.query, answer) if judge_fn is not None else None
            if judge_res is not None and judge_res.status == "ok":
                passed = judge_res.label == block.verdict
                results.append(
                    AssertionResult(
                        name="verdict",
                        level=level,
                        passed=passed,
                        expected=block.verdict,
                        actual=judge_res.label,
                        attribution="生成",
                        needs_manual_review=judge_res.label == "unclear",
                        verdict_source="judge",
                        judge_record=judge_res.to_dict(),
                    )
                )
            else:
                v_source = (
                    "judge_invalid"
                    if judge_res is not None and judge_res.status == "invalid"
                    else "unknown"
                )
                results.append(
                    AssertionResult(
                        name="verdict",
                        level=level,
                        passed=False,
                        expected=block.verdict,
                        actual="unknown",
                        attribution="生成",
                        needs_manual_review=True,
                        verdict_source=v_source,
                        judge_record=judge_res.to_dict() if judge_res is not None else None,
                    )
                )

    if block.conversation_changed is not None:
        curr_conv = str(response.get("conversation_id") or "")
        if block.conversation_changed and not prev_conversation_id:
            results.append(
                AssertionResult(
                    name="conversation_changed",
                    level=level,
                    passed=False,
                    expected=True,
                    actual="前置步骤未取得会话 ID",
                    attribution="权限",
                )
            )
        else:
            changed = bool(curr_conv) and curr_conv != (prev_conversation_id or "")
            results.append(
                AssertionResult(
                    name="conversation_changed",
                    level=level,
                    passed=changed == block.conversation_changed,
                    expected=block.conversation_changed,
                    actual=changed,
                    attribution="权限",
                )
            )

    return results


def evaluate_chat_step(
    step: ChatStep,
    response: dict[str, Any],
    *,
    category: str,
    sentinels: Mapping[str, Sequence[str]],
    issued_tokens: Sequence[str],
    prev_conversation_id: str | None = None,
    judge_fn: JudgeCallback | None = None,
    resolved_must: ChatAssertionBlock | None = None,
    resolved_should: ChatAssertionBlock | None = None,
    prior_token_leak: Mapping[str, Any] | None = None,
) -> list[AssertionResult]:
    """Evaluate all explicit + automatic assertions on a chat step (5.5.2)."""
    results: list[AssertionResult] = []

    # Auto-assertion on EVERY chat response: no issued token or JWT leaked (5.5.2, M11)
    leak_payload = {
        "answer": response.get("answer", ""),
        "citations": response.get("citations", []),
        "trace": response.get("trace", []),
        "message": response.get("message", ""),
    }
    leaked_tokens = find_leaked_tokens(leak_payload, issued_tokens)
    serialized_leak = json.dumps(leak_payload, ensure_ascii=False)
    if contains_jwt_like(serialized_leak):
        leaked_tokens.append("jwt_pattern")
    if REDACTED_TOKEN in serialized_leak:
        leaked_tokens.append("redacted_token")

    if prior_token_leak is not None and not prior_token_leak.get("passed", True):
        tl_passed = False
        tl_actual: Any = prior_token_leak.get("actual") or leaked_tokens or ["token_leak"]
    else:
        tl_passed = len(leaked_tokens) == 0
        tl_actual = leaked_tokens if leaked_tokens else "未泄露"

    results.append(
        AssertionResult(
            name="token_leak",
            level="must",
            passed=tl_passed,
            expected="响应中不出现本次运行用过的任何 token",
            actual=tl_actual,
            attribution="权限",
        )
    )

    if step.expect.http is not None:
        results.append(
            AssertionResult(
                name="http_status",
                level="must",
                passed=response.get("http_status") == step.expect.http.status,
                expected=step.expect.http.status,
                actual=response.get("http_status"),
                attribution="权限",
            )
        )
        if step.expect.http.code is not None:
            results.append(
                AssertionResult(
                    name="http_code",
                    level="must",
                    passed=response.get("error_code") == step.expect.http.code,
                    expected=step.expect.http.code,
                    actual=response.get("error_code"),
                    attribution="权限",
                )
            )
        return results

    # Normal 200 chat response check
    if response.get("http_status") != 200:
        results.append(
            AssertionResult(
                name="http_status",
                level="must",
                passed=False,
                expected=200,
                actual=response.get("http_status"),
                attribution="环境",
            )
        )
        return results

    # Auto-assertion for authz: deny sentinel words (5.5.2)
    if step.authz == "deny" and step.target and step.target in sentinels:
        words = list(sentinels[step.target])
        answer = str(response.get("answer") or "")
        leaked = [w for w in words if w in answer]
        results.append(
            AssertionResult(
                name=f"sentinel:{step.target}",
                level="must",
                passed=len(leaked) == 0,
                expected=f"回答不含敏感字段 {words}",
                actual=leaked,
                attribution="权限",
            )
        )

    # L10: skip LLM judge when token_leak already failed; otherwise redact query & answer first
    step_judge_fn: JudgeCallback | None = None
    if tl_passed and judge_fn is not None:

        def _safe_judge(q: str, a: str) -> JudgeVerdictResult:
            return judge_fn(
                redact_tokens(q, issued_tokens),
                redact_tokens(a, issued_tokens),
            )

        step_judge_fn = _safe_judge

    must_blk = resolved_must if resolved_must is not None else step.expect.must
    if must_blk is not None:
        results.extend(
            _evaluate_chat_block(
                must_blk,
                level="must",
                step=step,
                category=category,
                response=response,
                prev_conversation_id=prev_conversation_id,
                judge_fn=step_judge_fn,
            )
        )

    should_blk = resolved_should if resolved_should is not None else step.expect.should
    if should_blk is not None:
        results.extend(
            _evaluate_chat_block(
                should_blk,
                level="should",
                step=step,
                category=category,
                response=response,
                prev_conversation_id=prev_conversation_id,
                judge_fn=step_judge_fn,
            )
        )

    return results
