"""Tests for schema validation, case loader, and static checker L1–L9 (5.10.4, 5.13)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from harness.config import EVALS_DIR, REPO_ROOT
from harness.lint import run_lint
from harness.loader import filter_cases, load_calibration, load_cases, load_suite
from harness.schema import CaseSpec, ChatExpect, GuardStep, SuiteConfig


def test_repo_all_49_cases_pass_lint() -> None:
    issues = run_lint(evals_dir=EVALS_DIR, repo_root=REPO_ROOT)
    assert issues == [], "\n".join(i.format() for i in issues)

    loaded = load_cases(EVALS_DIR)
    assert len(loaded) == 49
    cases = [c for _, c in loaded]
    assert sum(1 for c in cases if c.layer == "tool") == 8
    assert sum(1 for c in cases if c.layer == "guard") == 7
    assert sum(1 for c in cases if c.layer == "dialogue") == 34

    # Verify M1 planned counts and chat turn / verdict counts match PROPOSAL 5.2.2 & 5.7.2
    authz_tg = sum(1 for c in cases if c.layer != "dialogue" for s in c.steps if s.authz)
    authz_d = sum(1 for c in cases if c.layer == "dialogue" for s in c.steps if s.authz)
    chat_msgs = sum(1 for c in cases for s in c.steps if s.do == "chat")
    verdicts = sum(
        1
        for c in cases
        for s in c.steps
        if s.do == "chat"
        for blk in (s.expect.must, s.expect.should)
        if blk and blk.verdict is not None
    )
    assert authz_tg == 30
    assert authz_d == 12
    assert chat_msgs == 52
    assert verdicts == 14

    # Verify M12: G-06 includes approval_execute audit_logs assertions for both 409 failures
    g06 = next(c for c in cases if c.id == "G-06")
    g06_audits = [s for s in g06.steps if s.do == "guard" and s.call == "audit_logs"]
    assert len(g06_audits) == 2
    assert g06_audits[0].params.get("event_type") == "approval_execute"
    assert g06_audits[0].expect.data is not None
    assert g06_audits[0].expect.data.get("items[0].status_code") == 409
    assert g06_audits[0].expect.data.get("items[0].error_code") == "order_not_refundable"
    assert g06_audits[1].expect.data is not None
    assert g06_audits[1].expect.data.get("items[0].error_code") == "refund_amount_exceeded"

    cal = load_calibration(EVALS_DIR)
    assert len(cal) == 12
    labels = {s.expected_label for s in cal}
    assert labels == {"refundable", "not_refundable", "need_info", "escalate", "unclear"}


def _clone_evals_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Copy minimal repo files and evals/ into tmp_path for isolated lint counter-examples."""
    repo = tmp_path / "repo"
    evals = repo / "evals"
    shutil.copytree(EVALS_DIR / "cases", evals / "cases")
    shutil.copy2(EVALS_DIR / "suite.yaml", evals / "suite.yaml")

    for rel in (
        "jwt/users.json",
        "mock-api/seed/seed.json",
        "gateway/generated/tools.manifest.json",
    ):
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst)

    shutil.copytree(REPO_ROOT / "knowledge-base" / "docs", repo / "knowledge-base" / "docs")
    return repo, evals


def test_lint_l1_counterexamples(tmp_path: Path) -> None:
    repo, evals = _clone_evals_workspace(tmp_path)

    # Bad filename prefix & duplicate id & bad layer dir
    dup_path = evals / "cases" / "tool" / "WRONG-name.yaml"
    shutil.copy2(evals / "cases" / "tool" / "T-01-agent-order-isolation.yaml", dup_path)
    issues = run_lint(evals_dir=evals, repo_root=repo, min_cases=30, max_cases=50)
    l1_msgs = [i.message for i in issues if i.rule == "L1"]
    assert any("重复" in m for m in l1_msgs)
    assert any("前缀" in m for m in l1_msgs)
    dup_path.unlink()

    # Layer directory mismatch (G-01 placed in tool/)
    wrong_dir = evals / "cases" / "tool" / "G-99-bad.yaml"
    data = yaml.safe_load(
        (evals / "cases" / "guard" / "G-01-approval-execute-happy-path.yaml").read_text()
    )
    data["id"] = "G-99"
    wrong_dir.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    issues = run_lint(evals_dir=evals, repo_root=repo)
    assert any(i.rule == "L1" and "所在目录" in i.message for i in issues)
    wrong_dir.unlink()

    # Case count out of [30, 50]
    issues = run_lint(evals_dir=evals, repo_root=repo, min_cases=50, max_cases=60)
    assert any(i.rule == "L1" and "用例总数" in i.message for i in issues)

    # Broken case schema
    bad_case = evals / "cases" / "tool" / "T-99-bad.yaml"
    bad_case.write_text("id: INVALID\n", encoding="utf-8")
    issues = run_lint(evals_dir=evals, repo_root=repo)
    assert any(i.rule == "L1" and "schema" in i.message for i in issues)
    bad_case.unlink()

    # Broken suite.yaml
    (evals / "suite.yaml").write_text("name: broken\n", encoding="utf-8")
    issues = run_lint(evals_dir=evals, repo_root=repo)
    assert len(issues) == 1 and issues[0].rule == "L1"


