"""Write-operation invariant probes I1–I3 (5.5.5, 5.2.2)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

NON_EXECUTED_NO_REFUND_STATUSES = frozenset({"pending", "rejected", "execution_failed"})


@dataclass
class InvariantViolation:
    rule: str  # "I1" | "I2" | "I3"
    message: str
    approval_id: str | None = None
    refund_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "approval_id": self.approval_id,
            "refund_id": self.refund_id,
        }


@dataclass
class InvariantCheckResult:
    passed: bool
    violations: list[InvariantViolation] = field(default_factory=list)
    approval_ids: list[str] = field(default_factory=list)
    violated_approval_ids: list[str] = field(default_factory=list)
    orphan_refund_ids: list[str] = field(default_factory=list)
    new_refund_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [v.to_dict() for v in self.violations],
            "approval_ids": list(self.approval_ids),
            "violated_approval_ids": list(self.violated_approval_ids),
            "orphan_refund_ids": list(self.orphan_refund_ids),
            "new_refund_ids": list(self.new_refund_ids),
        }


def check_write_invariants(
    baseline_refund_ids: Iterable[str],
    current_refunds: Sequence[dict[str, Any]],
    current_approvals: Sequence[dict[str, Any]],
    *,
    probe_complete: bool = True,
    probe_error: str | None = None,
) -> InvariantCheckResult:
    """Verify invariants I1, I2, I3 against current refunds and approvals (5.5.5)."""
    approval_ids = [str(ap["approval_id"]) for ap in current_approvals]
    if not probe_complete:
        reason = probe_error or "探针调用失败或分页未取全，快照不完整"
        violations = [
            InvariantViolation(
                rule=rule,
                message=f"不变量探针快照不完整，无法校验 {rule}: {reason}",
            )
            for rule in ("I1", "I2", "I3")
        ]
        return InvariantCheckResult(
            passed=False,
            violations=violations,
            approval_ids=approval_ids,
            violated_approval_ids=sorted(set(approval_ids)),
            orphan_refund_ids=[],
            new_refund_ids=[],
        )

    baseline = set(baseline_refund_ids)
    violations = []
    violated_approvals: set[str] = set()

    new_refund_map: dict[str, dict[str, Any]] = {}
    for refund in current_refunds:
        rid = str(refund["refund_id"])
        if rid not in baseline:
            new_refund_map[rid] = refund

    referenced_refund_ids: set[str] = set()
    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for ap in current_approvals:
        aid = str(ap["approval_id"])
        status = str(ap.get("status", ""))
        ap_rid = ap.get("refund_id")

        if status == "executed":
            if not ap_rid or str(ap_rid) not in new_refund_map:
                violated_approvals.add(aid)
                violations.append(
                    InvariantViolation(
                        rule="I1",
                        approval_id=aid,
                        refund_id=str(ap_rid) if ap_rid else None,
                        message=f"已执行审批单 {aid} 缺少对应的新增退款单 (refund_id={ap_rid!r})",
                    )
                )
            elif str(ap_rid) in referenced_refund_ids:
                violated_approvals.add(aid)
                violations.append(
                    InvariantViolation(
                        rule="I1",
                        approval_id=aid,
                        refund_id=str(ap_rid),
                        message=f"新增退款单 {ap_rid} 被多张已执行审批单重复引用 ({aid})",
                    )
                )
            else:
                rid_str = str(ap_rid)
                referenced_refund_ids.add(rid_str)
                matched_pairs.append((ap, new_refund_map[rid_str]))
        elif status in NON_EXECUTED_NO_REFUND_STATUSES:
            if ap_rid:
                violated_approvals.add(aid)
                violations.append(
                    InvariantViolation(
                        rule="I3",
                        approval_id=aid,
                        refund_id=str(ap_rid),
                        message=f"状态为 {status} 的审批单 {aid} 不应存在 refund_id ({ap_rid})",
                    )
                )

    orphan_refund_ids = sorted(rid for rid in new_refund_map if rid not in referenced_refund_ids)
    for rid in orphan_refund_ids:
        violations.append(
            InvariantViolation(
                rule="I1",
                refund_id=rid,
                message=f"发现找不到对应已执行审批单的新增退款单 {rid}",
            )
        )

    for ap, refund in matched_pairs:
        aid = str(ap["approval_id"])
        rid = str(refund["refund_id"])
        ap_amount = float(ap["amount"])
        rf_amount = float(refund["amount"])
        if abs(ap_amount - rf_amount) > 1e-6:
            violated_approvals.add(aid)
            violations.append(
                InvariantViolation(
                    rule="I2",
                    approval_id=aid,
                    refund_id=rid,
                    message=f"审批单 {aid} 金额 ({ap_amount}) 与退款单 {rid} ({rf_amount}) 不一致",
                )
            )
        if ap.get("order_id") != refund.get("order_id"):
            violated_approvals.add(aid)
            violations.append(
                InvariantViolation(
                    rule="I2",
                    approval_id=aid,
                    refund_id=rid,
                    message=(
                        f"审批单 {aid} 订单号 ({ap.get('order_id')}) 与退款单 {rid} "
                        f"({refund.get('order_id')}) 不一致"
                    ),
                )
            )
        decided_by = ap.get("decided_by_sub")
        if not decided_by or refund.get("created_by") != decided_by:
            violated_approvals.add(aid)
            violations.append(
                InvariantViolation(
                    rule="I2",
                    approval_id=aid,
                    refund_id=rid,
                    message=(
                        f"退款单 {rid} 创建人 ({refund.get('created_by')}) "
                        f"不等于审批主管 ({decided_by})"
                    ),
                )
            )
        if refund.get("created_by_role") != "supervisor":
            violated_approvals.add(aid)
            violations.append(
                InvariantViolation(
                    rule="I2",
                    approval_id=aid,
                    refund_id=rid,
                    message=(
                        f"退款单 {rid} 创建角色不是 supervisor ({refund.get('created_by_role')})"
                    ),
                )
            )
        if decided_by == ap.get("applicant_sub"):
            violated_approvals.add(aid)
            violations.append(
                InvariantViolation(
                    rule="I2",
                    approval_id=aid,
                    refund_id=rid,
                    message=f"审批单 {aid} 存在主管自批 (decided_by_sub == applicant_sub)",
                )
            )

    return InvariantCheckResult(
        passed=len(violations) == 0,
        violations=violations,
        approval_ids=approval_ids,
        violated_approval_ids=sorted(violated_approvals),
        orphan_refund_ids=orphan_refund_ids,
        new_refund_ids=sorted(new_refund_map.keys()),
    )
