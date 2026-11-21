"""Static consistency checker `harness lint` implementing L1–L9 (5.10.4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness.config import EVALS_DIR, REPO_ROOT
from harness.loader import discover_case_files, load_case, load_suite
from harness.schema import (
    ERROR_CODES,
    LAYER_VALUES,
    MCP_RESULTS,
    NONEXISTENT_SENTINEL_IDS,
    TRACE_KINDS,
    VERDICT_LABELS,
    CaseSpec,
    ChatStep,
    GuardStep,
    McpStep,
    SuiteConfig,
)

CLAUSE_HEADING_RE = re.compile(r"^##\s+(?P<doc>[^\s#]+)\s+第\s*(?P<clause>\d+\.\d+)\s*条(?:\s|$)")
VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENTITY_ID_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z]\d{4})(?![A-Za-z0-9_])")
ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9_])([ABCX]\d{4})(?![A-Za-z0-9_])")
TICKET_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(T\d{4})(?![A-Za-z0-9_])")
REFUND_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(R\d{6})(?![A-Za-z0-9_])")
APPROVAL_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(AP\d{6})(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class LintIssue:
    rule: str
    file: str
    message: str

    def format(self) -> str:
        return f"[{self.rule}] {self.file}: {self.message}"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _iter_strings(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.append(k)
            out.extend(_iter_strings(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_iter_strings(item))
    return out


def _extract_var_refs(obj: Any) -> list[str]:
    refs: list[str] = []
    for text in _iter_strings(obj):
        refs.extend(VAR_REF_RE.findall(text))
    return refs


def _load_seed_index(repo_root: Path) -> tuple[dict[str, str], set[str], dict[str, str], set[str]]:
    seed_path = repo_root / "mock-api" / "seed" / "seed.json"
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    orders_region: dict[str, str] = {}
    tickets: set[str] = set()
    refunds_region: dict[str, str] = {}
    customers: set[str] = {str(c["customer_id"]) for c in data.get("customers", [])}
    for order in data.get("orders", []):
        oid = str(order["order_id"])
        region = str(order["region"])
        orders_region[oid] = region
        for ticket in order.get("tickets", []):
            tickets.add(str(ticket["ticket_id"]))
        for refund in order.get("refunds", []):
            refunds_region[str(refund["refund_id"])] = region
    return orders_region, tickets, refunds_region, customers


def _load_kb_clauses(repo_root: Path) -> set[tuple[str, str]]:
    docs_root = repo_root / "knowledge-base" / "docs"
    clauses: set[tuple[str, str]] = set()
    if not docs_root.is_dir():
        return clauses
    for md_path in sorted(docs_root.rglob("*.md")):
        for line in md_path.read_text(encoding="utf-8").splitlines():
            match = CLAUSE_HEADING_RE.match(line.strip())
            if match:
                clauses.add((match.group("doc"), match.group("clause")))
    return clauses


def _load_idp_users(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Read ONLY username, sub, role, regions from jwt/users.json (never password, per L2)."""
    users_path = repo_root / "jwt" / "users.json"
    raw = json.loads(users_path.read_text(encoding="utf-8"))
    users: dict[str, dict[str, Any]] = {}
    for entry in raw:
        username = str(entry["username"])
        users[username] = {
            "sub": str(entry["sub"]),
            "role": str(entry["role"]),
            "regions": [str(r) for r in entry["regions"]],
        }
    return users


def _load_manifest_tool_names(repo_root: Path) -> set[str]:
    manifest_path = repo_root / "gateway" / "generated" / "tools.manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {str(item["name"]) for item in raw}


def _count_case_write_expectations(case: CaseSpec) -> tuple[int, int]:
    """Count planned pending submissions and executed approvals in a case for L3 range checks."""
    submit_count = 0
    approve_exec_count = 0
    for step in case.steps:
        if (
            isinstance(step, McpStep)
            and step.tool == "create_refund"
            and step.expect.result == "pending"
        ):
            submit_count += 1
        elif isinstance(step, ChatStep):
            for blk in (step.expect.must, step.expect.should):
                if (
                    blk
                    and blk.trace
                    and any(
                        c.tool == "create_refund" and c.kind == "pending" for c in blk.trace.calls
                    )
                ):
                    submit_count += 1
                    break
        elif (
            isinstance(step, GuardStep)
            and step.call == "approve"
            and step.expect.status == 200
            and not (step.expect.data and step.expect.data.get("status") == "execution_failed")
        ):
            approve_exec_count += 1
    return submit_count, approve_exec_count