def test_lint_l2_counterexamples(tmp_path: Path) -> None:
    repo, evals = _clone_evals_workspace(tmp_path)

    # Mutate jwt/users.json to create sub/role/regions mismatch + remove chen
    users_path = repo / "jwt" / "users.json"
    users = json.loads(users_path.read_text(encoding="utf-8"))
    users[0]["sub"] = "u9999"
    users[1]["role"] = "supervisor"
    users[3]["regions"] = ["north"]
    users = [u for u in users if u["username"] != "chen"]
    users_path.write_text(json.dumps(users), encoding="utf-8")

    # Also reference unknown actor in a case
    t01 = evals / "cases" / "tool" / "T-01-agent-order-isolation.yaml"
    d = yaml.safe_load(t01.read_text(encoding="utf-8"))
    d["steps"][0]["as"] = "ghost"
    t01.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")

    issues = run_lint(evals_dir=evals, repo_root=repo)
    l2 = [i.message for i in issues if i.rule == "L2"]
    assert any("sub" in m for m in l2)
    assert any("role" in m for m in l2)
    assert any("regions" in m for m in l2)
    assert any("chen" in m for m in l2)
    assert any("ghost" in m for m in l2)


def test_lint_l3_counterexamples(tmp_path: Path) -> None:
    repo, evals = _clone_evals_workspace(tmp_path)

    # 1. Unknown order / ticket / refund / customer in MCP & case.orders & target
    t01 = evals / "cases" / "tool" / "T-01-agent-order-isolation.yaml"
    d = yaml.safe_load(t01.read_text(encoding="utf-8"))
    d["orders"].append("A9999")
    d["steps"][0]["target"] = "A9998"
    d["steps"][2]["args"]["order_id"] = "A9997"
    d["steps"][2]["args"]["ticket_id"] = "T9999"
    d["steps"][2]["args"]["refund_id"] = "R999999"
    d["steps"][2]["args"]["customer_id"] = "C9999"
    t01.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")

    # 2. X9999 without not_found + unknown query order/ticket/refund + bad trace order_id
    d06 = evals / "cases" / "dialogue" / "D-06-policy-7day-no-reason.yaml"
    dd = yaml.safe_load(d06.read_text(encoding="utf-8"))
    dd["steps"][0]["query"] = "查订单 X9999、A8888、工单 T8888、退款 R888888"
    dd["steps"][0]["expect"]["should"]["trace"]["calls"][0]["order_id"] = "A8888"
    d06.write_text(yaml.safe_dump(dd, allow_unicode=True), encoding="utf-8")

    # 3. X9999 accidentally inside seed.json
    seed_path = repo / "mock-api" / "seed" / "seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    seed["orders"].append({"order_id": "X9999", "region": "east", "tickets": [], "refunds": []})
    seed_path.write_text(json.dumps(seed), encoding="utf-8")

    issues = run_lint(evals_dir=evals, repo_root=repo)
    l3 = [i.message for i in issues if i.rule == "L3"]
    assert any("X9999" in m and "种子数据" in m for m in l3)
    assert any("A9999" in m for m in l3)
    assert any("A9998" in m for m in l3)
    assert any("A9997" in m for m in l3)
    assert any("T9999" in m for m in l3)
    assert any("R999999" in m for m in l3)
    assert any("C9999" in m for m in l3)
    assert any("A8888" in m for m in l3)
    assert any("T8888" in m for m in l3)
    assert any("R888888" in m for m in l3)


