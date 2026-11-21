"""Sequential evaluation runner, variable interpolation, JSONL persistence, and rescoring."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jmespath

from harness import asserts as asserts_mod
from harness.asserts import (
    REDACTED_TOKEN,
    AssertionResult,
    JudgeCallback,
    _step_level,
    contains_jwt_like,
    evaluate_chat_step,
    evaluate_guard_step,
    evaluate_mcp_step,
    redact_tokens,
)
from harness.audit import (
    KIND_TO_OUTCOME,
    LedgerEntry,
    reconcile_case_audit,
    status_code_to_outcome,
)
from harness.clients import ChatClient, GuardClient, McpClient
from harness.config import EVALS_DIR, REPO_ROOT, Settings
from harness.environment import EnvironmentManager
from harness.invariants import check_write_invariants
from harness.judge import VerdictJudge
from harness.loader import load_cases, load_suite, load_tools_manifest
from harness.metrics import CaseRunOutcome, StepOutcome, compute_metrics
from harness.report import (
    build_report_json,
    collect_environment_info,
    sha256_file,
    sha256_tree,
    write_reports,
)
from harness.schema import (
    CaseSpec,
    ChatAssertionBlock,
    ChatStep,
    GuardStep,
    McpStep,
    SuiteConfig,
)

CN_TZ = timezone(timedelta(hours=8))
VAR_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
AUDITED_TRACE_KINDS = frozenset({"ok", "pending", "denied", "not_found", "conflict", "invalid"})
CONSECUTIVE_CHAT_ENV_LIMIT = 3
logger = logging.getLogger(__name__)


def _emit_warning(message: str, warnings_out: list[str] | None = None) -> None:
    logger.warning(message)
    print(f"[warn] {message}", file=sys.stderr)
    if warnings_out is not None:
        warnings_out.append(message)


def _is_valid_saved_assertions(raw: Any) -> bool:
    """Validate persisted per-step assertions loaded from transcripts.jsonl (R4-L2)."""
    if not isinstance(raw, list) or len(raw) == 0:
        return False
    for item in raw:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        level = item.get("level")
        passed = item.get("passed")
        attribution = item.get("attribution")
        if not isinstance(name, str) or not name:
            return False
        if level not in ("must", "should"):
            return False
        if not isinstance(passed, bool):
            return False
        if "expected" not in item or "actual" not in item:
            return False
        if not isinstance(attribution, str):
            return False
        if (
            "needs_manual_review" in item
            and item["needs_manual_review"] is not None
            and not isinstance(item["needs_manual_review"], bool)
        ):
            return False
        if (
            "verdict_source" in item
            and item["verdict_source"] is not None
            and not isinstance(item["verdict_source"], str)
        ):
            return False
        if (
            "judge_record" in item
            and item["judge_record"] is not None
            and not isinstance(item["judge_record"], dict)
        ):
            return False
    return True


def compute_eval_fingerprint(
    evals_dir: Path,
    judge: VerdictJudge | None = None,
) -> str:
    """Compute deterministic fingerprint of assertion rules and judge config (R4-L2)."""
    judge_enabled = bool(judge is not None and judge.enabled)
    judge_model = judge.model if judge_enabled and judge is not None else ""
    prompt_hash = (
        judge.prompt_sha256
        if judge is not None
        else (sha256_file(evals_dir / "judge" / "prompt.md") or "")
    )
    payload = {
        "keywords": {
            "escalate": list(asserts_mod.ESCALATE_KEYWORDS),
            "need_info": list(asserts_mod.NEED_INFO_KEYWORDS),
            "negative": list(asserts_mod.NEGATIVE_KEYWORDS),
            "positive": list(asserts_mod.POSITIVE_KEYWORDS),
        },
        "judge_prompt_sha256": prompt_hash,
        "judge_enabled": judge_enabled,
        "judge_model": judge_model,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def interpolate_value(obj: Any, variables: Mapping[str, Any]) -> tuple[Any, list[str]]:
    """Substitute `${var}` placeholders recursively; report any missing/empty variable names."""
    missing: list[str] = []

    def _walk(val: Any) -> Any:
        if isinstance(val, str):
            exact = VAR_PLACEHOLDER_RE.fullmatch(val)
            if exact:
                var_name = exact.group(1)
                resolved = variables.get(var_name)
                if resolved is None or resolved == "":
                    if var_name not in missing:
                        missing.append(var_name)
                    return val
                return resolved

            def _replace(match: re.Match[str]) -> str:
                var_name = match.group(1)
                resolved = variables.get(var_name)
                if resolved is None or resolved == "":
                    if var_name not in missing:
                        missing.append(var_name)
                    return match.group(0)
                return str(resolved)

            return VAR_PLACEHOLDER_RE.sub(_replace, val)
        if isinstance(val, list):
            return [_walk(item) for item in val]
        if isinstance(val, dict):
            return {k: _walk(v) for k, v in val.items()}
        return val

    return _walk(obj), missing


def compute_suite_hash(evals_dir: Path) -> str:
    """Compute a stable hash of suite.yaml + all case YAML files for --resume validation (L7-1)."""
    h = hashlib.sha256()
    h.update((sha256_file(evals_dir / "suite.yaml") or "").encode("utf-8"))
    h.update(b"\0")
    h.update((sha256_tree(evals_dir / "cases", "*.yaml") or "").encode("utf-8"))
    return h.hexdigest()


def count_planned_metrics(
    plan: Sequence[tuple[CaseSpec, int]],
) -> dict[str, dict[str, int]]:
    """Count planned M1 authz steps and planned M2 approval chains by layer (M1)."""
    authz_by_layer: dict[str, int] = {"tool": 0, "guard": 0, "dialogue": 0}
    approvals_by_layer: dict[str, int] = {"tool": 0, "guard": 0, "dialogue": 0}
    for case, _ in plan:
        layer = case.layer
        for step in case.steps:
            if step.authz is not None:
                authz_by_layer[layer] = authz_by_layer.get(layer, 0) + 1
            if (
                isinstance(step, McpStep)
                and step.tool == "create_refund"
                and step.expect.result == "pending"
            ):
                approvals_by_layer[layer] = approvals_by_layer.get(layer, 0) + 1
            elif isinstance(step, ChatStep):
                for blk in (step.expect.must, step.expect.should):
                    if (
                        blk
                        and blk.trace
                        and any(
                            c.tool == "create_refund" and c.kind == "pending"
                            for c in blk.trace.calls
                        )
                    ):
                        approvals_by_layer[layer] = approvals_by_layer.get(layer, 0) + 1
                        break
    return {
        "authz": authz_by_layer,
        "approvals": approvals_by_layer,
    }


def _build_run_plan(cases: Sequence[CaseSpec], repeats: int) -> list[tuple[CaseSpec, int]]:
    """Order per 5.8.3: tool (1x) -> guard (1x) -> dialogue round 1..R."""
    tool_cases = [c for c in cases if c.layer == "tool"]
    guard_cases = [c for c in cases if c.layer == "guard"]
    dialogue_cases = [c for c in cases if c.layer == "dialogue"]

    plan: list[tuple[CaseSpec, int]] = []
    for c in tool_cases:
        plan.append((c, 1))
    for c in guard_cases:
        plan.append((c, 1))
    for r in range(1, repeats + 1):
        for c in dialogue_cases:
            plan.append((c, r))
    return plan


def _create_run_dir(base_dir: Path, label: str = "") -> tuple[str, Path]:
    stamp = datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{label}" if label else stamp
    run_dir = base_dir / run_id
    suffix = 1
    while run_dir.exists():
        run_id = f"{stamp}-{label}-{suffix}" if label else f"{stamp}-{suffix}"
        run_dir = base_dir / run_id
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


def _build_step_ledger(
    step: ChatStep | McpStep | GuardStep,
    resolved_req: dict[str, Any],
    response: dict[str, Any],
    approval_applicants: Mapping[str, str],
) -> list[LedgerEntry]:
    """Construct expected audit ledger items for a step per 5.6.1."""
    if isinstance(step, McpStep):
        if response.get("is_jsonrpc_response") and not response.get("is_jsonrpc_error"):
            result = str(response.get("result") or "")
            sc = response.get("status_code")
            outcome = (
                "pending_approval"
                if result == "pending"
                else status_code_to_outcome(sc if isinstance(sc, int) else None)
            )
            args = resolved_req.get("args") or {}
            oid = args.get("order_id") if isinstance(args.get("order_id"), str) else None
            aid = args.get("approval_id") if isinstance(args.get("approval_id"), str) else None
            if result == "pending" and isinstance(response.get("data"), dict):
                aid = response["data"].get("approval_id") or aid
            return [
                LedgerEntry(
                    step_id=step.id,
                    source="mcp",
                    actor=step.actor,
                    event_type="tool_call",
                    channel="agent",
                    tool=step.tool,
                    expected_outcome=outcome,
                    expected_status_code=sc if isinstance(sc, int) else None,
                    expected_error_code=response.get("error_code"),
                    request_id=response.get("request_id") or None,
                    order_id=oid,
                    approval_id=aid,
                )
            ]
        if response.get("http_status") == 401:
            return [
                LedgerEntry(
                    step_id=step.id,
                    source="unauthenticated_401",
                    actor=step.actor,
                    event_type="tool_call",
                    channel="agent",
                    tool=step.tool,
                    expected_outcome="auth_failed",
                    expected_status_code=401,
                )
            ]
        return []

    if isinstance(step, ChatStep):
        if response.get("http_status") == 401:
            return [
                LedgerEntry(
                    step_id=step.id,
                    source="unauthenticated_401",
                    actor=step.actor,
                    event_type="tool_call",
                    channel="agent",
                    tool="chat",
                    expected_outcome="auth_failed",
                    expected_status_code=401,
                )
            ]
        entries: list[LedgerEntry] = []
        for idx, call in enumerate(response.get("trace") or []):
            kind = str(call.get("kind") or "")
            if kind not in AUDITED_TRACE_KINDS:
                continue
            sc = call.get("http_status")
            if kind == "pending":
                sc = 202
            entries.append(
                LedgerEntry(
                    step_id=step.id,
                    source="chat_trace",
                    actor=step.actor,
                    event_type="tool_call",
                    channel="agent",
                    tool=str(call.get("tool") or ""),
                    expected_outcome=KIND_TO_OUTCOME[kind],
                    expected_status_code=int(sc) if isinstance(sc, int) else None,
                    expected_error_code=call.get("error_code") or None,
                    request_id=call.get("request_id") or None,
                    approval_id=call.get("approval_id") or None,
                    trace_index=idx,
                )
            )
        return entries

    # GuardStep
    http_status = int(response.get("http_status") or 0)
    aid = resolved_req.get("approval")
    req_id = response.get("request_id") or None

    if step.call in ("detail", "audit_logs"):
        return []

    if step.call == "approve":
        data = response.get("data") or {}
        applicant_sub = data.get("applicant_sub") or approval_applicants.get(str(aid or ""))
        return [
            LedgerEntry(
                step_id=step.id,
                source="guard_approve",
                actor=step.actor,
                event_type="approval_approve",
                channel="console",
                tool="create_refund",
                expected_outcome=status_code_to_outcome(http_status),
                expected_status_code=http_status,
                expected_error_code=response.get("error_code"),
                request_id=req_id,
                approval_id=str(aid) if aid else None,
                applicant_sub=str(applicant_sub) if applicant_sub else None,
                approval_status_after=data.get("status") if http_status == 200 else None,
                refund_id_after=data.get("refund_id") if http_status == 200 else None,
            )
        ]

    if http_status == 401:
        return [
            LedgerEntry(
                step_id=step.id,
                source="unauthenticated_401",
                actor=step.actor,
                event_type="approval_reject" if step.call == "reject" else "tool_call",
                channel="console",
                tool="create_refund" if step.call == "reject" else "get_order",
                expected_outcome="auth_failed",
                expected_status_code=401,
            )
        ]

    if step.call == "reject":
        if http_status == 404:
            return []
        return [
            LedgerEntry(
                step_id=step.id,
                source="guard_reject",
                actor=step.actor,
                event_type="approval_reject",
                channel="console",
                tool="create_refund",
                expected_outcome=status_code_to_outcome(http_status),
                expected_status_code=http_status,
                expected_error_code=response.get("error_code"),
                request_id=req_id,
                approval_id=str(aid) if aid else None,
            )
        ]

    # step.call == "order": GET /approvals/{id}/order -> tool_call console get_order
    data = response.get("data") or {}
    return [
        LedgerEntry(
            step_id=step.id,
            source="guard_order",
            actor=step.actor,
            event_type="tool_call",
            channel="console",
            tool="get_order",
            expected_outcome=status_code_to_outcome(http_status),
            expected_status_code=http_status,
            expected_error_code=response.get("error_code"),
            request_id=req_id,
            order_id=data.get("order_id") if http_status == 200 else None,
            approval_id=str(aid) if aid else None,
        )
    ]


def _should_run_invariant_probe(
    step: ChatStep | McpStep | GuardStep,
    response: dict[str, Any],
) -> bool:
    if isinstance(step, McpStep) and response.get("result") == "pending":
        return True
    if isinstance(step, ChatStep):
        return any(call.get("kind") == "pending" for call in (response.get("trace") or []))
    if isinstance(step, GuardStep) and step.call in ("approve", "reject"):
        return True
    return False


def _score_case_from_records(
    case: CaseSpec,
    round_num: int,
    step_records: list[dict[str, Any]],
    audit_record: dict[str, Any],
    *,
    suite: SuiteConfig,
    tools_manifest: Mapping[str, dict[str, Any]],
    issued_tokens: Sequence[str],
    judge_fn: JudgeCallback | None,
    reuse_saved_assertions: bool = False,
    warnings_out: list[str] | None = None,
) -> CaseRunOutcome:
    """Pure scoring function shared by live `run` and offline `score` (5.16 #5)."""
    step_by_id = {s.id: s for s in case.steps}
    env_error = bool(audit_record.get("env_error"))
    duration_ms = int(audit_record.get("duration_ms") or 0)

    # 1. Re-evaluate invariants from saved probe snapshots
    inv_snapshots = audit_record.get("invariant_snapshots") or []
    all_approval_ids: list[str] = []
    violated_approval_ids: set[str] = set()
    orphan_refund_ids: set[str] = set()
    invariant_assertions: list[dict[str, Any]] = []
    latest_approvals: dict[str, dict[str, Any]] = {}
    latest_new_refund_ids: list[str] = []

    for snap in inv_snapshots:
        raw_refunds = snap.get("current_refunds")
        raw_approvals = snap.get("current_approvals")
        snap_complete = (
            bool(snap.get("probe_complete", True))
            and isinstance(raw_refunds, list)
            and isinstance(raw_approvals, list)
        )
        snap_error = snap.get("probe_error")
        if not snap_complete and not snap_error:
            snap_error = "不变量探针快照缺少完整的退款单或审批单列表"
        inv_res = check_write_invariants(
            snap.get("baseline_refund_ids") or [],
            raw_refunds if isinstance(raw_refunds, list) else [],
            raw_approvals if isinstance(raw_approvals, list) else [],
            probe_complete=snap_complete,
            probe_error=str(snap_error) if snap_error else None,
        )
        for aid in inv_res.approval_ids:
            if aid not in all_approval_ids:
                all_approval_ids.append(aid)
        violated_approval_ids.update(inv_res.violated_approval_ids)
        orphan_refund_ids.update(inv_res.orphan_refund_ids)
        if snap_complete:
            latest_new_refund_ids = list(inv_res.new_refund_ids)
            latest_approvals = {str(ap["approval_id"]): ap for ap in raw_approvals}
        invariant_assertions.append(
            AssertionResult(
                name=f"invariant:{snap.get('after_step', '')}",
                level="must",
                passed=inv_res.passed,
                expected="满足写操作不变量 I1–I3",
                actual="通过" if inv_res.passed else [v.to_dict() for v in inv_res.violations],
                attribution="审批",
            ).to_dict()
        )

    # 2. Re-run audit reconciliation R1–R6
    step_order = list(audit_record.get("step_order") or [])
    raw_ledgers = audit_record.get("step_ledgers") or {}
    step_ledgers = {
        sid: [LedgerEntry.from_dict(item) for item in entries]
        for sid, entries in raw_ledgers.items()
    }
    step_audits = audit_record.get("step_audits") or {}
    all_audit_flat = [rec for sid in step_order for rec in step_audits.get(sid, [])]

    recon = reconcile_case_audit(
        step_order=step_order,
        step_ledgers=step_ledgers,
        step_audits=step_audits,
        accounts=suite.accounts,
        tools_manifest=tools_manifest,
        final_approvals=latest_approvals,
        new_refund_ids=latest_new_refund_ids,
        issued_tokens=issued_tokens,
    )

    # Preserve any live R6 credential failure during offline rescore, plus JWT/placeholder fallback
    audit_issues = [i.to_dict() for i in recon.issues]
    if not issued_tokens:
        prior_r6 = [i for i in (audit_record.get("r6_issues") or [])]
        audit_issues.extend(prior_r6)
        serialized_audit = json.dumps(all_audit_flat, ensure_ascii=False)
        if not prior_r6 and (
            contains_jwt_like(serialized_audit) or REDACTED_TOKEN in serialized_audit
        ):
            audit_issues.append({"rule": "R6", "message": "审计记录中检测到 JWT 形态凭证"})

    audit_assertions: list[dict[str, Any]] = []
    if not env_error:
        audit_ok = (
            len(audit_issues) == 0
            and recon.unexpected_count == 0
            and recon.matched_count == recon.expected_count
        )
        audit_assertions.append(
            AssertionResult(
                name="audit_reconcile",
                level="must",
                passed=audit_ok,
                expected=f"matched={recon.expected_count}, unexpected=0, R1–R6 全通过",
                actual=(
                    f"matched={recon.matched_count}/{recon.expected_count}, "
                    f"unexpected={recon.unexpected_count}"
                    if audit_ok
                    else audit_issues
                ),
                attribution="审计",
            ).to_dict()
        )

    # 3. Evaluate step assertions (after applying backfilled order_ids to chat trace!)
    step_outcomes: list[StepOutcome] = []
    variables: dict[str, Any] = {}

    for rec in step_records:
        sid = str(rec["step_id"])
        step = step_by_id.get(sid)
        if step is None:
            continue

        if rec.get("skipped"):
            raw_saved_skip = rec.get("assertions") if reuse_saved_assertions else None
            if reuse_saved_assertions and _is_valid_saved_assertions(raw_saved_skip):
                skip_assertions: list[dict[str, Any]] = json.loads(json.dumps(raw_saved_skip))
            else:
                if reuse_saved_assertions:
                    reason = "缺失" if "assertions" not in rec or raw_saved_skip is None else "损坏"
                    _emit_warning(
                        f"用例 {case.id} 第 {round_num} 轮跳过步骤 {sid} 的落盘断言{reason}，"
                        "已回退为重新判定。",
                        warnings_out,
                    )
                skip_reason = str(rec.get("skip_reason") or "missing_var")
                skip_level = _step_level(case.layer, step.severity, skipped=True)
                skip_assertions = [
                    AssertionResult(
                        name="skipped_missing_var",
                        level=skip_level,
                        passed=False,
                        expected="前置变量已就绪",
                        actual=skip_reason,
                        attribution="路由",
                    ).to_dict()
                ]
            step_outcomes.append(
                StepOutcome(
                    step_id=sid,
                    do=step.do,
                    authz=step.authz,
                    severity=step.severity,
                    skipped=True,
                    env_error=False,
                    duration_ms=int(rec.get("duration_ms") or 0),
                    assertions=skip_assertions,
                    request=rec.get("request") or {},
                    response={},
                    audit_records=[],
                )
            )
            continue

        response = json.loads(json.dumps(rec.get("response") or {}))
        step_env_err = bool(rec.get("env_error"))

        # Backfill order_id into chat trace items from R1 (5.6.2)
        if isinstance(step, ChatStep) and isinstance(response.get("trace"), list):
            for idx, call in enumerate(response["trace"]):
                bf_oid = recon.backfilled_order_ids.get(f"{sid}:{idx}")
                if bf_oid:
                    call["order_id"] = bf_oid

        # R4-L2: during --resume, strictly validate persisted assertions before reusing
        raw_saved = rec.get("assertions") if reuse_saved_assertions else None
        if reuse_saved_assertions and _is_valid_saved_assertions(raw_saved):
            assertions_dicts: list[dict[str, Any]] = json.loads(json.dumps(raw_saved))
        else:
            if reuse_saved_assertions:
                reason = "缺失" if "assertions" not in rec or raw_saved is None else "损坏"
                _emit_warning(
                    f"用例 {case.id} 第 {round_num} 轮步骤 {sid} 的落盘断言{reason}，"
                    "已回退为重新判定。",
                    warnings_out,
                )
            # Re-interpolate expect fields using saved variables so dynamic assertions match
            if isinstance(step, McpStep):
                exp_data, _ = (
                    interpolate_value(step.expect.data, variables)
                    if step.expect.data is not None
                    else (None, [])
                )
                exp_msg, _ = (
                    interpolate_value(step.expect.message_contains, variables)
                    if step.expect.message_contains is not None
                    else (None, [])
                )
                assert_objs = evaluate_mcp_step(
                    step,
                    response,
                    layer=case.layer,
                    category=case.category,
                    sentinels=suite.sentinels,
                    expected_data=exp_data,
                    expected_message_contains=exp_msg,
                )
            elif isinstance(step, GuardStep):
                exp_data, _ = (
                    interpolate_value(step.expect.data, variables)
                    if step.expect.data is not None
                    else (None, [])
                )
                exp_msg, _ = (
                    interpolate_value(step.expect.message_contains, variables)
                    if step.expect.message_contains is not None
                    else (None, [])
                )
                assert_objs = evaluate_guard_step(
                    step,
                    response,
                    layer=case.layer,
                    category=case.category,
                    sentinels=suite.sentinels,
                    expected_data=exp_data,
                    expected_message_contains=exp_msg,
                )
            else:
                res_must = None
                if step.expect.must is not None:
                    m_dict, _ = interpolate_value(step.expect.must.model_dump(), variables)
                    res_must = ChatAssertionBlock.model_validate(m_dict)
                res_should = None
                if step.expect.should is not None:
                    s_dict, _ = interpolate_value(step.expect.should.model_dump(), variables)
                    res_should = ChatAssertionBlock.model_validate(s_dict)
                assert_objs = evaluate_chat_step(
                    step,
                    response,
                    category=case.category,
                    sentinels=suite.sentinels,
                    issued_tokens=issued_tokens,
                    prev_conversation_id=rec.get("prev_conversation_id"),
                    judge_fn=judge_fn,
                    resolved_must=res_must,
                    resolved_should=res_should,
                    prior_token_leak=rec.get("token_leak"),
                )
            assertions_dicts = [a.to_dict() for a in assert_objs]

        if step.save:
            for var_name, expr in step.save.items():
                variables[var_name] = jmespath.search(expr, response)

        step_outcomes.append(
            StepOutcome(
                step_id=sid,
                do=step.do,
                authz=step.authz,
                severity=step.severity,
                skipped=False,
                env_error=step_env_err,
                duration_ms=int(rec.get("duration_ms") or 0),
                assertions=assertions_dicts,
                trace=response.get("trace") or [] if isinstance(step, ChatStep) else [],
                query=str((rec.get("request") or {}).get("query") or ""),
                answer=str(response.get("answer") or "") if isinstance(step, ChatStep) else "",
                citations=response.get("citations") or [] if isinstance(step, ChatStep) else [],
                request=rec.get("request") or {},
                response=response,
                audit_records=list(step_audits.get(sid, [])),
            )
        )

    return CaseRunOutcome(
        case_id=case.id,
        title=case.title,
        layer=case.layer,
        category=case.category,
        round_num=round_num,
        duration_ms=duration_ms,
        env_error=env_error,
        steps=step_outcomes,
        approval_ids=all_approval_ids,
        violated_approval_ids=sorted(violated_approval_ids),
        orphan_refund_ids=sorted(orphan_refund_ids),
        invariant_assertions=invariant_assertions,
        audit_expected=recon.expected_count,
        audit_matched=recon.matched_count,
        audit_unexpected=recon.unexpected_count,
        audit_assertions=audit_assertions,
        audit_records=all_audit_flat,
        audit_access_denied_expected=recon.access_denied_expected,
        audit_access_denied_actual=recon.access_denied_actual,
        audit_approval_chains=[c.to_dict() for c in recon.approval_chains],
        audit_discrepancies=[{"case": case.id, **d} for d in audit_issues],
        audit_matches=list(recon.record_matches),
    )


def _redact_outcome(outcome: CaseRunOutcome, tokens: Sequence[str]) -> CaseRunOutcome:
    """Return a copy of CaseRunOutcome with all token-bearing fields redacted (L10)."""
    return CaseRunOutcome(
        case_id=outcome.case_id,
        title=outcome.title,
        layer=outcome.layer,
        category=outcome.category,
        round_num=outcome.round_num,
        duration_ms=outcome.duration_ms,
        env_error=outcome.env_error,
        steps=[
            StepOutcome(
                step_id=s.step_id,
                do=s.do,
                authz=s.authz,
                severity=s.severity,
                skipped=s.skipped,
                env_error=s.env_error,
                duration_ms=s.duration_ms,
                assertions=redact_tokens(s.assertions, tokens),
                trace=redact_tokens(s.trace, tokens),
                query=redact_tokens(s.query, tokens),
                answer=redact_tokens(s.answer, tokens),
                citations=redact_tokens(s.citations, tokens),
                request=redact_tokens(s.request, tokens),
                response=redact_tokens(s.response, tokens),
                audit_records=redact_tokens(s.audit_records, tokens),
            )
            for s in outcome.steps
        ],
        approval_ids=list(outcome.approval_ids),
        violated_approval_ids=list(outcome.violated_approval_ids),
        orphan_refund_ids=list(outcome.orphan_refund_ids),
        invariant_assertions=redact_tokens(outcome.invariant_assertions, tokens),
        audit_expected=outcome.audit_expected,
        audit_matched=outcome.audit_matched,
        audit_unexpected=outcome.audit_unexpected,
        audit_assertions=redact_tokens(outcome.audit_assertions, tokens),
        audit_records=redact_tokens(outcome.audit_records, tokens),
        audit_access_denied_expected=dict(outcome.audit_access_denied_expected),
        audit_access_denied_actual=dict(outcome.audit_access_denied_actual),
        audit_approval_chains=redact_tokens(outcome.audit_approval_chains, tokens),
        audit_discrepancies=redact_tokens(outcome.audit_discrepancies, tokens),
        audit_matches=redact_tokens(outcome.audit_matches, tokens),
    )


def _aggregate_audit_summary(outcomes: Sequence[CaseRunOutcome]) -> dict[str, Any]:
    expected = 0
    matched = 0
    unexpected = 0
    exp_denied: Counter[str] = Counter()
    act_denied: Counter[str] = Counter()
    chains: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    for o in outcomes:
        if o.env_error:
            discrepancies.extend(o.audit_discrepancies)
            continue
        expected += o.audit_expected
        matched += o.audit_matched
        unexpected += o.audit_unexpected
        exp_denied.update(o.audit_access_denied_expected)
        act_denied.update(o.audit_access_denied_actual)
        chains.extend(o.audit_approval_chains)
        discrepancies.extend(o.audit_discrepancies)

    codes = sorted(set(exp_denied) | set(act_denied))
    denied_cmp = {
        code: {"expected": exp_denied.get(code, 0), "actual": act_denied.get(code, 0)}
        for code in codes
    }
    return {
        "expected": expected,
        "matched": matched,
        "unexpected": unexpected,
        "access_denied_by_code": denied_cmp,
        "approval_chains": chains,
        "discrepancies": discrepancies,
    }


def _estimate_cost(
    outcomes: Sequence[CaseRunOutcome],
    *,
    resets: int,
    judge: VerdictJudge | None,
    prior_cost_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    chat_messages = 0
    chat_turns = 0
    main_model_calls = 0
    judge_unavailable_steps = 0
    layer_ms: dict[str, int] = {}
    for o in outcomes:
        layer_ms[o.layer] = layer_ms.get(o.layer, 0) + o.duration_ms
        for s in o.steps:
            if not o.env_error and any(
                a.get("name") == "verdict"
                and isinstance(a.get("judge_record"), dict)
                and a["judge_record"].get("status") == "unavailable"
                for a in s.assertions
            ):
                judge_unavailable_steps += 1
            if s.do == "chat" and not s.skipped:
                chat_messages += 1
                if not s.env_error and not any(
                    a["name"] == "http_status" and a["expected"] == 401 for a in s.assertions
                ):
                    chat_turns += 1
                    if s.citations or any(call.get("tool") == "create_refund" for call in s.trace):
                        main_model_calls += 1

    # 5.7.2: 2 router calls/turn + 1 main model call when reaching advise/decide_refund/answer_kb
    router_calls_estimated = chat_turns * 2
    estimated_model_calls = router_calls_estimated + main_model_calls
    prior = prior_cost_counts or {}
    judge_calibration_calls = int(prior.get("judge_calibration_calls", 0) or 0) + (
        judge.calibration_call_count if judge is not None else 0
    )
    judge_calls = int(prior.get("judge_calls", 0) or 0) + (
        judge.call_count if judge is not None else 0
    )
    judge_cache_hits = int(prior.get("judge_cache_hits", 0) or 0) + (
        judge.cache_hits if judge is not None else 0
    )
    judge_invalid = int(prior.get("judge_invalid", 0) or 0) + (
        judge.invalid_count if judge is not None else 0
    )
    return {
        "chat_messages": chat_messages,
        "chat_turns": chat_turns,
        "router_calls_estimated": router_calls_estimated,
        "main_model_calls_estimated": main_model_calls,
        "model_calls_estimated": estimated_model_calls,
        "model_calls_upper_bound": chat_turns * 3,
        "judge_calibration_calls": judge_calibration_calls,
        "judge_calls": judge_calls,
        "judge_cache_hits": judge_cache_hits,
        "judge_invalid": judge_invalid,
        "judge_unavailable_steps": judge_unavailable_steps,
        "resets": resets,
        "by_layer_duration_seconds": {k: round(v / 1000.0, 2) for k, v in layer_ms.items()},
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    path.write_text(content, encoding="utf-8")


class EvalRunner:
    """Orchestrates sequential execution of filtered cases, probes, audit, and reporting."""

    def __init__(
        self,
        settings: Settings,
        suite: SuiteConfig,
        *,
        mcp_client: McpClient,
        guard_client: GuardClient,
        chat_client: ChatClient,
        token_provider: Any,
        env_manager: EnvironmentManager,
        tools_manifest: Mapping[str, dict[str, Any]],
        judge: VerdictJudge | None = None,
        evals_dir: Path = EVALS_DIR,
        repo_root: Path = REPO_ROOT,
        sleep_fn: Callable[[float], None] = time.sleep,
        perf_counter_fn: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._settings = settings
        self._suite = suite
        self._mcp = mcp_client
        self._guard = guard_client
        self._chat = chat_client
        self._tokens = token_provider
        self._env = env_manager
        self._manifest = tools_manifest
        self._judge = judge
        self._evals_dir = evals_dir
        self._repo_root = repo_root
        self._sleep = sleep_fn
        self._perf = perf_counter_fn

    def _probe_paginated_list(
        self,
        tool: str,
        base_step_id: str,
        auditor_token: str,
        step_order: list[str],
        step_ledgers: dict[str, list[dict[str, Any]]],
        step_audits: dict[str, list[dict[str, Any]]],
        *,
        max_pages: int = 50,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """Fetch all pages of list_refunds / list_approvals for invariant probes (H4)."""
        collected: list[dict[str, Any]] = []
        page = 1
        page_size: int | None = None
        expected_total: int | None = None

        while page <= max_pages:
            args: dict[str, Any] = {} if page == 1 else {"page": page}
            if page > 1 and page_size is not None:
                args["page_size"] = page_size
            probe_step_id = base_step_id if page == 1 else f"{base_step_id}_p{page}"
            resp = self._mcp.call_tool(tool, args, auditor_token)
            step_order.append(probe_step_id)
            if resp.get("is_jsonrpc_response") and not resp.get("is_jsonrpc_error"):
                sc = resp.get("status_code")
                step_ledgers[probe_step_id] = [
                    LedgerEntry(
                        step_id=probe_step_id,
                        source="mcp",
                        actor=self._suite.auditor,
                        event_type="tool_call",
                        channel="agent",
                        tool=tool,
                        expected_outcome=status_code_to_outcome(
                            sc if isinstance(sc, int) else None
                        ),
                        expected_status_code=sc if isinstance(sc, int) else None,
                        expected_error_code=resp.get("error_code"),
                        request_id=resp.get("request_id") or None,
                    ).to_dict()
                ]
            else:
                step_ledgers[probe_step_id] = []
            audits, self._env.audit_cursor = self._guard.fetch_audit_since(
                self._env.audit_cursor, auditor_token
            )
            step_audits[probe_step_id] = audits

            if resp.get("result") != "ok":
                return (
                    [],
                    False,
                    f"{tool} 探针调用失败 (page={page}): "
                    f"result={resp.get('result')}, status={resp.get('status_code')}",
                )

            data = resp.get("data")
            if (
                not isinstance(data, dict)
                or "items" not in data
                or not isinstance(data["items"], list)
            ):
                return ([], False, f"{tool} 探针响应缺少合法 items 键 (page={page})")

            items = data["items"]
            collected.extend(items)
            raw_total = data.get("total")
            if raw_total is not None:
                if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total < 0:
                    return ([], False, f"{tool} 探针响应 total 字段非法: {raw_total!r}")
                expected_total = raw_total
            raw_ps = data.get("page_size")
            if isinstance(raw_ps, int) and not isinstance(raw_ps, bool) and raw_ps > 0:
                page_size = raw_ps
            elif page_size is None and items:
                page_size = len(items)

            if expected_total is None or len(collected) >= expected_total:
                break
            if not items:
                return (
                    [],
                    False,
                    f"{tool} 探针分页第 {page} 页返回空列表但未达到 total={expected_total}",
                )
            page += 1

        if expected_total is not None and len(collected) < expected_total:
            return (
                [],
                False,
                f"{tool} 探针分页未取全: collected={len(collected)}, total={expected_total}",
            )
        return (collected, True, None)

    def _execute_case(
        self,
        case: CaseSpec,
        round_num: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        """Execute a single (case, round) returning transcripts, audit row, and env-error streak."""
        if self._env.needs_reset(fresh=case.fresh):
            self._env.reset()

        t0 = self._perf()
        auditor_token = self._tokens.get_token(self._suite.auditor, "valid")
        assert isinstance(auditor_token, str)

        variables: dict[str, Any] = {}
        conversations: dict[str, str] = {}
        approval_applicants: dict[str, str] = {}

        step_order: list[str] = []
        step_ledgers: dict[str, list[dict[str, Any]]] = {}
        step_audits: dict[str, list[dict[str, Any]]] = {}
        invariant_snapshots: list[dict[str, Any]] = []
        transcript_rows: list[dict[str, Any]] = []
        case_env_error = False
        chat_env_errors_seen: list[bool] = []

        for step in case.steps:
            # Interpolate step payload
            raw_req: dict[str, Any]
            if isinstance(step, ChatStep):
                raw_req = {"query": step.query, "conversation": step.conversation}
            elif isinstance(step, McpStep):
                raw_req = {"tool": step.tool, "args": step.args}
            else:
                raw_req = {
                    "call": step.call,
                    "approval": step.approval,
                    "comment": step.comment,
                    "params": step.params,
                }

            resolved_req, missing_vars = interpolate_value(raw_req, variables)
            _, missing_expect_vars = interpolate_value(step.expect.model_dump(), variables)
            for v in missing_expect_vars:
                if v not in missing_vars:
                    missing_vars.append(v)
            if missing_vars:
                transcript_rows.append(
                    {
                        "case_id": case.id,
                        "round": round_num,
                        "step_id": step.id,
                        "do": step.do,
                        "actor": step.actor,
                        "token_type": step.token,
                        "request": raw_req,
                        "skipped": True,
                        "skip_reason": f"missing_var:{','.join(missing_vars)}",
                        "response": {},
                        "duration_ms": 0,
                        "env_error": False,
                    }
                )
                continue

            token = self._tokens.get_token(step.actor, step.token)
            prev_conv_id = (
                conversations.get(step.conversation) if isinstance(step, ChatStep) else None
            )

            st_t0 = self._perf()
            if isinstance(step, McpStep):
                response = self._mcp.call_tool(resolved_req["tool"], resolved_req["args"], token)
            elif isinstance(step, GuardStep):
                response = self._guard.call(
                    resolved_req["call"],
                    token,
                    approval_id=resolved_req.get("approval"),
                    comment=resolved_req.get("comment"),
                    params=resolved_req.get("params"),
                )
            else:
                response = self._chat.send_message(
                    resolved_req["query"],
                    token,
                    conversation_id=prev_conv_id,
                )
                if self._settings.turn_interval_seconds > 0:
                    self._sleep(self._settings.turn_interval_seconds)
            st_ms = int(round((self._perf() - st_t0) * 1000))

            if isinstance(step, ChatStep) and response.get("conversation_id"):
                conversations[step.conversation] = str(response["conversation_id"])

            if isinstance(step, McpStep) and response.get("result") == "pending":
                data = response.get("data") or {}
                if data.get("approval_id"):
                    approval_applicants[str(data["approval_id"])] = str(
                        data.get("applicant_sub") or self._suite.accounts[step.actor].sub
                    )

            step_env_err = bool(response.get("env_error"))
            if step_env_err:
                case_env_error = True
                self._env.mark_dirty()

            if isinstance(step, ChatStep):
                is_chat_unavail = response.get("http_status") in (502, 503, 0) or any(
                    c.get("kind") == "unavailable" for c in (response.get("trace") or [])
                )
                chat_env_errors_seen.append(is_chat_unavail)

            # Record ledger and fetch incremental audit right after the step (5.6.2)
            entries = _build_step_ledger(step, resolved_req, response, approval_applicants)
            step_order.append(step.id)
            step_ledgers[step.id] = [e.to_dict() for e in entries]
            fetched, new_cursor = self._guard.fetch_audit_since(
                self._env.audit_cursor, auditor_token
            )
            self._env.audit_cursor = new_cursor
            step_audits[step.id] = fetched

            # Save variables via JMESPath
            if step.save:
                for var_name, expr in step.save.items():
                    variables[var_name] = jmespath.search(expr, response)

            # Dirty & invariant probe trigger (5.5.5, 5.8.2, H4)
            if _should_run_invariant_probe(step, response):
                self._env.mark_dirty()
                curr_refunds, rf_ok, rf_err = self._probe_paginated_list(
                    "list_refunds",
                    f"{step.id}__probe_refunds",
                    auditor_token,
                    step_order,
                    step_ledgers,
                    step_audits,
                )
                if rf_ok:
                    curr_approvals, ap_ok, ap_err = self._probe_paginated_list(
                        "list_approvals",
                        f"{step.id}__probe_approvals",
                        auditor_token,
                        step_order,
                        step_ledgers,
                        step_audits,
                    )
                else:
                    curr_approvals, ap_ok, ap_err = [], False, None

                if not rf_ok or not ap_ok:
                    case_env_error = True
                    probe_err = "; ".join(e for e in (rf_err, ap_err) if e)
                    invariant_snapshots.append(
                        {
                            "after_step": step.id,
                            "probe_complete": False,
                            "probe_error": probe_err,
                            "baseline_refund_ids": sorted(self._env.baseline_refund_ids),
                            "current_refunds": None,
                            "current_approvals": None,
                        }
                    )
                else:
                    for ap in curr_approvals:
                        if ap.get("approval_id") and ap.get("applicant_sub"):
                            approval_applicants[str(ap["approval_id"])] = str(ap["applicant_sub"])
                    invariant_snapshots.append(
                        {
                            "after_step": step.id,
                            "probe_complete": True,
                            "probe_error": None,
                            "baseline_refund_ids": sorted(self._env.baseline_refund_ids),
                            "current_refunds": curr_refunds,
                            "current_approvals": curr_approvals,
                        }
                    )

            transcript_rows.append(
                {
                    "case_id": case.id,
                    "round": round_num,
                    "step_id": step.id,
                    "do": step.do,
                    "actor": step.actor,
                    "token_type": step.token,
                    "request": resolved_req,
                    "prev_conversation_id": prev_conv_id,
                    "skipped": False,
                    "skip_reason": None,
                    "response": response,
                    "duration_ms": st_ms,
                    "env_error": step_env_err,
                }
            )

        case_ms = int(round((self._perf() - t0) * 1000))
        audit_row = {
            "case_id": case.id,
            "round": round_num,
            "env_error": case_env_error,
            "duration_ms": case_ms,
            "step_order": step_order,
            "step_ledgers": step_ledgers,
            "step_audits": step_audits,
            "invariant_snapshots": invariant_snapshots,
            "r6_issues": [],
        }

        # Count trailing consecutive chat env errors inside this case
        streak_signals = 0
        for flag in chat_env_errors_seen:
            streak_signals = streak_signals + 1 if flag else 0
        return (
            transcript_rows,
            audit_row,
            streak_signals
            if all(chat_env_errors_seen) and chat_env_errors_seen
            else -streak_signals,
        )

    def run(
        self,
        cases: Sequence[CaseSpec],
        *,
        repeats: int,
        layers: list[str],
        is_full_suite: bool,
        categories: list[str] | None = None,
        case_ids: list[str] | None = None,
        label: str = "",
        note: str = "",
        keep_state: bool = False,
        resume_dir: Path | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        """Execute planned cases (with optional resume) and write all run artifacts."""
        started_at = datetime.now(CN_TZ).isoformat(timespec="seconds")
        run_t0 = self._perf()

        plan = _build_run_plan(cases, repeats)
        planned_counts = count_planned_metrics(plan)
        planned_authz_by_layer = planned_counts["authz"]
        planned_approvals_by_layer = planned_counts["approvals"]
        planned_authz = sum(planned_authz_by_layer.values())
        planned_approvals = sum(planned_approvals_by_layer.values())
        suite_hash = compute_suite_hash(self._evals_dir)
        eval_fingerprint = compute_eval_fingerprint(self._evals_dir, self._judge)
        case_map = {c.id: c for c in cases}

        transcript_all: list[dict[str, Any]] = []
        audit_all: list[dict[str, Any]] = []
        completed_keys: set[tuple[str, int]] = set()
        live_outcomes: dict[tuple[str, int], CaseRunOutcome] = {}
        prev_fingerprint: str | None = None
        reuse_saved_assertions = False
        resumed_rejudged = False
        prior_resets = 0
        prior_judge_cost: dict[str, int] = {
            "judge_calibration_calls": 0,
            "judge_calls": 0,
            "judge_cache_hits": 0,
            "judge_invalid": 0,
        }

        if resume_dir is not None:
            run_dir = resume_dir
            meta_path = run_dir / "run_meta.json"
            prev_meta = (
                json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            )
            prev_run = prev_meta.get("run") if isinstance(prev_meta.get("run"), dict) else prev_meta
            run_id = str(prev_run.get("id") or run_dir.name)
            started_at = str(prev_run.get("started_at") or started_at)
            if not label and prev_run.get("label"):
                label = str(prev_run["label"])
            prior_audits = _read_jsonl(run_dir / "audit.jsonl")
            prior_transcripts = _read_jsonl(run_dir / "transcripts.jsonl")
            for a_row in prior_audits:
                key = (str(a_row["case_id"]), int(a_row["round"]))
                if not a_row.get("env_error"):
                    completed_keys.add(key)
                    audit_all.append(a_row)
            for t_row in prior_transcripts:
                key = (str(t_row["case_id"]), int(t_row["round"]))
                if key in completed_keys:
                    transcript_all.append(t_row)

            raw_prev_fp = prev_run.get("eval_fingerprint") or prev_meta.get("eval_fingerprint")
            prev_fingerprint = raw_prev_fp if isinstance(raw_prev_fp, str) and raw_prev_fp else None
            fingerprint_matched = prev_fingerprint == eval_fingerprint
            reuse_saved_assertions = fingerprint_matched
            resumed_rejudged = (not fingerprint_matched) and len(completed_keys) > 0
            if resumed_rejudged:
                _emit_warning(
                    "因断言规则或评审配置变化，本次续跑已放弃复用落盘断言，将对既有记录统一重新判定。"
                )

            raw_cost_counts = prev_meta.get("cost_counts")
            prev_cost = raw_cost_counts if isinstance(raw_cost_counts, dict) else {}
            prior_resets = int(prev_cost.get("resets", prev_meta.get("resets") or 0) or 0)
            if fingerprint_matched:
                prior_judge_cost = {
                    "judge_calibration_calls": int(
                        prev_cost.get("judge_calibration_calls", 0) or 0
                    ),
                    "judge_calls": int(prev_cost.get("judge_calls", 0) or 0),
                    "judge_cache_hits": int(prev_cost.get("judge_cache_hits", 0) or 0),
                    "judge_invalid": int(prev_cost.get("judge_invalid", 0) or 0),
                }
        else:
            report_base = self._settings.resolve_report_dir(self._evals_dir)
            run_id, run_dir = _create_run_dir(report_base, label=label)

        judge_meta = (
            self._judge.metadata()
            if self._judge is not None
            else {"configured": False, "enabled": False}
        )
        env_info = collect_environment_info(
            self._settings,
            judge_meta=judge_meta,
            note=note,
            evals_dir=self._evals_dir,
            repo_root=self._repo_root,
        )

        # Keep previous fingerprint on disk until resumed transcripts are re-evaluated & flushed
        active_fingerprint: str | None = prev_fingerprint if resumed_rejudged else eval_fingerprint

        def _current_cost_counts() -> dict[str, int]:
            return {
                "judge_calibration_calls": prior_judge_cost["judge_calibration_calls"]
                + (self._judge.calibration_call_count if self._judge is not None else 0),
                "judge_calls": prior_judge_cost["judge_calls"]
                + (self._judge.call_count if self._judge is not None else 0),
                "judge_cache_hits": prior_judge_cost["judge_cache_hits"]
                + (self._judge.cache_hits if self._judge is not None else 0),
                "judge_invalid": prior_judge_cost["judge_invalid"]
                + (self._judge.invalid_count if self._judge is not None else 0),
                "resets": prior_resets + self._env.reset_count,
            }

        def _make_run_info(
            *, complete: bool, duration_seconds: float, aborted: bool
        ) -> dict[str, Any]:
            info: dict[str, Any] = {
                "id": run_id,
                "label": label,
                "layers": list(layers),
                "repeats": repeats,
                "categories": list(categories) if categories is not None else None,
                "cases": list(case_ids) if case_ids is not None else None,
                "suite_hash": suite_hash,
                "eval_fingerprint": active_fingerprint,
                "complete": complete,
                "is_full_suite": is_full_suite,
                "started_at": started_at,
                "duration_seconds": duration_seconds,
                "planned_runs": len(plan),
                "planned_authz": planned_authz,
                "planned_approvals": planned_approvals,
                "planned_authz_by_layer": dict(planned_authz_by_layer),
                "planned_approvals_by_layer": dict(planned_approvals_by_layer),
                "aborted_env_error": aborted,
            }
            if resumed_rejudged:
                info["resumed_rejudged"] = True
            return info

        def _write_run_meta(run_info: dict[str, Any], tokens: Sequence[str] = ()) -> None:
            cost_counts = _current_cost_counts()
            meta_doc = redact_tokens(
                {
                    "run": run_info,
                    "environment": env_info,
                    "resets": cost_counts["resets"],
                    "cost_counts": cost_counts,
                },
                tokens,
            )
            (run_dir / "run_meta.json").write_text(
                json.dumps(meta_doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        # Write initial run_meta.json at start so --resume works even after abrupt crash (L7-1)
        _write_run_meta(_make_run_info(complete=False, duration_seconds=0.0, aborted=False))

        consecutive_chat_errors = 0
        aborted_env_error = False

        for case, round_num in plan:
            key = (case.id, round_num)
            if key in completed_keys:
                continue

            try:
                t_rows, a_row, err_signal = self._execute_case(case, round_num)
            except Exception as exc:
                aborted_env_error = True
                live_tokens = self._tokens.all_issued_tokens()
                audit_all.append(
                    redact_tokens(
                        {
                            "case_id": case.id,
                            "round": round_num,
                            "env_error": True,
                            "duration_ms": 0,
                            "step_order": [],
                            "step_ledgers": {},
                            "step_audits": {},
                            "invariant_snapshots": [],
                            "r6_issues": [],
                            "audit_matches": [],
                            "error": str(exc),
                        },
                        live_tokens,
                    )
                )
                _write_jsonl(run_dir / "audit.jsonl", audit_all)
                break

            # Evaluate token_leak & R6 immediately with live tokens BEFORE redacting for disk (M11)
            live_tokens = self._tokens.all_issued_tokens()
            live_outcome = _score_case_from_records(
                case,
                round_num,
                t_rows,
                a_row,
                suite=self._suite,
                tools_manifest=self._manifest,
                issued_tokens=live_tokens,
                judge_fn=self._judge.classify
                if self._judge is not None and self._judge.enabled
                else None,
            )
            a_row["r6_issues"] = [
                d for d in live_outcome.audit_discrepancies if d.get("rule") == "R6"
            ]
            a_row["audit_matches"] = list(live_outcome.audit_matches)

            # Persist per-step assertions & token_leak result on transcript rows before redaction
            outcome_step_map = {s.step_id: s for s in live_outcome.steps}
            for row in t_rows:
                so = outcome_step_map.get(str(row["step_id"]))
                if so is not None:
                    row["assertions"] = so.assertions
                    if row.get("do") == "chat" and not row.get("skipped"):
                        tl = next((a for a in so.assertions if a["name"] == "token_leak"), None)
                        if tl is not None:
                            row["token_leak"] = {
                                "passed": bool(tl["passed"]),
                                "actual": tl["actual"],
                            }

            # Redact all issued tokens & JWT patterns before persisting to disk (M11, L10)
            safe_t_rows = redact_tokens(t_rows, live_tokens)
            safe_a_row = redact_tokens(a_row, live_tokens)
            live_outcomes[key] = _redact_outcome(live_outcome, live_tokens)
            transcript_all.extend(safe_t_rows)
            audit_all.append(safe_a_row)

            # Incremental flush for crash-safe --resume
            _write_jsonl(run_dir / "transcripts.jsonl", transcript_all)
            _write_jsonl(run_dir / "audit.jsonl", audit_all)
            _write_run_meta(
                _make_run_info(
                    complete=False,
                    duration_seconds=round(self._perf() - run_t0, 2),
                    aborted=False,
                ),
                live_tokens,
            )

            if case.layer == "dialogue":
                if err_signal > 0:
                    consecutive_chat_errors += err_signal
                elif err_signal < 0:
                    consecutive_chat_errors = -err_signal
                else:
                    consecutive_chat_errors = 0
                if consecutive_chat_errors >= CONSECUTIVE_CHAT_ENV_LIMIT:
                    aborted_env_error = True
                    break

        # Final reset unless --keep-state or aborted (5.8.2)
        if not keep_state and not aborted_env_error and self._env.dirty:
            try:
                self._env.reset()
            except Exception:
                aborted_env_error = True

        duration_seconds = round(self._perf() - run_t0, 2)
        live_tokens = self._tokens.all_issued_tokens()
        judge_fn = self._judge.classify if self._judge is not None and self._judge.enabled else None

        # Reuse live-scored outcomes to avoid duplicate judge calls / inflated cache hits (L10);
        # only score runs loaded from --resume or aborted via exception.
        outcomes: list[CaseRunOutcome] = []
        grouped_transcripts: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for t_row in transcript_all:
            grouped_transcripts.setdefault((str(t_row["case_id"]), int(t_row["round"])), []).append(
                t_row
            )
        transcripts_dirty = False
        for a_row in audit_all:
            cid = str(a_row["case_id"])
            rnum = int(a_row["round"])
            if cid not in case_map:
                continue
            key = (cid, rnum)
            if key in live_outcomes:
                outcomes.append(live_outcomes[key])
            else:
                case_t_rows = grouped_transcripts.get(key, [])
                had_invalid_saved = any(
                    not _is_valid_saved_assertions(r.get("assertions")) for r in case_t_rows
                )
                re_outcome = _score_case_from_records(
                    case_map[cid],
                    rnum,
                    case_t_rows,
                    a_row,
                    suite=self._suite,
                    tools_manifest=self._manifest,
                    issued_tokens=[],
                    judge_fn=judge_fn,
                    reuse_saved_assertions=reuse_saved_assertions,
                )
                outcomes.append(re_outcome)
                if not reuse_saved_assertions or had_invalid_saved:
                    so_map = {s.step_id: s for s in re_outcome.steps}
                    for row in case_t_rows:
                        so = so_map.get(str(row["step_id"]))
                        if so is not None:
                            row["assertions"] = redact_tokens(so.assertions, live_tokens)
                    transcripts_dirty = True

        if transcripts_dirty:
            _write_jsonl(run_dir / "transcripts.jsonl", redact_tokens(transcript_all, live_tokens))

        active_fingerprint = eval_fingerprint
        metrics_res = compute_metrics(
            self._suite,
            outcomes,
            planned_run_count=len(plan),
            aborted_env_error=aborted_env_error,
            is_full_suite=is_full_suite,
            planned_authz_by_layer=planned_authz_by_layer,
            planned_approval_by_layer=planned_approvals_by_layer,
        )
        audit_summary = _aggregate_audit_summary(outcomes)
        cost = _estimate_cost(
            outcomes,
            resets=prior_resets + self._env.reset_count,
            judge=self._judge,
            prior_cost_counts=prior_judge_cost,
        )
        run_info = _make_run_info(
            complete=metrics_res["complete"],
            duration_seconds=duration_seconds,
            aborted=aborted_env_error,
        )
        _write_run_meta(run_info, live_tokens)

        report = build_report_json(
            run_info=run_info,
            metrics_result=metrics_res,
            audit_summary=audit_summary,
            environment=env_info,
            cost=cost,
        )
        write_reports(run_dir, report, issued_tokens=live_tokens)
        return run_dir, redact_tokens(report, live_tokens)


def rescore_run_dir(
    run_dir: Path,
    *,
    settings: Settings | None = None,
    judge: VerdictJudge | None = None,
    evals_dir: Path = EVALS_DIR,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Offline `score <run_dir>`: re-evaluate saved transcripts & audit without SUT (5.11.1)."""
    cfg = settings or Settings()
    suite = load_suite(evals_dir)
    all_cases = {c.id: c for _, c in load_cases(evals_dir)}
    tools_manifest = load_tools_manifest(repo_root)

    meta_path = run_dir / "run_meta.json"
    transcripts_path = run_dir / "transcripts.jsonl"
    audit_path = run_dir / "audit.jsonl"
    if not meta_path.is_file():
        raise FileNotFoundError(f"运行目录缺少 run_meta.json: {run_dir}")
    if not transcripts_path.is_file() or not audit_path.is_file():
        raise FileNotFoundError(f"运行目录缺少 transcripts.jsonl 或 audit.jsonl: {run_dir}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or not isinstance(meta.get("run"), dict):
        raise ValueError(f"run_meta.json 格式无效（缺少 run 对象）: {meta_path}")
    run_info = dict(meta["run"])
    run_info.pop("resumed_rejudged", None)
    env_info = dict(meta.get("environment") or {})
    meta_cost_counts = meta.get("cost_counts") if isinstance(meta.get("cost_counts"), dict) else {}
    resets = int(meta_cost_counts.get("resets", meta.get("resets") or 0) or 0)

    transcripts = _read_jsonl(transcripts_path)
    audits = _read_jsonl(audit_path)
    if not audits:
        raise ValueError(f"运行目录没有任何用例记录: {run_dir}")

    grouped_t: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for t_row in transcripts:
        grouped_t.setdefault((str(t_row["case_id"]), int(t_row["round"])), []).append(t_row)

    judge_fn = judge.classify if judge is not None and judge.enabled else None
    outcomes: list[CaseRunOutcome] = []
    for a_row in audits:
        cid = str(a_row["case_id"])
        rnum = int(a_row["round"])
        if cid not in all_cases:
            continue
        outcomes.append(
            _score_case_from_records(
                all_cases[cid],
                rnum,
                grouped_t.get((cid, rnum), []),
                a_row,
                suite=suite,
                tools_manifest=tools_manifest,
                issued_tokens=[],
                judge_fn=judge_fn,
            )
        )
    if not outcomes:
        raise ValueError(f"运行目录没有任何有效用例记录: {run_dir}")

    planned = run_info.get("planned_runs")
    aborted = bool(run_info.get("aborted_env_error", False))
    is_full_suite = bool(run_info.get("is_full_suite", False))
    raw_authz_by_layer = run_info.get("planned_authz_by_layer")
    raw_approvals_by_layer = run_info.get("planned_approvals_by_layer")
    planned_authz_by_layer = raw_authz_by_layer if isinstance(raw_authz_by_layer, dict) else None
    planned_approvals_by_layer = (
        raw_approvals_by_layer if isinstance(raw_approvals_by_layer, dict) else None
    )
    metrics_res = compute_metrics(
        suite,
        outcomes,
        planned_run_count=int(planned) if planned is not None else len(outcomes),
        aborted_env_error=aborted,
        is_full_suite=is_full_suite,
        planned_authz_by_layer=planned_authz_by_layer,
        planned_approval_by_layer=planned_approvals_by_layer,
    )
    run_info["complete"] = metrics_res["complete"]
    audit_summary = _aggregate_audit_summary(outcomes)
    if not env_info:
        env_info = collect_environment_info(
            cfg,
            judge_meta=judge.metadata()
            if judge is not None
            else {"configured": False, "enabled": False},
            evals_dir=evals_dir,
            repo_root=repo_root,
        )
    elif judge is not None:
        env_info.setdefault("models", {})["judge"] = judge.metadata()

    cost = _estimate_cost(outcomes, resets=resets, judge=judge)
    report = build_report_json(
        run_info=run_info,
        metrics_result=metrics_res,
        audit_summary=audit_summary,
        environment=env_info,
        cost=cost,
    )
    write_reports(run_dir, report)
    return redact_tokens(report)