def _check_l1_structure(
    evals_dir: Path,
    repo_root: Path,
    min_cases: int,
    max_cases: int,
) -> tuple[SuiteConfig | None, list[tuple[Path, CaseSpec]], list[LintIssue]]:
    issues: list[LintIssue] = []
    suite: SuiteConfig | None = None
    suite_path = evals_dir / "suite.yaml"
    try:
        suite = load_suite(evals_dir)
    except (OSError, ValueError, ValidationError) as exc:
        issues.append(LintIssue("L1", _rel(suite_path, repo_root), f"suite.yaml 校验失败: {exc}"))

    files = discover_case_files(evals_dir)
    cases: list[tuple[Path, CaseSpec]] = []
    seen_ids: dict[str, Path] = {}

    for path in files:
        rel_path = _rel(path, repo_root)
        try:
            case = load_case(path)
        except (OSError, ValueError, ValidationError) as exc:
            issues.append(LintIssue("L1", rel_path, f"用例 schema 校验失败: {exc}"))
            continue

        if case.id in seen_ids:
            issues.append(
                LintIssue(
                    "L1",
                    rel_path,
                    f"用例 id '{case.id}' 重复（已出现在 {_rel(seen_ids[case.id], repo_root)}）",
                )
            )
        else:
            seen_ids[case.id] = path

        stem = path.stem
        if not (stem == case.id or stem.startswith(f"{case.id}-")):
            issues.append(
                LintIssue("L1", rel_path, f"文件名 '{path.name}' 前缀与用例 id '{case.id}' 不一致")
            )

        parent_name = path.parent.name
        if parent_name not in LAYER_VALUES or parent_name != case.layer:
            issues.append(
                LintIssue(
                    "L1",
                    rel_path,
                    f"所在目录 '{parent_name}' 与用例 layer '{case.layer}' 不一致",
                )
            )

        cases.append((path, case))

    total_cases = len(files)
    if not (min_cases <= total_cases <= max_cases):
        issues.append(
            LintIssue(
                "L1",
                _rel(evals_dir / "cases", repo_root),
                f"用例总数为 {total_cases}，必须在 {min_cases}–{max_cases} 之间",
            )
        )

    return suite, cases, issues