def test_lint_l3_leaf_scan_and_unknown_ids_exemption(tmp_path: Path) -> None:
    repo, evals = _clone_evals_workspace(tmp_path)

    # M13: nonexistent order ID inside not_contains must be caught by L3
    d19 = evals / "cases" / "dialogue" / "D-19-authz-list-orders-filter.yaml"
    d19_data = yaml.safe_load(d19.read_text(encoding="utf-8"))
    d19_data["steps"][0]["expect"]["must"]["not_contains"].append("B9999")
    d19.write_text(yaml.safe_dump(d19_data, allow_unicode=True), encoding="utf-8")

    # Nonexistent order ID in expect.data key/value, out-of-range AP000003, unauthorized R000105
    t06 = evals / "cases" / "tool" / "T-06-write-gate-pending-and-duplicate.yaml"
    t06_data = yaml.safe_load(t06.read_text(encoding="utf-8"))
    t06_data["steps"][0]["expect"]["data"]["items[?order_id=='A7777']"] = "AP000003"
    t06_data["steps"][0]["expect"]["message_contains"].append("R000105")
    t06.write_text(yaml.safe_dump(t06_data, allow_unicode=True), encoding="utf-8")

    # Bad sentinel ID in suite.yaml
    suite_path = evals / "suite.yaml"
    suite_data = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    suite_data["sentinels"]["B9998"] = ["R999998", "AP000001"]
    suite_path.write_text(yaml.safe_dump(suite_data, allow_unicode=True), encoding="utf-8")

    issues = run_lint(evals_dir=evals, repo_root=repo)
    l3_msgs = [i.message for i in issues if i.rule == "L3"]
    assert any("B9999" in m for m in l3_msgs)
    assert any("A7777" in m for m in l3_msgs)
    assert any("AP000003" in m for m in l3_msgs)
    assert any("R000105" in m for m in l3_msgs)
    assert any("B9998" in m for m in l3_msgs)
    assert any("R999998" in m for m in l3_msgs)
    assert any("AP000001" in m for m in l3_msgs)

    # Adding B9999 to unknown_ids exempts it cleanly
    d19_data["unknown_ids"] = ["B9999"]
    d19.write_text(yaml.safe_dump(d19_data, allow_unicode=True), encoding="utf-8")
    issues_after = run_lint(evals_dir=evals, repo_root=repo)
    assert not any(i.rule == "L3" and "B9999" in i.message for i in issues_after)


def test_lint_h3_dialogue_authz_must_non_empty(tmp_path: Path) -> None:
    repo, evals = _clone_evals_workspace(tmp_path)

    # H3: filter authz step with empty must block -> L8 error
    d19 = evals / "cases" / "dialogue" / "D-19-authz-list-orders-filter.yaml"
    d19_data = yaml.safe_load(d19.read_text(encoding="utf-8"))
    d19_data["steps"][0]["expect"]["must"] = {}
    d19.write_text(yaml.safe_dump(d19_data, allow_unicode=True), encoding="utf-8")

    issues = run_lint(evals_dir=evals, repo_root=repo)
    assert any(i.rule == "L8" and "D-19" in i.file and "expect.must" in i.message for i in issues)


