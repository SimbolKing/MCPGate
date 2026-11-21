"""Audit ledger and reconciliation rules R1–R6 with order_id backfill (5.6)."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from harness.schema import AccountSpec

KIND_TO_OUTCOME: dict[str, str] = {
    "ok": "success",
    "pending": "pending_approval",
    "denied": "access_denied",
    "not_found": "not_found",
    "conflict": "conflict",
    "invalid": "invalid",
}

GUARD_OWN_APPROVE_CODES: frozenset[str] = frozenset(
    {
        "insufficient_role",
        "approval_access_denied",
        "self_approval_forbidden",
        "approval_not_pending",
        "validation_error",
    }
)


def status_code_to_outcome(status_code: int | None, *, pending_approval: bool = False) -> str:
    """Map HTTP status code to guard audit outcome per 3.9.3."""
    if pending_approval:
        return "pending_approval"
    if status_code is None:
        return "error"
    if 200 <= status_code < 300:
        return "success"
    if status_code == 401:
        return "auth_failed"
    if status_code == 403:
        return "access_denied"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if 400 <= status_code < 500:
        return "invalid"
    return "error"


@dataclass
class LedgerEntry:
    """One auditable call recorded by the harness during execution (5.6.1)."""

    step_id: str
    # source: mcp | chat_trace | guard_approve | guard_reject | guard_order | unauthenticated_401
    source: str
    actor: str
    event_type: str
    channel: str
    tool: str
    expected_outcome: str
    expected_status_code: int | None = None
    expected_error_code: str | None = None
    request_id: str | None = None
    order_id: str | None = None
    approval_id: str | None = None
    applicant_sub: str | None = None
    approval_status_after: str | None = None
    refund_id_after: str | None = None
    trace_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        return cls(**data)


@dataclass
class AuditIssue:
    rule: str  # "R1".."R6"
    message: str
    step_id: str | None = None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalChainSummary:
    approval_id: str
    passed: bool
    replayed_status: str | None
    actual_status: str | None
    event_count: int
    issues: list[str] = field(default_factory=list)
    applicant_sub: str | None = None
    approver_sub: str | None = None
    refund_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditReconciliationResult:
    passed: bool
    expected_count: int
    matched_count: int
    unexpected_count: int
    issues: list[AuditIssue] = field(default_factory=list)
    backfilled_order_ids: dict[str, str] = field(default_factory=dict)  # "step_id:idx" -> order_id
    access_denied_expected: dict[str, int] = field(default_factory=dict)
    access_denied_actual: dict[str, int] = field(default_factory=dict)
    approval_chains: list[ApprovalChainSummary] = field(default_factory=list)
    record_matches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "expected_count": self.expected_count,
            "matched_count": self.matched_count,
            "unexpected_count": self.unexpected_count,
            "issues": [i.to_dict() for i in self.issues],
            "backfilled_order_ids": dict(self.backfilled_order_ids),
            "access_denied_expected": dict(self.access_denied_expected),
            "access_denied_actual": dict(self.access_denied_actual),
            "approval_chains": [c.to_dict() for c in self.approval_chains],
            "record_matches": list(self.record_matches),
        }


def _path_matches_template(actual_path: str | None, template_path: str) -> bool:
    if not actual_path:
        return False
    pattern = "^" + re.sub(r"\{[^/]+\}", "[^/]+", template_path) + "$"
    return bool(re.match(pattern, actual_path))


def _expand_approve_entry(
    entry: LedgerEntry,
    step_records: Sequence[dict[str, Any]],
) -> list[LedgerEntry]:
    """Expand a guard_approve ledger entry into 0, 1, or 2 expected audit entries per 5.6.1."""
    sc = entry.expected_status_code
    ec = entry.expected_error_code

    if sc == 200:
        approve_ev = LedgerEntry(
            step_id=entry.step_id,
            source="guard_approve_decision",
            actor=entry.actor,
            event_type="approval_approve",
            channel="console",
            tool="create_refund",
            expected_outcome="success",
            expected_status_code=200,
            request_id=entry.request_id,
            approval_id=entry.approval_id,
        )
        exec_ev = LedgerEntry(
            step_id=entry.step_id,
            source="guard_approve_execute",
            actor=entry.actor,
            event_type="approval_execute",
            channel="console",
            tool="create_refund",
            expected_outcome="*",  # determined by approval_status_after
            expected_status_code=None,
            request_id=entry.request_id,
            approval_id=entry.approval_id,
            applicant_sub=entry.applicant_sub,
            approval_status_after=entry.approval_status_after,
            refund_id_after=entry.refund_id_after,
        )
        return [approve_ev, exec_ev]

    if sc in (403, 409, 422) and ec in GUARD_OWN_APPROVE_CODES:
        return [
            LedgerEntry(
                step_id=entry.step_id,
                source="guard_approve_denied",
                actor=entry.actor,
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                expected_outcome=status_code_to_outcome(sc),
                expected_status_code=sc,
                expected_error_code=ec,
                request_id=entry.request_id,
                approval_id=entry.approval_id,
            )
        ]

    if sc == 403 and ec == "order_access_denied":
        return [
            LedgerEntry(
                step_id=entry.step_id,
                source="guard_approve_decision",
                actor=entry.actor,
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                expected_outcome="success",
                expected_status_code=200,
                request_id=entry.request_id,
                approval_id=entry.approval_id,
            ),
            LedgerEntry(
                step_id=entry.step_id,
                source="guard_approve_execute",
                actor=entry.actor,
                event_type="approval_execute",
                channel="console",
                tool="create_refund",
                expected_outcome="access_denied",
                expected_status_code=403,
                expected_error_code="order_access_denied",
                request_id=entry.request_id,
                approval_id=entry.approval_id,
                applicant_sub=entry.applicant_sub,
                approval_status_after="pending",
            ),
        ]

    if sc == 401 or (sc == 403 and ec == "invalid_claims"):
        matching = [r for r in step_records if r.get("request_id") == entry.request_id]
        if not matching:
            return []
        return [
            LedgerEntry(
                step_id=entry.step_id,
                source="guard_approve_decision",
                actor=entry.actor,
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                expected_outcome="success",
                expected_status_code=200,
                request_id=entry.request_id,
                approval_id=entry.approval_id,
            ),
            LedgerEntry(
                step_id=entry.step_id,
                source="guard_approve_execute",
                actor=entry.actor,
                event_type="approval_execute",
                channel="console",
                tool="create_refund",
                expected_outcome=status_code_to_outcome(sc),
                expected_status_code=sc,
                expected_error_code=ec,
                request_id=entry.request_id,
                approval_id=entry.approval_id,
                applicant_sub=entry.applicant_sub,
                approval_status_after="pending",
            ),
        ]

    # 404 on nonexistent approval does not produce an audit record (3.9.2)
    return []


def _compare_entry_and_record(
    entry: LedgerEntry,
    record: dict[str, Any],
    accounts: Mapping[str, AccountSpec],
) -> list[str]:
    mismatches: list[str] = []
    acc = accounts.get(entry.actor)
    if acc is not None and record.get("actor_sub") != acc.sub:
        mismatches.append(f"actor_sub 期望 {acc.sub}，实际 {record.get('actor_sub')}")
    if record.get("event_type") != entry.event_type:
        mismatches.append(f"event_type 期望 {entry.event_type}，实际 {record.get('event_type')}")
    if record.get("channel") != entry.channel:
        mismatches.append(f"channel 期望 {entry.channel}，实际 {record.get('channel')}")
    if record.get("tool") != entry.tool:
        mismatches.append(f"tool 期望 {entry.tool}，实际 {record.get('tool')}")

    if entry.expected_outcome != "*" and record.get("outcome") != entry.expected_outcome:
        mismatches.append(f"outcome 期望 {entry.expected_outcome}，实际 {record.get('outcome')}")
    if (
        entry.expected_status_code is not None
        and record.get("status_code") != entry.expected_status_code
    ):
        mismatches.append(
            f"status_code 期望 {entry.expected_status_code}，实际 {record.get('status_code')}"
        )
    if (
        entry.expected_error_code is not None
        and record.get("error_code") != entry.expected_error_code
    ):
        mismatches.append(
            f"error_code 期望 {entry.expected_error_code}，实际 {record.get('error_code')}"
        )
    if entry.order_id is not None and record.get("order_id") != entry.order_id:
        mismatches.append(f"order_id 期望 {entry.order_id}，实际 {record.get('order_id')}")
    if entry.approval_id is not None and record.get("approval_id") != entry.approval_id:
        mismatches.append(f"approval_id 期望 {entry.approval_id}，实际 {record.get('approval_id')}")

    if entry.event_type == "approval_execute":
        if entry.applicant_sub and record.get("on_behalf_of_sub") != entry.applicant_sub:
            actual_obo = record.get("on_behalf_of_sub")
            mismatches.append(f"on_behalf_of_sub 期望 {entry.applicant_sub}，实际 {actual_obo}")
        res_obj = record.get("result") or {}
        if entry.approval_status_after and res_obj.get("to") != entry.approval_status_after:
            mismatches.append(
                f"迁移终点期望 {entry.approval_status_after}，实际 {res_obj.get('to')}"
            )
        if (
            entry.approval_status_after == "executed"
            and entry.refund_id_after
            and res_obj.get("refund_id") != entry.refund_id_after
        ):
            mismatches.append(
                f"result.refund_id 期望 {entry.refund_id_after}，实际 {res_obj.get('refund_id')}"
            )

    return mismatches


def _check_r3_approval_chains(
    all_records: Sequence[dict[str, Any]],
    final_approvals: Mapping[str, dict[str, Any]],
) -> tuple[list[ApprovalChainSummary], list[AuditIssue]]:
    issues: list[AuditIssue] = []
    summaries: list[ApprovalChainSummary] = []

    by_approval: dict[str, list[dict[str, Any]]] = {}
    for rec in all_records:
        aid = rec.get("approval_id")
        if not aid:
            continue
        ev_type = rec.get("event_type")
        is_submit = ev_type == "tool_call" and rec.get("outcome") == "pending_approval"
        if is_submit or (isinstance(ev_type, str) and ev_type.startswith("approval_")):
            by_approval.setdefault(str(aid), []).append(rec)

    all_aids = sorted(set(by_approval.keys()) | set(final_approvals.keys()))
    for aid in all_aids:
        events = sorted(by_approval.get(aid, []), key=lambda r: int(r["id"]))
        chain_issues: list[str] = []
        actual_ap = final_approvals.get(aid)
        actual_status = str(actual_ap["status"]) if actual_ap else None

        if not events:
            ap_info = final_approvals[aid]
            chain_issues.append(f"审批单 {aid} 在审计中缺少任何提交或审批记录")
            summaries.append(
                ApprovalChainSummary(
                    aid,
                    False,
                    None,
                    actual_status,
                    0,
                    chain_issues,
                    applicant_sub=ap_info.get("applicant_sub"),
                    approver_sub=ap_info.get("decided_by_sub"),
                    refund_id=ap_info.get("refund_id"),
                )
            )
            issues.append(AuditIssue("R3", chain_issues[0], approval_id=aid))
            continue

        first = events[0]
        if not (
            first.get("event_type") == "tool_call" and first.get("outcome") == "pending_approval"
        ):
            chain_issues.append(f"审批单 {aid} 首条记录不是提交记录 (pending_approval)")
        applicant_sub = str(first.get("actor_sub") or "")
        approver_sub: str | None = None
        state = "pending"
        last_refund_id: str | None = None
        exec_2xx_count = 0
        pending_exec_req_id: str | None = None
        pending_exec_actor: str | None = None

        for rec in events[1:]:
            ev_type = rec.get("event_type")
            sc = rec.get("status_code")
            res = rec.get("result")

            if pending_exec_req_id is not None:
                if (
                    ev_type != "approval_execute"
                    or rec.get("request_id") != pending_exec_req_id
                    or rec.get("actor_sub") != pending_exec_actor
                ):
                    chain_issues.append(
                        f"审批单 {aid} 在 200 同意后未紧跟同 request_id/同操作人的执行记录"
                    )
                pending_exec_req_id = None
                pending_exec_actor = None

            if ev_type == "approval_approve":
                if sc == 200:
                    if not (
                        isinstance(res, dict)
                        and res.get("from") == "pending"
                        and res.get("to") == "executing"
                    ):
                        chain_issues.append(f"审批单 {aid} 同意成功记录缺少 pending→executing 迁移")
                    state = "executing"
                    approver_sub = rec.get("actor_sub") or approver_sub
                    pending_exec_req_id = str(rec.get("request_id") or "")
                    pending_exec_actor = str(rec.get("actor_sub") or "")
                elif sc in (403, 409, 422):
                    if isinstance(res, dict) and "to" in res:
                        chain_issues.append(f"审批单 {aid} 被拒的同意尝试不应包含状态迁移")
                else:
                    chain_issues.append(f"审批单 {aid} 同意记录状态码异常: {sc}")

            elif ev_type == "approval_execute":
                if rec.get("on_behalf_of_sub") != applicant_sub:
                    chain_issues.append(
                        f"审批单 {aid} 执行记录的 on_behalf_of_sub "
                        f"({rec.get('on_behalf_of_sub')}) 不是申请人 ({applicant_sub})"
                    )
                if sc is not None and 200 <= sc < 300:
                    exec_2xx_count += 1
                    if not (
                        isinstance(res, dict)
                        and res.get("to") == "executed"
                        and res.get("refund_id")
                    ):
                        chain_issues.append(
                            f"审批单 {aid} 2xx 执行记录缺少 executed 迁移或 refund_id"
                        )
                    else:
                        last_refund_id = str(res["refund_id"])
                    state = "executed"
                elif sc in (401, 403):
                    to_st = res.get("to") if isinstance(res, dict) else None
                    if to_st not in ("pending", "executing"):
                        chain_issues.append(f"审批单 {aid} 401/403 执行记录迁移终点异常: {to_st}")
                    else:
                        state = str(to_st)
                elif sc is not None and 400 <= sc < 500:
                    if not (isinstance(res, dict) and res.get("to") == "execution_failed"):
                        chain_issues.append(f"审批单 {aid} 4xx 执行记录缺少 execution_failed 迁移")
                    state = "execution_failed"
                else:
                    if not (isinstance(res, dict) and res.get("to") == "executing"):
                        chain_issues.append(f"审批单 {aid} 5xx/无应答执行记录缺少 executing 迁移")
                    state = "executing"

            elif ev_type == "approval_reject":
                if sc == 200:
                    if not (isinstance(res, dict) and res.get("to") == "rejected"):
                        chain_issues.append(f"审批单 {aid} 拒绝成功记录缺少 rejected 迁移")
                    state = "rejected"
                    approver_sub = rec.get("actor_sub") or approver_sub
                elif sc in (403, 409, 422):
                    if isinstance(res, dict) and "to" in res:
                        chain_issues.append(f"审批单 {aid} 被拒的拒绝尝试不应包含状态迁移")
                else:
                    chain_issues.append(f"审批单 {aid} 拒绝记录状态码异常: {sc}")

            elif ev_type == "approval_expire":
                if not (isinstance(res, dict) and res.get("to") == "expired"):
                    chain_issues.append(f"审批单 {aid} 过期记录缺少 expired 迁移")
                state = "expired"

            else:
                chain_issues.append(f"审批单 {aid} 出现意外事件类型: {ev_type}")

        if pending_exec_req_id is not None:
            chain_issues.append(f"审批单 {aid} 200 同意记录后缺少执行记录")
        if exec_2xx_count > 1:
            chain_issues.append(f"审批单 {aid} 出现 {exec_2xx_count} 条 2xx 执行记录（至多 1 条）")

        final_applicant: str | None = applicant_sub or None
        final_approver: str | None = approver_sub
        final_refund: str | None = last_refund_id
        if actual_ap is not None:
            final_applicant = actual_ap.get("applicant_sub") or final_applicant
            final_approver = actual_ap.get("decided_by_sub") or final_approver
            final_refund = actual_ap.get("refund_id") or final_refund
            if state != actual_status:
                chain_issues.append(
                    f"审批单 {aid} 审计重放状态 ({state}) 与探针状态 ({actual_status}) 不一致"
                )
            if state == "executed" and last_refund_id != actual_ap.get("refund_id"):
                chain_issues.append(
                    f"审批单 {aid} 执行记录 refund_id ({last_refund_id}) "
                    f"与审批单 ({actual_ap.get('refund_id')}) 不一致"
                )

        passed = len(chain_issues) == 0
        summaries.append(
            ApprovalChainSummary(
                aid,
                passed,
                state,
                actual_status,
                len(events),
                chain_issues,
                applicant_sub=final_applicant,
                approver_sub=final_approver,
                refund_id=final_refund,
            )
        )
        for msg in chain_issues:
            issues.append(AuditIssue("R3", msg, approval_id=aid))

    return summaries, issues


def _check_r5_record_fields(
    entry: LedgerEntry,
    record: dict[str, Any],
    accounts: Mapping[str, AccountSpec],
    tools_manifest: Mapping[str, dict[str, Any]],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    acc = accounts.get(entry.actor)
    if acc is not None:
        if (
            record.get("actor_sub") != acc.sub
            or record.get("actor_role") != acc.role
            or record.get("actor_regions") != acc.regions_csv
        ):
            issues.append(
                AuditIssue(
                    "R5",
                    f"步骤 {entry.step_id} 审计记录账号字段与 {entry.actor} 不一致",
                    step_id=entry.step_id,
                )
            )

    if record.get("event_type") == "tool_call":
        tool_name = str(record.get("tool") or "")
        spec = tools_manifest.get(tool_name)
        if spec is None:
            issues.append(
                AuditIssue(
                    "R5",
                    f"步骤 {entry.step_id} tool_call 的 tool '{tool_name}' 不在工具清单中",
                    step_id=entry.step_id,
                )
            )
        else:
            if record.get("http_method") != spec["method"] or not _path_matches_template(
                record.get("http_path"), str(spec["path"])
            ):
                issues.append(
                    AuditIssue(
                        "R5",
                        f"步骤 {entry.step_id} tool '{tool_name}' 的 method/path "
                        f"({record.get('http_method')} {record.get('http_path')}) 与清单不一致",
                        step_id=entry.step_id,
                    )
                )

    sc = record.get("status_code")
    outcome = record.get("outcome")
    error_code = record.get("error_code")
    expected_outcome = status_code_to_outcome(sc, pending_approval=(outcome == "pending_approval"))
    if outcome == "pending_approval":
        if sc != 202 or error_code is not None:
            issues.append(
                AuditIssue(
                    "R5",
                    f"步骤 {entry.step_id} pending_approval 应对应 status_code=202 且无 error_code",
                    step_id=entry.step_id,
                )
            )
    elif outcome != expected_outcome:
        issues.append(
            AuditIssue(
                "R5",
                f"步骤 {entry.step_id} status_code={sc} 与 outcome={outcome} 不符 3.9.3 映射",
                step_id=entry.step_id,
            )
        )
    elif sc is not None and 400 <= sc < 500 and not error_code:
        issues.append(
            AuditIssue(
                "R5",
                f"步骤 {entry.step_id} 4xx 审计记录 ({sc}) 缺少 error_code",
                step_id=entry.step_id,
            )
        )

    return issues


def reconcile_case_audit(
    *,
    step_order: Sequence[str],
    step_ledgers: Mapping[str, Sequence[LedgerEntry]],
    step_audits: Mapping[str, Sequence[dict[str, Any]]],
    accounts: Mapping[str, AccountSpec],
    tools_manifest: Mapping[str, dict[str, Any]],
    final_approvals: Mapping[str, dict[str, Any]],
    new_refund_ids: Sequence[str],
    issued_tokens: Sequence[str],
) -> AuditReconciliationResult:
    """Execute R1–R6 audit reconciliation and order_id backfill for a single case run (5.6.2)."""
    issues: list[AuditIssue] = []
    backfilled: dict[str, str] = {}
    all_records: list[dict[str, Any]] = []
    all_expanded_entries: list[LedgerEntry] = []
    record_matches: list[dict[str, Any]] = []

    expected_count = 0
    matched_count = 0
    unexpected_count = 0

    # R1: Step-level 1-to-1 matching
    for step_id in step_order:
        raw_entries = list(step_ledgers.get(step_id, []))
        records = sorted(step_audits.get(step_id, []), key=lambda r: int(r["id"]))
        all_records.extend(records)

        expanded: list[LedgerEntry] = []
        for entry in raw_entries:
            if entry.source == "unauthenticated_401":
                continue
            if entry.source == "guard_approve":
                expanded.extend(_expand_approve_entry(entry, records))
            else:
                expanded.append(entry)

        all_expanded_entries.extend(expanded)
        expected_count += len(expanded)

        used_rec_indices: set[int] = set()
        unmatched_no_reqid: list[LedgerEntry] = []

        # Pass 1: exact match by request_id (+ event_type)
        for entry in expanded:
            if not entry.request_id:
                unmatched_no_reqid.append(entry)
                continue
            found_idx: int | None = None
            for idx, rec in enumerate(records):
                if idx in used_rec_indices:
                    continue
                if (
                    rec.get("request_id") == entry.request_id
                    and rec.get("event_type") == entry.event_type
                ):
                    found_idx = idx
                    break
            if found_idx is None:
                issues.append(
                    AuditIssue(
                        "R1",
                        f"步骤 {step_id} 缺少匹配 request_id={entry.request_id} "
                        f"({entry.event_type}) 的审计记录",
                        step_id=step_id,
                    )
                )
                record_matches.append(
                    {
                        "step_id": step_id,
                        "status": "missing",
                        "match_label": "缺失",
                        "audit_id": None,
                        "ledger": entry.to_dict(),
                        "record": None,
                        "diffs": ["缺失对应审计记录"],
                    }
                )
                continue
            used_rec_indices.add(found_idx)
            rec = records[found_idx]
            diffs = _compare_entry_and_record(entry, rec, accounts)
            if diffs:
                issues.append(
                    AuditIssue(
                        "R1", f"步骤 {step_id} 字段不符: {'; '.join(diffs)}", step_id=step_id
                    )
                )
                record_matches.append(
                    {
                        "step_id": step_id,
                        "status": "mismatch",
                        "match_label": "字段不符",
                        "audit_id": rec.get("id"),
                        "ledger": entry.to_dict(),
                        "record": rec,
                        "diffs": diffs,
                    }
                )
            else:
                matched_count += 1
                record_matches.append(
                    {
                        "step_id": step_id,
                        "status": "matched",
                        "match_label": "匹配",
                        "audit_id": rec.get("id"),
                        "ledger": entry.to_dict(),
                        "record": rec,
                        "diffs": [],
                    }
                )
            if (
                entry.source == "chat_trace"
                and entry.trace_index is not None
                and rec.get("order_id")
            ):
                backfilled[f"{step_id}:{entry.trace_index}"] = str(rec["order_id"])
            issues.extend(_check_r5_record_fields(entry, rec, accounts, tools_manifest))

        # Pass 2: match entries without request_id sequentially against remaining records by id asc
        remaining_records = [rec for idx, rec in enumerate(records) if idx not in used_rec_indices]
        pair_count = min(len(unmatched_no_reqid), len(remaining_records))
        for i in range(pair_count):
            entry = unmatched_no_reqid[i]
            rec = remaining_records[i]
            diffs = _compare_entry_and_record(entry, rec, accounts)
            if diffs:
                issues.append(
                    AuditIssue(
                        "R1", f"步骤 {step_id} 字段不符: {'; '.join(diffs)}", step_id=step_id
                    )
                )
                record_matches.append(
                    {
                        "step_id": step_id,
                        "status": "mismatch",
                        "match_label": "字段不符",
                        "audit_id": rec.get("id"),
                        "ledger": entry.to_dict(),
                        "record": rec,
                        "diffs": diffs,
                    }
                )
            else:
                matched_count += 1
                record_matches.append(
                    {
                        "step_id": step_id,
                        "status": "matched",
                        "match_label": "匹配",
                        "audit_id": rec.get("id"),
                        "ledger": entry.to_dict(),
                        "record": rec,
                        "diffs": [],
                    }
                )
            if (
                entry.source == "chat_trace"
                and entry.trace_index is not None
                and rec.get("order_id")
            ):
                backfilled[f"{step_id}:{entry.trace_index}"] = str(rec["order_id"])
            issues.extend(_check_r5_record_fields(entry, rec, accounts, tools_manifest))

        for entry in unmatched_no_reqid[pair_count:]:
            issues.append(
                AuditIssue(
                    "R1",
                    f"步骤 {step_id} 缺少工具调用 {entry.tool} "
                    f"({entry.expected_outcome}) 的审计记录",
                    step_id=step_id,
                )
            )
            record_matches.append(
                {
                    "step_id": step_id,
                    "status": "missing",
                    "match_label": "缺失",
                    "audit_id": None,
                    "ledger": entry.to_dict(),
                    "record": None,
                    "diffs": ["缺失对应审计记录"],
                }
            )
        extra = remaining_records[pair_count:]
        if extra:
            unexpected_count += len(extra)
            extra_ids = [r["id"] for r in extra]
            issues.append(
                AuditIssue(
                    "R1",
                    f"步骤 {step_id} 存在 {len(extra)} 条多余审计记录 (ids={extra_ids})",
                    step_id=step_id,
                )
            )
            for rec in extra:
                record_matches.append(
                    {
                        "step_id": step_id,
                        "status": "unexpected",
                        "match_label": "多余",
                        "audit_id": rec.get("id"),
                        "ledger": None,
                        "record": rec,
                        "diffs": ["多余审计记录"],
                    }
                )

    # R2: access_denied total and breakdown by error_code
    exp_denied = Counter(
        str(e.expected_error_code or "")
        for e in all_expanded_entries
        if e.expected_outcome == "access_denied"
    )
    act_denied = Counter(
        str(r.get("error_code") or "") for r in all_records if r.get("outcome") == "access_denied"
    )
    if exp_denied != act_denied:
        issues.append(
            AuditIssue(
                "R2",
                f"越权拒绝计数不符: 期望 {dict(exp_denied)}，实际 {dict(act_denied)}",
            )
        )

    # R3: Approval chain replay
    chain_summaries, r3_issues = _check_r3_approval_chains(all_records, final_approvals)
    issues.extend(r3_issues)

    # R4: 2xx approval_execute records match new refunds
    exec_2xx_refund_ids = sorted(
        str((r.get("result") or {}).get("refund_id") or "")
        for r in all_records
        if r.get("event_type") == "approval_execute"
        and r.get("status_code") is not None
        and 200 <= int(r["status_code"]) < 300
    )
    probe_new_refund_ids = sorted(str(rid) for rid in new_refund_ids)
    if exec_2xx_refund_ids != probe_new_refund_ids:
        issues.append(
            AuditIssue(
                "R4",
                f"2xx 执行记录退款单 {exec_2xx_refund_ids} "
                f"与探针新增退款单 {probe_new_refund_ids} 不符",
            )
        )

    # R6: No issued token or JWT signature segment in serialized audit records
    serialized_records = json.dumps(all_records, ensure_ascii=False)
    for idx, token in enumerate(issued_tokens, start=1):
        if not token:
            continue
        parts = token.split(".")
        sig = parts[2] if len(parts) == 3 and len(parts[2]) >= 16 else ""
        if token in serialized_records or (sig and sig in serialized_records):
            issues.append(AuditIssue("R6", f"审计记录中检测到敏感凭证泄露 (token#{idx})"))

    return AuditReconciliationResult(
        passed=len(issues) == 0 and unexpected_count == 0 and matched_count == expected_count,
        expected_count=expected_count,
        matched_count=matched_count,
        unexpected_count=unexpected_count,
        issues=issues,
        backfilled_order_ids=backfilled,
        access_denied_expected=dict(exp_denied),
        access_denied_actual=dict(act_denied),
        approval_chains=chain_summaries,
        record_matches=record_matches,
    )