def _check_l2_accounts(
    suite: SuiteConfig,
    cases: list[tuple[Path, CaseSpec]],
    idp_users: dict[str, dict[str, Any]],
    evals_dir: Path,
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    suite_rel = _rel(evals_dir / "suite.yaml", repo_root)

    for username, spec in suite.accounts.items():
        if username not in idp_users:
            issues.append(LintIssue("L2", suite_rel, f"账号 '{username}' 不存在于 jwt/users.json"))
            continue
        idp = idp_users[username]
        if spec.sub != idp["sub"]:
            issues.append(
                LintIssue(
                    "L2",
                    suite_rel,
                    f"账号 '{username}' 的 sub ({spec.sub}) "
                    f"与 jwt/users.json ({idp['sub']}) 不一致",
                )
            )
        if spec.role != idp["role"]:
            issues.append(
                LintIssue(
                    "L2",
                    suite_rel,
                    f"账号 '{username}' 的 role ({spec.role}) "
                    f"与 jwt/users.json ({idp['role']}) 不一致",
                )
            )
        if set(spec.regions) != set(idp["regions"]):
            issues.append(
                LintIssue(
                    "L2",
                    suite_rel,
                    f"账号 '{username}' 的 regions ({spec.regions}) "
                    f"与 jwt/users.json ({idp['regions']}) 不一致",
                )
            )

    for path, case in cases:
        rel_path = _rel(path, repo_root)
        for step in case.steps:
            if step.actor not in suite.accounts:
                issues.append(
                    LintIssue(
                        "L2",
                        rel_path,
                        f"步骤 '{step.id}' 的 as='{step.actor}' 不在 suite.yaml 的 accounts 中",
                    )
                )
    return issues


def _check_l3_seed_refs(
    suite: SuiteConfig,
    cases: list[tuple[Path, CaseSpec]],
    orders_region: dict[str, str],
    tickets: set[str],
    refunds_region: dict[str, str],
    customers: set[str],
    evals_dir: Path,
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    seed_rel = "mock-api/seed/seed.json"
    for sentinel_id in sorted(NONEXISTENT_SENTINEL_IDS):
        if sentinel_id in orders_region or sentinel_id in tickets or sentinel_id in refunds_region:
            issues.append(
                LintIssue("L3", seed_rel, f"不存在标识 '{sentinel_id}' 意外出现在种子数据中")
            )

    known_entities = set(orders_region) | tickets | customers
    known_refunds_base = set(refunds_region)
    max_seed_refund = max((int(rid[1:]) for rid in refunds_region), default=100)

    # Check suite.yaml sentinels keys and values against seed IDs (M13)
    suite_rel = _rel(evals_dir / "suite.yaml", repo_root)
    reported_suite: set[str] = set()
    for text in _iter_strings(suite.sentinels):
        cleaned = VAR_REF_RE.sub("", text)
        for eid in ENTITY_ID_RE.findall(cleaned):
            if eid not in known_entities and eid not in reported_suite:
                reported_suite.add(eid)
                issues.append(LintIssue("L3", suite_rel, f"sentinels 中的标识 '{eid}' 不在种子中"))
        for rid in REFUND_ID_RE.findall(cleaned):
            if rid not in known_refunds_base and rid not in reported_suite:
                reported_suite.add(rid)
                issues.append(
                    LintIssue("L3", suite_rel, f"sentinels 中的退款单号 '{rid}' 不在种子中")
                )
        for aid in APPROVAL_ID_RE.findall(cleaned):
            if aid not in reported_suite:
                reported_suite.add(aid)
                issues.append(
                    LintIssue("L3", suite_rel, f"sentinels 中不应引用未落库的审批单号 '{aid}'")
                )

    for path, case in cases:
        rel_path = _rel(path, repo_root)
        reported_ids: set[str] = set()
        for oid in case.orders:
            if oid not in orders_region:
                reported_ids.add(oid)
                issues.append(LintIssue("L3", rel_path, f"orders 中的订单号 '{oid}' 不在种子中"))

        submit_count, approve_exec_count = _count_case_write_expectations(case)
        allowed_approvals = {f"AP{i:06d}" for i in range(1, submit_count + 1)}
        allowed_refunds = known_refunds_base | {
            f"R{max_seed_refund + i:06d}" for i in range(1, approve_exec_count + 1)
        }
        exempt_ids = set(case.unknown_ids)

        case_dump = case.model_dump(by_alias=True, exclude={"unknown_ids"})
        for text in _iter_strings(case_dump):
            cleaned = VAR_REF_RE.sub("", text)
            for eid in ENTITY_ID_RE.findall(cleaned):
                if eid in known_entities or eid in exempt_ids or eid in reported_ids:
                    continue
                reported_ids.add(eid)
                issues.append(
                    LintIssue(
                        "L3",
                        rel_path,
                        f"标识 '{eid}' 不在种子数据中"
                        "（如确为故意使用的不存在标识，请加入 unknown_ids）",
                    )
                )
            for rid in REFUND_ID_RE.findall(cleaned):
                if rid in allowed_refunds or rid in exempt_ids or rid in reported_ids:
                    continue
                reported_ids.add(rid)
                issues.append(
                    LintIssue(
                        "L3",
                        rel_path,
                        f"退款单号 '{rid}' 不在种子数据或本用例新建范围内"
                        "（如确为故意使用，请加入 unknown_ids）",
                    )
                )
            for aid in APPROVAL_ID_RE.findall(cleaned):
                if aid in allowed_approvals or aid in exempt_ids or aid in reported_ids:
                    continue
                reported_ids.add(aid)
                issues.append(
                    LintIssue(
                        "L3",
                        rel_path,
                        f"审批单号 '{aid}' 超出本用例提交次数范围 (1..{submit_count})"
                        "（如确为故意使用，请加入 unknown_ids）",
                    )
                )

    return issues


def _check_l4_tools(
    cases: list[tuple[Path, CaseSpec]],
    manifest_tools: set[str],
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for path, case in cases:
        rel_path = _rel(path, repo_root)
        for step in case.steps:
            if isinstance(step, McpStep):
                if step.tool not in manifest_tools:
                    issues.append(
                        LintIssue(
                            "L4",
                            rel_path,
                            f"步骤 '{step.id}' 调用的工具 '{step.tool}' "
                            "不在 tools.manifest.json 中",
                        )
                    )
            elif isinstance(step, ChatStep):
                for block in (step.expect.must, step.expect.should):
                    if not block:
                        continue
                    if block.trace:
                        for call in block.trace.calls:
                            if call.tool not in manifest_tools:
                                issues.append(
                                    LintIssue(
                                        "L4",
                                        rel_path,
                                        f"步骤 '{step.id}' trace 期望的工具 '{call.tool}' "
                                        "不在 tools.manifest.json 中",
                                    )
                                )
                    if block.trace_forbid:
                        for item in block.trace_forbid:
                            if item.tool not in manifest_tools:
                                issues.append(
                                    LintIssue(
                                        "L4",
                                        rel_path,
                                        f"步骤 '{step.id}' trace_forbid 的工具 '{item.tool}' "
                                        "不在 tools.manifest.json 中",
                                    )
                                )
    return issues


def _check_l5_clauses(
    cases: list[tuple[Path, CaseSpec]],
    kb_clauses: set[tuple[str, str]],
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for path, case in cases:
        rel_path = _rel(path, repo_root)
        for step in case.steps:
            if not isinstance(step, ChatStep):
                continue
            for block in (step.expect.must, step.expect.should):
                if not block:
                    continue
                for field_name in ("clauses", "clauses_any", "citations"):
                    refs = getattr(block, field_name)
                    if not refs:
                        continue
                    for ref in refs:
                        if (ref.doc, ref.clause) not in kb_clauses:
                            issues.append(
                                LintIssue(
                                    "L5",
                                    rel_path,
                                    f"步骤 '{step.id}' {field_name} 引用了不存在的条款 "
                                    f"《{ref.doc}》第 {ref.clause} 条",
                                )
                            )
    return issues


def _check_l6_enums(
    cases: list[tuple[Path, CaseSpec]],
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for path, case in cases:
        rel_path = _rel(path, repo_root)
        for step in case.steps:
            if isinstance(step, McpStep):
                if step.expect.result not in MCP_RESULTS:
                    issues.append(
                        LintIssue(
                            "L6",
                            rel_path,
                            f"步骤 '{step.id}' 的 result='{step.expect.result}' 不在允许枚举内",
                        )
                    )
                if step.expect.error_code and step.expect.error_code not in ERROR_CODES:
                    issues.append(
                        LintIssue(
                            "L6",
                            rel_path,
                            f"步骤 '{step.id}' 的 error_code='{step.expect.error_code}' "
                            "不在允许枚举内",
                        )
                    )
            elif isinstance(step, GuardStep):
                if step.expect.error_code and step.expect.error_code not in ERROR_CODES:
                    issues.append(
                        LintIssue(
                            "L6",
                            rel_path,
                            f"步骤 '{step.id}' 的 error_code='{step.expect.error_code}' "
                            "不在允许枚举内",
                        )
                    )
            elif isinstance(step, ChatStep):
                if (
                    step.expect.http
                    and step.expect.http.code
                    and step.expect.http.code not in ERROR_CODES
                ):
                    issues.append(
                        LintIssue(
                            "L6",
                            rel_path,
                            f"步骤 '{step.id}' 的 http.code='{step.expect.http.code}' "
                            "不在允许枚举内",
                        )
                    )
                for block in (step.expect.must, step.expect.should):
                    if not block:
                        continue
                    if block.verdict is not None and block.verdict not in VERDICT_LABELS:
                        issues.append(
                            LintIssue(
                                "L6",
                                rel_path,
                                f"步骤 '{step.id}' 的 verdict='{block.verdict}' 不在四标签内",
                            )
                        )
                    if block.trace:
                        for call in block.trace.calls:
                            if call.kind not in TRACE_KINDS:
                                issues.append(
                                    LintIssue(
                                        "L6",
                                        rel_path,
                                        f"步骤 '{step.id}' trace.kind='{call.kind}' "
                                        "不在 4.16 枚举内",
                                    )
                                )
                            if call.error_code and call.error_code not in ERROR_CODES:
                                issues.append(
                                    LintIssue(
                                        "L6",
                                        rel_path,
                                        f"步骤 '{step.id}' trace.error_code='{call.error_code}' "
                                        "不在允许枚举内",
                                    )
                                )
                    if block.trace_forbid:
                        for item in block.trace_forbid:
                            if item.kind is not None and item.kind not in TRACE_KINDS:
                                issues.append(
                                    LintIssue(
                                        "L6",
                                        rel_path,
                                        f"步骤 '{step.id}' trace_forbid.kind='{item.kind}' "
                                        "不在 4.16 枚举内",
                                    )
                                )
    return issues


def _check_l7_authz_consistency(
    suite: SuiteConfig,
    cases: list[tuple[Path, CaseSpec]],
    orders_region: dict[str, str],
    refunds_region: dict[str, str],
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for path, case in cases:
        rel_path = _rel(path, repo_root)
        for step in case.steps:
            if step.token != "valid" or step.actor not in suite.accounts:
                continue
            account = suite.accounts[step.actor]
            regions = set(account.regions)

            if isinstance(step, McpStep):
                oid = step.args.get("order_id")
                if (
                    step.tool in ("get_order", "create_refund")
                    and isinstance(oid, str)
                    and oid in orders_region
                ):
                    in_region = orders_region[oid] in regions
                    expects_denied = (
                        step.expect.result == "denied"
                        and step.expect.error_code == "order_access_denied"
                    )
                    if not in_region and not expects_denied:
                        issues.append(
                            LintIssue(
                                "L7",
                                rel_path,
                                f"步骤 '{step.id}' 账号 {step.actor}({account.regions}) "
                                f"无权访问订单 {oid}({orders_region[oid]})，但未期望被拒",
                            )
                        )
                    if in_region and expects_denied:
                        issues.append(
                            LintIssue(
                                "L7",
                                rel_path,
                                f"步骤 '{step.id}' 账号 {step.actor}({account.regions}) "
                                f"有权访问订单 {oid}({orders_region[oid]})，却期望被拒",
                            )
                        )

                rid = step.args.get("refund_id")
                if step.tool == "get_refund" and isinstance(rid, str) and rid in refunds_region:
                    in_region = refunds_region[rid] in regions
                    expects_denied = (
                        step.expect.result == "denied"
                        and step.expect.error_code == "refund_access_denied"
                    )
                    if not in_region and not expects_denied:
                        issues.append(
                            LintIssue(
                                "L7",
                                rel_path,
                                f"步骤 '{step.id}' 账号 {step.actor} "
                                f"无权访问退款单 {rid}，但未期望被拒",
                            )
                        )
                    if in_region and expects_denied:
                        issues.append(
                            LintIssue(
                                "L7",
                                rel_path,
                                f"步骤 '{step.id}' 账号 {step.actor} "
                                f"有权访问退款单 {rid}，却期望被拒",
                            )
                        )

            elif isinstance(step, ChatStep) and step.expect.http is None:
                # Identify target seed order if step addresses a single known order
                target_order: str | None = None
                if step.target and step.target in orders_region:
                    target_order = step.target
                else:
                    trace_orders = {
                        c.order_id
                        for blk in (step.expect.must, step.expect.should)
                        if blk and blk.trace
                        for c in blk.trace.calls
                        if c.order_id and c.order_id in orders_region
                    }
                    query_orders = {
                        o for o in ORDER_ID_RE.findall(step.query) if o in orders_region
                    }
                    candidates = trace_orders or query_orders
                    if len(candidates) == 1:
                        target_order = next(iter(candidates))

                if target_order is None:
                    continue

                in_region = orders_region[target_order] in regions
                all_trace_calls = [
                    c
                    for blk in (step.expect.must, step.expect.should)
                    if blk and blk.trace
                    for c in blk.trace.calls
                ]
                expects_order_ok = any(
                    c.tool == "get_order" and c.kind == "ok" for c in all_trace_calls
                )
                expects_order_denied = step.authz == "deny" or any(
                    c.tool == "get_order"
                    and c.kind == "denied"
                    and c.error_code == "order_access_denied"
                    for c in all_trace_calls
                )

                if not in_region and (expects_order_ok or not expects_order_denied):
                    issues.append(
                        LintIssue(
                            "L7",
                            rel_path,
                            f"步骤 '{step.id}' 账号 {step.actor}({account.regions}) "
                            f"无权访问订单 {target_order}({orders_region[target_order]})，"
                            "但权限期望未要求拒绝",
                        )
                    )
                if in_region and expects_order_denied:
                    issues.append(
                        LintIssue(
                            "L7",
                            rel_path,
                            f"步骤 '{step.id}' 账号 {step.actor}({account.regions}) "
                            f"有权访问订单 {target_order}({orders_region[target_order]})，"
                            "却期望被拒",
                        )
                    )

    return issues


def _check_l8_sentinels_and_must(
    suite: SuiteConfig,
    cases: list[tuple[Path, CaseSpec]],
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for path, case in cases:
        rel_path = _rel(path, repo_root)
        for step in case.steps:
            if step.target is not None and step.target not in suite.sentinels:
                issues.append(
                    LintIssue(
                        "L8",
                        rel_path,
                        f"步骤 '{step.id}' 的 target='{step.target}' "
                        "不在 suite.yaml 的 sentinels 中",
                    )
                )
            if case.layer == "dialogue" and step.authz is not None:
                if isinstance(step, ChatStep):
                    if step.expect.must is None or not step.expect.must.has_any():
                        issues.append(
                            LintIssue(
                                "L8",
                                rel_path,
                                f"对话层 authz 步骤 '{step.id}' 至少需要一条 expect.must 断言",
                            )
                        )
                else:
                    if step.severity != "must":
                        issues.append(
                            LintIssue(
                                "L8",
                                rel_path,
                                f"对话层非 chat 的 authz 步骤 '{step.id}' 必须标记 severity: must",
                            )
                        )
    return issues


def _check_l9_assertions_and_vars(
    cases: list[tuple[Path, CaseSpec]],
    repo_root: Path,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for path, case in cases:
        rel_path = _rel(path, repo_root)
        defined_vars: set[str] = set()
        for step in case.steps:
            if isinstance(step, ChatStep):
                has_assert = (
                    step.expect.http is not None
                    or (step.expect.must is not None and step.expect.must.has_any())
                    or (step.expect.should is not None and step.expect.should.has_any())
                )
                if not has_assert:
                    issues.append(
                        LintIssue("L9", rel_path, f"对话步骤 '{step.id}' 至少需要一条断言")
                    )

            # Check variable references in step payload & expectations (excluding own save)
            step_dump = step.model_dump(by_alias=True, exclude={"save"})
            for var_name in _extract_var_refs(step_dump):
                if var_name not in defined_vars:
                    issues.append(
                        LintIssue(
                            "L9",
                            rel_path,
                            f"步骤 '{step.id}' 引用了尚未通过 save 定义的变量 '${{{var_name}}}'",
                        )
                    )

            if step.save:
                defined_vars.update(step.save.keys())
    return issues


def run_lint(
    evals_dir: Path = EVALS_DIR,
    repo_root: Path | None = None,
    *,
    min_cases: int = 30,
    max_cases: int = 50,
) -> list[LintIssue]:
    """Run all static checks L1–L9 without requiring any running service."""
    root = repo_root or REPO_ROOT
    suite, cases, issues = _check_l1_structure(evals_dir, root, min_cases, max_cases)
    if suite is None:
        return issues

    idp_users = _load_idp_users(root)
    orders_region, tickets, refunds_region, customers = _load_seed_index(root)
    manifest_tools = _load_manifest_tool_names(root)
    kb_clauses = _load_kb_clauses(root)

    issues.extend(_check_l2_accounts(suite, cases, idp_users, evals_dir, root))
    issues.extend(
        _check_l3_seed_refs(
            suite, cases, orders_region, tickets, refunds_region, customers, evals_dir, root
        )
    )
    issues.extend(_check_l4_tools(cases, manifest_tools, root))
    issues.extend(_check_l5_clauses(cases, kb_clauses, root))
    issues.extend(_check_l6_enums(cases, root))
    issues.extend(_check_l7_authz_consistency(suite, cases, orders_region, refunds_region, root))
    issues.extend(_check_l8_sentinels_and_must(suite, cases, root))
    issues.extend(_check_l9_assertions_and_vars(cases, root))
    return issues