def test_lint_l4_l5_l6_l7_l8_l9_counterexamples(tmp_path: Path) -> None:
    repo, evals = _clone_evals_workspace(tmp_path)

    # L4: unknown tool in mcp, trace, trace_forbid
    t01 = evals / "cases" / "tool" / "T-01-agent-order-isolation.yaml"
    d1 = yaml.safe_load(t01.read_text(encoding="utf-8"))
    d1["steps"][0]["tool"] = "delete_everything"
    t01.write_text(yaml.safe_dump(d1, allow_unicode=True), encoding="utf-8")

    # L5 + L6 + L7 + L8 + L9 mutations on D-06 & D-16 & T-02
    d06 = evals / "cases" / "dialogue" / "D-06-policy-7day-no-reason.yaml"
    dd = yaml.safe_load(d06.read_text(encoding="utf-8"))
    dd["steps"][0]["as"] = "wang"  # L7: wang(east) querying C3051(south) expecting ok!
    dd["steps"][0]["expect"]["should"]["clauses"].append(
        {"doc": "售后制度", "clause": "99.9"}
    )  # L5
    dd["steps"][0]["expect"]["should"]["verdict"] = "maybe_ok"  # L6
    dd["steps"][0]["expect"]["should"]["trace"]["calls"][0]["kind"] = "bogus_kind"  # L6
    dd["steps"][0]["expect"]["should"]["trace"]["calls"][0]["error_code"] = "bogus_code"  # L6
    dd["steps"][0]["expect"]["should"]["trace_forbid"] = [
        {"tool": "fake_tool", "kind": "bad"}
    ]  # L4+L6
    dd["steps"][0]["query"] = "订单 C3051 ${undefined_var}"  # L9
    d06.write_text(yaml.safe_dump(dd, allow_unicode=True), encoding="utf-8")

    # L8: target not in sentinels + dialogue authz without must
    d16 = evals / "cases" / "dialogue" / "D-16-authz-same-question-diff-role.yaml"
    d16_data = yaml.safe_load(d16.read_text(encoding="utf-8"))
    d16_data["steps"][1]["target"] = "C3051"  # not in sentinels
    d16_data["steps"][1]["expect"].pop("must")
    d16.write_text(yaml.safe_dump(d16_data, allow_unicode=True), encoding="utf-8")

    # L9: chat step with no assertions at all
    d32 = evals / "cases" / "dialogue" / "D-32-kbqa-fresh-food.yaml"
    d32_data = yaml.safe_load(d32.read_text(encoding="utf-8"))
    d32_data["steps"][0]["expect"] = {}
    d32.write_text(yaml.safe_dump(d32_data, allow_unicode=True), encoding="utf-8")

    # L7: in-region MCP order expecting denied + L6 bad MCP result/error_code
    t02 = evals / "cases" / "tool" / "T-02-supervisor-order-isolation.yaml"
    t02_data = yaml.safe_load(t02.read_text(encoding="utf-8"))
    t02_data["steps"][2]["expect"] = {"result": "denied", "error_code": "order_access_denied"}
    t02_data["steps"][0]["expect"]["result"] = "weird_result"
    t02_data["steps"][0]["expect"]["error_code"] = "weird_code"
    t02.write_text(yaml.safe_dump(t02_data, allow_unicode=True), encoding="utf-8")

    issues = run_lint(evals_dir=evals, repo_root=repo)
    rules = {i.rule for i in issues}
    for expected_rule in ("L4", "L5", "L6", "L7", "L8", "L9"):
        assert expected_rule in rules, f"Expected {expected_rule} in {rules}"


def test_loader_filter_and_schema_validators() -> None:
    suite = load_suite(EVALS_DIR)
    assert suite.auditor == "sun"
    cases = [c for _, c in load_cases(EVALS_DIR)]

    assert len(filter_cases(cases, layers=["tool"])) == 8
    assert len(filter_cases(cases, categories=["kbqa"])) == 3
    assert [c.id for c in filter_cases(cases, case_ids=["D-01", "T-01"])] == ["T-01", "D-01"]

    with pytest.raises(ValueError, match="未知的 layer"):
        filter_cases(cases, layers=["unknown"])
    with pytest.raises(ValueError, match="未知的 category"):
        filter_cases(cases, categories=["unknown"])
    with pytest.raises(ValueError, match="未知的用例 ID"):
        filter_cases(cases, case_ids=["Z-99"])

    # Schema validators
    with pytest.raises(ValidationError):
        ChatExpect(http={"status": 401}, must={"no_jwt": True})
    with pytest.raises(ValidationError):
        GuardStep.model_validate(
            {"id": "g1", "do": "guard", "as": "li", "call": "approve", "expect": {"status": 200}}
        )
    with pytest.raises(ValidationError):
        SuiteConfig.model_validate(
            {
                "name": "x",
                "thresholds": {
                    "authz_block_rate": 1.0,
                    "approval_compliance": 1.0,
                    "audit_completeness": 1.0,
                    "answer_accuracy": 0.9,
                },
                "accounts": {"wang": {"sub": "u1", "role": "agent", "regions": ["east"]}},
                "auditor": "wang",
            }
        )
    with pytest.raises(ValidationError):
        CaseSpec.model_validate(
            {
                "id": "T-01",
                "title": "bad",
                "layer": "guard",
                "category": "approval",
                "steps": [
                    {
                        "id": "s1",
                        "do": "guard",
                        "as": "li",
                        "call": "audit_logs",
                        "expect": {"status": 200},
                    },
                ],
            }
        )
