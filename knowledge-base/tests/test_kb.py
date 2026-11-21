"""Unit tests for knowledge-base lint.py, sync.py, and probe.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from scripts import lint, probe, sync

REAL_KB_DIR = Path(__file__).resolve().parent.parent


def _copy_real_kb(tmp_path: Path) -> Path:
    """Copy the real knowledge-base files (kb.yaml, probes.yaml, docs/) into tmp_path."""
    kb_dir = tmp_path / "kb"
    shutil.copytree(REAL_KB_DIR / "docs", kb_dir / "docs")
    shutil.copy2(REAL_KB_DIR / "kb.yaml", kb_dir / "kb.yaml")
    shutil.copy2(REAL_KB_DIR / "probes.yaml", kb_dir / "probes.yaml")
    return kb_dir


def _make_minimal_kb(tmp_path: Path, doc_text: str, filename: str = "测试制度.md") -> Path:
    kb_dir = tmp_path / "minimal_kb"
    doc_path = kb_dir / "docs" / "policy" / filename
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(doc_text, encoding="utf-8")
    kb_yaml = {
        "datasets": [
            {
                "key": "policy",
                "name": "测试制度",
                "documents": [f"docs/policy/{filename}"],
            }
        ],
        "indexing": {
            "indexing_technique": "high_quality",
            "doc_form": "text_model",
            "doc_language": "Chinese",
            "process_rule": {
                "mode": "custom",
                "rules": {
                    "pre_processing_rules": [
                        {"id": "remove_extra_spaces", "enabled": True},
                        {"id": "remove_urls_emails", "enabled": False},
                    ],
                    "segmentation": {
                        "separator": "\n\n",
                        "max_tokens": 1000,
                        "chunk_overlap": 0,
                    },
                },
            },
        },
        "retrieval_model": {
            "search_method": "semantic_search",
            "reranking_enable": False,
            "top_k": 8,
            "score_threshold_enabled": False,
        },
    }
    (kb_dir / "kb.yaml").write_text(yaml.safe_dump(kb_yaml, allow_unicode=True), encoding="utf-8")
    return kb_dir


# ---------------------------------------------------------------------------
# lint.py tests
# ---------------------------------------------------------------------------


def test_real_knowledge_base_passes_lint() -> None:
    errors, reports = lint.lint_all(REAL_KB_DIR)
    assert errors == [], [e.format() for e in errors]
    counts = {r.doc_name: r.clause_count for r in reports}
    assert counts == {
        "售后制度": 35,
        "客服操作手册": 18,
        "故障排查手册": 12,
    }
    assert lint.main(["--kb-dir", str(REAL_KB_DIR)]) == 0


def test_lint_w1_non_md_invalid_utf8_and_bom(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb_w1"
    (kb_dir / "docs").mkdir(parents=True)
    (kb_dir / "docs" / "bad.txt").write_text("hello", encoding="utf-8")
    (kb_dir / "docs" / "invalid_utf8.md").write_bytes(b"\xff\xfe# invalid")
    (kb_dir / "docs" / "bom.md").write_bytes("\ufeff# 制度\n说明。\n".encode())

    errors, _ = lint.lint_all(kb_dir)
    w1_errors = [e for e in errors if e.rule == "W1"]
    assert len(w1_errors) == 3


def test_lint_w2_h1_errors(tmp_path: Path) -> None:
    # Missing H1
    kb1 = _make_minimal_kb(tmp_path / "no_h1", "## 测试制度 第 1.1 条 标题\n规则：内容。\n")
    errors1, _ = lint.lint_all(kb1)
    assert any(e.rule == "W2" for e in errors1)

    # Duplicate H1
    kb2 = _make_minimal_kb(
        tmp_path / "dup_h1",
        "# 测试制度\n说明文本。\n\n# 重复标题\n说明。\n",
    )
    errors2, _ = lint.lint_all(kb2)
    assert any(e.rule == "W2" and "只能有一个一级标题" in e.message for e in errors2)

    # H1 without description
    kb3 = _make_minimal_kb(
        tmp_path / "empty_h1",
        "# 测试制度\n\n## 测试制度 第 1.1 条 标题\n规则：内容。\n",
    )
    errors3, _ = lint.lint_all(kb3)
    assert any(e.rule == "W2" and "说明" in e.message for e in errors3)


def test_lint_w3_and_w4_headings(tmp_path: Path) -> None:
    text = (
        "# 测试制度\n"
        "用途说明。\n\n"
        "## 其他文档 第 1.1 条 文档名不匹配\n"
        "规则：内容。\n\n"
        "## 格式错误的标题\n"
        "规则：内容。\n\n"
        "### 三级标题不允许\n"
        "规则：内容。\n"
    )
    kb_dir = _make_minimal_kb(tmp_path, text)
    errors, _ = lint.lint_all(kb_dir)
    rules = {e.rule for e in errors}
    assert "W3" in rules
    assert "W4" in rules


def test_lint_w5_leading_symbols_and_book_title_marks(tmp_path: Path) -> None:
    text = (
        "# 《测试制度》\n"
        "。以句号开头的说明。\n\n"
        "## 《测试制度》 第 1.1 条 。标点开头标题\n"
        "《适用：以书名号开头。\n"
    )
    kb_dir = _make_minimal_kb(tmp_path, text)
    errors, _ = lint.lint_all(kb_dir)
    w5_errors = [e for e in errors if e.rule == "W5"]
    assert len(w5_errors) >= 4


def test_lint_w6_blank_lines(tmp_path: Path) -> None:
    # Blank line inside clause
    kb1 = _make_minimal_kb(
        tmp_path / "inside",
        "# 测试制度\n说明。\n\n## 测试制度 第 1.1 条 条款一\n"
        "适用：所有订单。\n\n规则：内部空行。\n",
    )
    errors1, _ = lint.lint_all(kb1)
    assert any(e.rule == "W6" and "内部" in e.message for e in errors1)

    # Double blank line between clauses
    kb2 = _make_minimal_kb(
        tmp_path / "double",
        "# 测试制度\n说明。\n\n\n## 测试制度 第 1.1 条 条款一\n规则：内容。\n",
    )
    errors2, _ = lint.lint_all(kb2)
    assert any(e.rule == "W6" and "连续空行" in e.message for e in errors2)

    # Missing blank line before H2
    kb3 = _make_minimal_kb(
        tmp_path / "missing",
        "# 测试制度\n说明。\n## 测试制度 第 1.1 条 条款一\n规则：内容。\n",
    )
    errors3, _ = lint.lint_all(kb3)
    assert any(e.rule == "W6" and "恰好空一行" in e.message for e in errors3)


def test_lint_w7_max_length(tmp_path: Path) -> None:
    long_rule = "规则：" + "长" * 600 + "。"
    kb_dir = _make_minimal_kb(
        tmp_path,
        f"# 测试制度\n说明。\n\n## 测试制度 第 1.1 条 超长条款\n{long_rule}\n",
    )
    errors, _ = lint.lint_all(kb_dir)
    assert any(e.rule == "W7" for e in errors)


def test_lint_w8_forbidden_markup(tmp_path: Path) -> None:
    text = (
        "# 测试制度\n"
        "用途说明。\n\n"
        "## 测试制度 第 1.1 条 尖括号\n"
        "规则：天数 <= 7 天。\n\n"
        "## 测试制度 第 1.2 条 表格与链接\n"
        "规则：查看 [链接](https://example.com) 和 ![图片](a.png)。\n"
        "| 列一 | 列二 |\n"
    )
    kb_dir = _make_minimal_kb(tmp_path, text)
    errors, _ = lint.lint_all(kb_dir)
    w8_messages = [e.message for e in errors if e.rule == "W8"]
    assert any("尖括号" in m for m in w8_messages)
    assert any("链接" in m for m in w8_messages)
    assert any("图片" in m for m in w8_messages)
    assert any("表格" in m for m in w8_messages)


def test_lint_w9_labels_and_w13_deprecated(tmp_path: Path) -> None:
    # Bad label in general document
    kb_bad = _make_minimal_kb(
        tmp_path / "bad_label",
        "# 测试制度\n说明。\n\n## 测试制度 第 1.1 条 标题\n说明：错误标签。\n",
    )
    errors_bad, _ = lint.lint_all(kb_bad)
    assert any(e.rule == "W9" for e in errors_bad)

    # Deprecated clause (W13) is accepted
    kb_dep = _make_minimal_kb(
        tmp_path / "deprecated",
        "# 测试制度\n说明。\n\n## 测试制度 第 1.1 条 已废止条款\n本条已废止\n",
    )
    errors_dep, _ = lint.lint_all(kb_dep)
    assert errors_dep == []

    # Troubleshoot handbook uses 现象/排查/认定 for ch. 1-3 and 适用/规则/处理/例外 for ch. 4
    kb_real = _copy_real_kb(tmp_path / "ts_bad")
    ts_path = kb_real / "docs" / "handbook" / "故障排查手册.md"
    ts_text = ts_path.read_text(encoding="utf-8").replace("现象：数码家电", "规则：数码家电", 1)
    ts_path.write_text(ts_text, encoding="utf-8")
    errors_ts, _ = lint.lint_all(kb_real)
    assert any(e.rule == "W9" and "故障排查手册" in e.file for e in errors_ts)


def test_lint_w10_enum_and_field_annotations(tmp_path: Path) -> None:
    assert "days_since_shipped" not in lint.W10_TERM_ZH_MAP
    assert "request_type" not in lint.W10_TERM_ZH_MAP

    # Positive case: properly annotated enums/fields + whitelisted error codes/sample IDs in parens
    good_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 正确标注\n"
        "适用：商品类目（category）为数码家电（electronics），客户等级（level）为普通（regular）"
        "或会员（vip），订单状态（status）为已签收（delivered），"
        "退款进度（refund_status）为未退款（none）。\n"
        "规则：工单申请类型（type）为退货退款（return_refund）或仅退款（refund_only），"
        "原因类别（reason_category）为质量问题（quality_issue），"
        "已签收天数（days_since_delivery）不超过 7 天（含第 7 天）；"
        "审批状态为待审批（pending）或已执行（executed）。\n"
        "处理：单号示例（如 A1023、AP000001、R000101）及错误码（对应错误码 order_not_found、"
        "token_expired、invalid_claims）、上游 5xx 或普通词 HTTP 与 SKU 不受影响。\n"
    )
    kb_good = _make_minimal_kb(tmp_path / "w10_good", good_text)
    errors_good, _ = lint.lint_all(kb_good)
    assert errors_good == [], [e.format() for e in errors_good]

    # Negative case: missing Chinese, half-width parens, wrong Chinese, bare field names,
    # misspelled/wrong-case/fake tokens in full-width parens
    bad_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 错误标注\n"
        "适用：核对申请类型（return_refund 或 refund_only）与原因类别（quality_issue）。\n"
        "规则：质量问题(quality_issue)且查看 days_since_delivery、reason_category、type。\n"
        "处理：订单为 pending_payment 或 vip 时不予受理；质量问题（quality_issues）、"
        "会员（VIP）、申请类型（request_type）、已发货天数（days_since_shipped）"
        "与错误码（对应错误码 unknown_error_code）均应报错。\n"
    )
    kb_bad = _make_minimal_kb(tmp_path / "w10_bad", bad_text)
    errors_bad, _ = lint.lint_all(kb_bad)
    w10_errors = [e for e in errors_bad if e.rule == "W10"]
    by_line: dict[int, list[str]] = {}
    for err in w10_errors:
        by_line.setdefault(err.line, []).append(err.message)
    # Line 5: return_refund, refund_only, quality_issue (wrong Chinese prefix 原因类别)
    assert any("return_refund" in m for m in by_line.get(5, []))
    assert any("refund_only" in m for m in by_line.get(5, []))
    assert any("quality_issue" in m for m in by_line.get(5, []))
    # Line 6: half-width parens quality_issue + bare field names
    assert any("quality_issue" in m for m in by_line.get(6, []))
    assert any("days_since_delivery" in m for m in by_line.get(6, []))
    assert any("reason_category" in m for m in by_line.get(6, []))
    assert any("type" in m for m in by_line.get(6, []))
    # Line 7: bare pending_payment & vip + unknown/misspelled/wrong-case/fake paren tokens
    line7_msgs = by_line.get(7, [])
    assert any("pending_payment" in m for m in line7_msgs)
    assert any("「vip」" in m for m in line7_msgs)
    assert any("quality_issues" in m for m in line7_msgs)
    assert any("VIP" in m and "大小写" in m for m in line7_msgs)
    assert any("request_type" in m for m in line7_msgs)
    assert any("days_since_shipped" in m for m in line7_msgs)
    assert any("unknown_error_code" in m for m in line7_msgs)

    # Regression check on real KB: deleting Chinese label before an enum triggers W10
    kb_real = _copy_real_kb(tmp_path / "w10_real_mut")
    policy_path = kb_real / "docs" / "policy" / "售后制度.md"
    mutated = policy_path.read_text(encoding="utf-8").replace(
        "质量问题（quality_issue）", "（quality_issue）", 1
    )
    policy_path.write_text(mutated, encoding="utf-8")
    errors_mut, _ = lint.lint_all(kb_real)
    assert any(e.rule == "W10" and "quality_issue" in e.message for e in errors_mut)


def test_lint_w11_deadline_metric_and_boundary(tmp_path: Path) -> None:
    # Positive case: valid 已签收天数/已发货天数 + boundary + multi-deadline sentence + new phrases
    good_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 期限规范\n"
        "适用：签收当天为第 0 天，审批提交后 72 小时未审批自动置为已过期（expired）。\n"
        "规则：已签收天数（days_since_delivery）不超过 7 天（含第 7 天）的可退货；"
        "已签收天数达到 8 天及以上（不含第 7 天）的不予受理；"
        "已签收天数大于 7 天（不含第 7 天）且少于 15 天（不含第 15 天）的需人工复核。\n"
        "处理：已发货天数达到 7 天及以上（含第 7 天）的可仅退款（refund_only）；"
        "已发货天数不满 7 天（不超过 6 天，含第 6 天）的暂不退款，"
        "已签收天数 30 天以下（含第 30 天）或 31 天及以上可按对应条款办理。\n"
    )
    kb_good = _make_minimal_kb(tmp_path / "w11_good", good_text)
    errors_good, _ = lint.lint_all(kb_good)
    assert errors_good == [], [e.format() for e in errors_good]

    # Negative case: missing metric/boundary, new phrases, and dual deadlines in one sentence
    bad_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 缺失口径与边界\n"
        "适用：不超过 7 天（含第 7 天）的订单；7 天及以上的订单。\n"
        "规则：已签收天数超过 15 天的不予受理；已签收天数不超过 7 天的可退货。\n"
        "处理：7 天以内可提出申请；达到 10 天的不予受理。\n"
        "例外：已签收天数不超过 7 天（含第 7 天）可自助办理，超过 15 天不予受理；"
        "已签收天数 7 天以上可核查，已签收天数 30 天以下可登记；"
        "已签收天数大于 7 天且少于 15 天（不含第 15 天）需复核。\n"
    )
    kb_bad = _make_minimal_kb(tmp_path / "w11_bad", bad_text)
    errors_bad, _ = lint.lint_all(kb_bad)
    w11_errors = [e for e in errors_bad if e.rule == "W11"]
    # Line 5: two sentences missing 已签收天数/已发货天数 (including "7 天及以上")
    assert len([e for e in w11_errors if e.line == 5 and "已签收天数" in e.message]) == 2
    # Line 6: two sentences both missing boundary
    line6_boundary = [e for e in w11_errors if e.line == 6 and "边界" in e.message]
    assert len(line6_boundary) == 2
    # Line 7: two sentences both missing metric and boundary
    assert len([e for e in w11_errors if e.line == 7 and "已签收天数" in e.message]) == 2
    assert len([e for e in w11_errors if e.line == 7 and "边界" in e.message]) == 2
    # Line 8: dual deadlines where one lacks boundary + 以上/以下/大于 without boundary
    line8_boundary = [e.message for e in w11_errors if e.line == 8 and "边界" in e.message]
    assert len(line8_boundary) == 4
    assert any("超过 15 天" in m for m in line8_boundary)
    assert any("7 天以上" in m for m in line8_boundary)
    assert any("30 天以下" in m for m in line8_boundary)
    assert any("大于 7 天" in m for m in line8_boundary)

    # Regression check on real KB: removing boundary from clause 2.1 triggers W11
    kb_real = _copy_real_kb(tmp_path / "w11_real_mut")
    policy_path = kb_real / "docs" / "policy" / "售后制度.md"
    mutated = policy_path.read_text(encoding="utf-8").replace(
        "不超过 7 天（含第 7 天）", "不超过 7 天", 1
    )
    policy_path.write_text(mutated, encoding="utf-8")
    errors_mut, _ = lint.lint_all(kb_real)
    assert any(e.rule == "W11" and "边界" in e.message for e in errors_mut)


def test_lint_l7_sample_ids_halfwidth_parens_and_w11_day_and_boundary_numbers(
    tmp_path: Path,
) -> None:
    # 1. Positive cases: [A-Z]\d{4} sample IDs (B2087/C3051/Z9999/C0001/T5001),
    # bare non-paren English words (VIP 客户 / HTTP / SKU), "日" synonym, and consistent boundaries
    good_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 示例单号与日口径\n"
        "适用：华北订单（如 B2087）、华南订单（如 C3051、Z9999）、客户号（如 C0001）、"
        "工单号（如 T5001）、审批单号（如 AP000001）及退款单号（如 R000105）；"
        "不带括号的 VIP 客户、HTTP 或 SKU 不受影响。\n"
        "规则：已签收天数 7 日内（含第 7 日）可办理；已签收天数 15 日后（不含第 15 日）"
        "或已签收天数 15 日后（达到 16 日及以上）不予受理；"
        "已发货天数达到 7 日及以上（含第 7 日）可仅退款（refund_only）。\n"
        "处理：已签收天数达到 8 天及以上（不含第 7 天）不予受理；"
        "已签收天数超过 30 天（达到 31 天及以上）转保修；"
        "已发货天数不满 7 日（不超过 6 日，含第 6 日）先跟进物流。\n"
    )
    kb_good = _make_minimal_kb(tmp_path / "l7_good", good_text)
    errors_good, _ = lint.lint_all(kb_good)
    assert errors_good == [], [e.format() for e in errors_good]

    # 2. Negative cases for W10: malformed sample IDs + half-width parens
    bad_w10_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 单号与半角括号反例\n"
        "适用：错误单号（如 B208、B20871、AB1234、a1023、AP00001、R00010）。\n"
        "规则：半角括号内未登记记号(request_type)、大小写不符(VIP)、"
        "规范取值质量问题(quality_issue)、示例单号(如 B2087)与错误码(order_not_found)。\n"
    )
    kb_bad_w10 = _make_minimal_kb(tmp_path / "l7_bad_w10", bad_w10_text)
    errors_w10, _ = lint.lint_all(kb_bad_w10)
    w10_only = [e for e in errors_w10 if e.rule == "W10"]
    line5_msgs = [e.message for e in w10_only if e.line == 5]
    for bad_id in ("B208", "B20871", "AB1234", "a1023", "AP00001", "R00010"):
        assert any(bad_id in m for m in line5_msgs), f"expected W10 error for {bad_id}"

    line6_msgs = [e.message for e in w10_only if e.line == 6]
    assert any("request_type" in m and "全角括号" in m for m in line6_msgs)
    assert any("VIP" in m and "大小写" in m and "全角括号" in m for m in line6_msgs)
    assert any("quality_issue" in m and "全角括号" in m for m in line6_msgs)
    assert any("B2087" in m and "全角括号" in m for m in line6_msgs)
    assert any("order_not_found" in m and "全角括号" in m for m in line6_msgs)

    # 3. Negative cases for W11: "日" missing metric/boundary + mismatched boundary numbers
    bad_w11_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 期限日与边界数字反例\n"
        "适用：7 日内（含第 7 日）可申请；7 日及以上不予受理。\n"
        "规则：已签收天数 7 日内可申请；已签收天数 15 日后不予受理。\n"
        "处理：已签收天数不超过 7 天（含第 30 天）可退；"
        "已签收天数达到 8 天及以上（不含第 15 天）不予受理；"
        "已签收天数超过 30 天（达到 35 天及以上）转保修。\n"
        "例外：已签收天数 7 日内（含第 30 日）可退；"
        "已签收天数 15 日后（不含第 7 日）不予受理；"
        "已签收天数不超过 7 天（不含第 7 天）可退；"
        "已发货天数不满 7 天（含第 7 天）暂不退款。\n"
    )
    kb_bad_w11 = _make_minimal_kb(tmp_path / "l7_bad_w11", bad_w11_text)
    errors_w11, _ = lint.lint_all(kb_bad_w11)
    w11_only = [e for e in errors_w11 if e.rule == "W11"]

    # Line 5: two sentences missing 已签收天数/已发货天数
    assert len([e for e in w11_only if e.line == 5 and "已签收天数" in e.message]) == 2
    # Line 6: two sentences missing boundary for "7 日内" and "15 日后"
    line6_w11 = [e.message for e in w11_only if e.line == 6 and "边界" in e.message]
    assert any("7 日内" in m for m in line6_w11)
    assert any("15 日后" in m for m in line6_w11)
    # Line 7: three mismatched boundary numbers
    line7_w11 = [e.message for e in w11_only if e.line == 7 and "不一致" in e.message]
    assert len(line7_w11) == 3
    assert any("含第 30 天" in m for m in line7_w11)
    assert any("不含第 15 天" in m for m in line7_w11)
    assert any("35 天及以上" in m for m in line7_w11)
    # Line 8: four mismatched/contradictory boundaries
    line8_w11 = [e.message for e in w11_only if e.line == 8 and "不一致" in e.message]
    assert len(line8_w11) == 4
    assert any("含第 30 日" in m for m in line8_w11)
    assert any("不含第 7 日" in m for m in line8_w11)
    assert any("不含第 7 天" in m for m in line8_w11)
    assert any("含第 7 天" in m for m in line8_w11)


def test_lint_r4_l5_w11_excludes_calendar_dates_but_checks_deadlines(tmp_path: Path) -> None:
    # Calendar dates preceded by 月 / 年 (with or without spaces) must NOT trigger W11
    good_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 生效日期与期限区分\n"
        "规则：自 2026 年 12 月 1 日后签收的订单适用本条款。\n"
        "适用：12 月 1 日起至 12 月 15 日内下单的订单，"
        "以及 2026年12月31日以后与 2026 年 1 日后生效的批次。\n"
        "处理：自 2026 年 12 月 1 日后签收的订单，已签收天数不超过 7 天（含第 7 天）可退货。\n"
    )
    kb_good = _make_minimal_kb(tmp_path / "r4_l5_good", good_text)
    errors_good, _ = lint.lint_all(kb_good)
    assert errors_good == [], [e.format() for e in errors_good]

    # Plain deadline with 日 and mixed calendar date + plain deadline must still trigger W11
    bad_text = (
        "# 测试制度\n"
        "说明文档用途。\n\n"
        "## 测试制度 第 1.1 条 普通期限仍报错\n"
        "规则：7 日内可申请退货。\n"
        "处理：自 2026 年 12 月 1 日后签收的订单，7 日内可申请退货。\n"
    )
    kb_bad = _make_minimal_kb(tmp_path / "r4_l5_bad", bad_text)
    errors_bad, _ = lint.lint_all(kb_bad)
    w11_errors = [e for e in errors_bad if e.rule == "W11"]

    line5_w11 = [e.message for e in w11_errors if e.line == 5]
    assert any("已签收天数" in m for m in line5_w11)
    assert any("7 日内" in m and "边界" in m for m in line5_w11)

    line6_w11 = [e.message for e in w11_errors if e.line == 6]
    line6_boundary = [m for m in line6_w11 if "边界" in m]
    assert len(line6_boundary) == 1
    assert "「7 日内」" in line6_boundary[0]
    assert not any("「1 日后」" in m for m in line6_w11)


def test_policy_clause_2_1_applies_without_regular_only_restriction() -> None:
    policy_text = (REAL_KB_DIR / "docs" / "policy" / "售后制度.md").read_text(encoding="utf-8")
    marker = "## 售后制度 第 2.1 条 七天无理由退货\n"
    start = policy_text.index(marker)
    clause_block = policy_text[start : policy_text.index("\n\n", start)]
    applicable_line = next(ln for ln in clause_block.splitlines() if ln.startswith("适用："))
    exception_line = next(ln for ln in clause_block.splitlines() if ln.startswith("例外："))
    assert "普通（regular）" not in applicable_line
    assert "第 6.1 条" in exception_line


def test_lint_w12_relative_references(tmp_path: Path) -> None:
    text = (
        "# 测试制度\n"
        "用途说明。\n\n"
        "## 测试制度 第 1.1 条 相对引用\n"
        "规则：如前所述，并参考上一条办理。\n"
    )
    kb_dir = _make_minimal_kb(tmp_path, text)
    errors, _ = lint.lint_all(kb_dir)
    w12_errors = [e for e in errors if e.rule == "W12"]
    assert len(w12_errors) == 2


def test_lint_numbering_completeness_and_broken_refs(tmp_path: Path) -> None:
    # Duplicate and non-increasing clause numbers
    kb_num = _make_minimal_kb(
        tmp_path / "num",
        "# 测试制度\n说明。\n\n"
        "## 测试制度 第 1.2 条 条款二\n规则：内容。\n\n"
        "## 测试制度 第 1.1 条 倒序\n规则：内容。\n\n"
        "## 测试制度 第 1.2 条 重复\n规则：内容。\n",
    )
    errors_num, _ = lint.lint_all(kb_num)
    assert len([e for e in errors_num if e.rule == "NUM"]) >= 2

    # Missing required 4.7.3 clause + broken cross-doc & same-doc references
    kb_real = _copy_real_kb(tmp_path / "missing_clause")
    policy_path = kb_real / "docs" / "policy" / "售后制度.md"
    policy_text = policy_path.read_text(encoding="utf-8")
    # Remove clause 8.3 and inject broken references
    cut_idx = policy_text.index("\n\n## 售后制度 第 8.3 条")
    policy_text = policy_text[:cut_idx] + "\n"
    policy_text = policy_text.replace(
        "见第 8.2 条",
        "见第 9.9 条与《故障排查手册》第 9.8 条以及《未知手册》第 1.1 条",
    )
    policy_path.write_text(policy_text, encoding="utf-8")

    errors, _ = lint.lint_all(kb_real)
    assert any(e.rule == "COMPLETENESS" and "8.3" in e.message for e in errors)
    ref_errors = [e for e in errors if e.rule == "REF"]
    assert any("第 9.9 条" in e.message for e in ref_errors)
    assert any("《故障排查手册》第 9.8 条" in e.message for e in ref_errors)
    assert any("《未知手册》" in e.message for e in ref_errors)
    assert lint.main(["--kb-dir", str(kb_real)]) == 1


# ---------------------------------------------------------------------------
# sync.py tests
# ---------------------------------------------------------------------------


def _build_mock_dify_sync_handler(
    *,
    existing_datasets: list[dict[str, Any]] | None = None,
    existing_docs: dict[str, list[dict[str, Any]]] | None = None,
    indexing_sequence: dict[str, list[dict[str, Any]]] | None = None,
    doc_detail_sequence: dict[str, list[dict[str, Any]]] | None = None,
    update_fail_responses: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
    create_fail_responses: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
    strict_dify_status: bool = False,
    segment_override: dict[str, int] | None = None,
    paginate_datasets: bool = False,
    paginate_docs: bool = False,
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """Build an in-memory Dify Service API mock transport and call log."""
    datasets_store = [dict(ds) for ds in (existing_datasets or [])]
    docs_store: dict[str, list[dict[str, Any]]] = {
        k: [dict(doc) for doc in v] for k, v in (existing_docs or {}).items()
    }
    seq_store: dict[str, list[dict[str, Any]]] = {
        k: list(v) for k, v in (indexing_sequence or {}).items()
    }
    detail_seq_store: dict[str, list[dict[str, Any]]] = {
        k: list(v) for k, v in (doc_detail_sequence or {}).items()
    }
    update_fail_store: dict[str, list[tuple[int, dict[str, Any]]]] = {
        k: list(v) for k, v in (update_fail_responses or {}).items()
    }
    create_fail_store: dict[str, list[tuple[int, dict[str, Any]]]] = {
        k: list(v) for k, v in (create_fail_responses or {}).items()
    }
    log: dict[str, Any] = {
        "created_datasets": [],
        "patched_datasets": [],
        "created_docs": [],
        "updated_docs": [],
        "deleted_docs": [],
        "unarchived_docs": [],
        "doc_detail_polls": [],
        "status_polls": [],
        "auth_headers": [],
        "docs_store": docs_store,
    }
    batch_meta: dict[str, tuple[str, str, int]] = {}
    counters = {"ds": 0, "doc": 0, "batch": 0}

    def _find_doc(ds_id: str, doc_id: str) -> dict[str, Any] | None:
        for item in docs_store.get(ds_id, []):
            if str(item.get("id")) == doc_id:
                return item
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        log["auth_headers"].append(request.headers.get("Authorization"))
        path = request.url.path
        method = request.method

        # GET /v1/datasets
        if method == "GET" and path == "/v1/datasets":
            page = int(request.url.params.get("page", "1"))
            if paginate_datasets and len(datasets_store) > 1:
                idx = page - 1
                chunk = datasets_store[idx : idx + 1]
                has_more = idx + 1 < len(datasets_store)
                return httpx.Response(
                    200,
                    json={
                        "data": chunk,
                        "has_more": has_more,
                        "page": page,
                        "limit": 1,
                        "total": len(datasets_store),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": datasets_store,
                    "has_more": False,
                    "page": page,
                    "limit": 100,
                    "total": len(datasets_store),
                },
            )

        # POST /v1/datasets
        if method == "POST" and path == "/v1/datasets":
            body = json.loads(request.content.decode("utf-8"))
            counters["ds"] += 1
            ds_id = f"ds-uuid-{counters['ds']}"
            log["created_datasets"].append(body)
            rec = {
                "id": ds_id,
                "name": body["name"],
                "indexing_technique": body.get("indexing_technique"),
                "embedding_model_provider": body.get("embedding_model_provider"),
                "embedding_model": body.get("embedding_model"),
                "retrieval_model_dict": body.get("retrieval_model", {}),
            }
            datasets_store.append(rec)
            docs_store.setdefault(ds_id, [])
            return httpx.Response(200, json=rec)

        # PATCH /v1/datasets/{dataset_id}/documents/status/{action}
        if method == "PATCH" and "/documents/status/" in path:
            parts = path.split("/")
            ds_id, action = parts[3], parts[6]
            body = json.loads(request.content.decode("utf-8"))
            doc_ids = [str(i) for i in body.get("document_ids", [])]
            if action == "un_archive":
                for doc_id in doc_ids:
                    log["unarchived_docs"].append({"dataset_id": ds_id, "document_id": doc_id})
                    found = _find_doc(ds_id, doc_id)
                    if found is not None:
                        found["archived"] = False
                        if found.get("display_status") == "archived":
                            found["display_status"] = (
                                "available" if found.get("enabled", True) else "disabled"
                            )
            return httpx.Response(200, json={"result": "success"})

        # PATCH /v1/datasets/{dataset_id}
        if method == "PATCH" and path.startswith("/v1/datasets/"):
            ds_id = path.split("/")[3]
            body = json.loads(request.content.decode("utf-8"))
            log["patched_datasets"].append({"dataset_id": ds_id, "body": body})
            return httpx.Response(200, json={"id": ds_id, **body})

        # GET /v1/datasets/{dataset_id}/documents
        if method == "GET" and path.endswith("/documents"):
            ds_id = path.split("/")[3]
            items = docs_store.get(ds_id, [])
            page = int(request.url.params.get("page", "1"))
            if paginate_docs and len(items) > 1:
                idx = page - 1
                chunk = items[idx : idx + 1]
                has_more = idx + 1 < len(items)
                return httpx.Response(
                    200,
                    json={
                        "data": chunk,
                        "has_more": has_more,
                        "page": page,
                        "limit": 1,
                        "total": len(items),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": items,
                    "has_more": False,
                    "page": page,
                    "limit": 100,
                    "total": len(items),
                },
            )

        # POST /v1/datasets/{dataset_id}/document/create-by-file
        if method == "POST" and path.endswith("/document/create-by-file"):
            ds_id = path.split("/")[3]
            raw_body = request.content.decode("utf-8", errors="replace")
            filename = _extract_multipart_filename(raw_body)
            if filename in create_fail_store and create_fail_store[filename]:
                status_code, err_body = create_fail_store[filename].pop(0)
                return httpx.Response(status_code, json=err_body)
            counters["doc"] += 1
            counters["batch"] += 1
            doc_id = f"doc-{counters['doc']}"
            batch = f"batch-{counters['batch']}"
            # Count '## ' headings inside uploaded multipart body
            h2_count = sum(1 for ln in raw_body.splitlines() if ln.startswith("## "))
            log["created_docs"].append(
                {
                    "dataset_id": ds_id,
                    "document_id": doc_id,
                    "filename": filename,
                    "raw": raw_body,
                }
            )
            docs_store.setdefault(ds_id, []).append(
                {
                    "id": doc_id,
                    "name": filename,
                    "display_status": "available",
                    "indexing_status": "completed",
                    "enabled": True,
                    "archived": False,
                }
            )
            batch_meta[batch] = (doc_id, filename, h2_count + 1)
            return httpx.Response(
                200,
                json={"document": {"id": doc_id, "name": filename}, "batch": batch},
            )

        # POST /v1/datasets/{dataset_id}/documents/{doc_id}/update-by-file
        if method == "POST" and path.endswith("/update-by-file"):
            parts = path.split("/")
            ds_id, doc_id = parts[3], parts[5]
            raw_body = request.content.decode("utf-8", errors="replace")
            filename = _extract_multipart_filename(raw_body)
            if doc_id in update_fail_store and update_fail_store[doc_id]:
                status_code, err_body = update_fail_store[doc_id].pop(0)
                return httpx.Response(status_code, json=err_body)
            found = _find_doc(ds_id, doc_id)
            if strict_dify_status and found is not None:
                if (
                    found.get("display_status") != "available"
                    or found.get("enabled") is False
                    or found.get("archived") is True
                ):
                    return httpx.Response(
                        400,
                        json={
                            "code": "invalid_param",
                            "message": "Document is not available",
                            "status": 400,
                        },
                    )
            counters["batch"] += 1
            batch = f"batch-{counters['batch']}"
            h2_count = sum(1 for ln in raw_body.splitlines() if ln.startswith("## "))
            log["updated_docs"].append(
                {
                    "dataset_id": ds_id,
                    "document_id": doc_id,
                    "filename": filename,
                    "raw": raw_body,
                }
            )
            if found is not None:
                found.update(
                    {
                        "name": filename,
                        "display_status": "available",
                        "indexing_status": "completed",
                        "enabled": True,
                        "archived": False,
                    }
                )
            batch_meta[batch] = (doc_id, filename, h2_count + 1)
            return httpx.Response(
                200,
                json={"document": {"id": doc_id, "name": filename}, "batch": batch},
            )

        # DELETE /v1/datasets/{dataset_id}/documents/{doc_id}
        if method == "DELETE" and "/documents/" in path:
            parts = path.split("/")
            ds_id, doc_id = parts[3], parts[5]
            found = _find_doc(ds_id, doc_id)
            if strict_dify_status and found is not None and found.get("archived") is True:
                return httpx.Response(
                    403,
                    json={
                        "code": "archived_document_immutable",
                        "message": "The archived document is not editable.",
                        "status": 403,
                    },
                )
            log["deleted_docs"].append({"dataset_id": ds_id, "document_id": doc_id})
            docs_store[ds_id] = [d for d in docs_store.get(ds_id, []) if str(d.get("id")) != doc_id]
            return httpx.Response(204)

        # GET /v1/datasets/{dataset_id}/documents/{batch}/indexing-status
        if method == "GET" and path.endswith("/indexing-status"):
            parts = path.split("/")
            batch = parts[5]
            doc_id, filename, default_segments = batch_meta[batch]
            log["status_polls"].append({"batch": batch, "filename": filename})

            if filename in seq_store and seq_store[filename]:
                entry = dict(seq_store[filename].pop(0))
                entry.setdefault("id", doc_id)
                if entry.get("indexing_status") == "completed":
                    entry.setdefault(
                        "total_segments",
                        (segment_override or {}).get(filename, default_segments),
                    )
                return httpx.Response(200, json={"data": [entry]})

            total_segments = (segment_override or {}).get(filename, default_segments)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": doc_id,
                            "indexing_status": "completed",
                            "completed_segments": total_segments,
                            "total_segments": total_segments,
                            "error": None,
                        }
                    ]
                },
            )

        # GET /v1/datasets/{dataset_id}/documents/{doc_id}
        if method == "GET" and "/documents/" in path:
            parts = path.split("/")
            ds_id, doc_id = parts[3], parts[5]
            log["doc_detail_polls"].append({"dataset_id": ds_id, "document_id": doc_id})
            found = _find_doc(ds_id, doc_id)
            if doc_id in detail_seq_store and detail_seq_store[doc_id]:
                nxt = dict(detail_seq_store[doc_id].pop(0))
                if found is not None:
                    found.update(nxt)
                    return httpx.Response(200, json=dict(found))
                nxt.setdefault("id", doc_id)
                return httpx.Response(200, json=nxt)
            if found is None:
                return httpx.Response(404, json={"message": "Document not found."})
            return httpx.Response(200, json=dict(found))

        return httpx.Response(404, json={"message": f"Unhandled {method} {path}"})

    return httpx.MockTransport(handler), log


def _extract_multipart_filename(raw_body: str) -> str:
    for line in raw_body.splitlines():
        if "filename=" in line:
            part = line.split("filename=", 1)[1].strip()
            if part.startswith('"'):
                return part.split('"')[1]
    return "unknown.md"


def test_sync_creates_datasets_documents_and_lock_file(tmp_path: Path) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    transport, log = _build_mock_dify_sync_handler(
        indexing_sequence={
            "售后制度.md": [
                {"indexing_status": "waiting"},
                {"indexing_status": "indexing"},
                {"indexing_status": "completed"},
            ]
        }
    )
    slept: list[float] = []
    env = {
        "KB_DIFY_BASE_URL": "http://dify.test",
        "KB_DIFY_API_KEY": "dataset-secret-key",
        "KB_EMBEDDING_PROVIDER": "langgenius/openai/openai",
        "KB_EMBEDDING_MODEL": "text-embedding-3-small",
    }

    with httpx.Client(transport=transport) as client:
        result = sync.sync_knowledge_base(
            kb_dir,
            env=env,
            client=client,
            poll_interval=0.1,
            sleep_fn=slept.append,
        )

    assert result == {"policy": "ds-uuid-1", "handbook": "ds-uuid-2"}
    lock_file = json.loads((kb_dir / "kb.lock.json").read_text(encoding="utf-8"))
    assert lock_file == result

    # Created 2 datasets with embedding + default semantic top_k=8 retrieval_model
    assert len(log["created_datasets"]) == 2
    assert log["created_datasets"][0]["name"] == "售后制度"
    assert log["created_datasets"][0]["embedding_model_provider"] == "langgenius/openai/openai"
    assert log["created_datasets"][0]["embedding_model"] == "text-embedding-3-small"
    assert log["created_datasets"][0]["retrieval_model"] == {
        "search_method": "semantic_search",
        "reranking_enable": False,
        "top_k": 8,
        "score_threshold_enabled": False,
    }
    # Created 3 documents total, polled waiting -> indexing -> completed
    assert len(log["created_docs"]) == 3
    assert len(log["updated_docs"]) == 0
    assert len(log["deleted_docs"]) == 0
    assert len(slept) == 2
    assert all(h == "Bearer dataset-secret-key" for h in log["auth_headers"])


def test_sync_updates_dataset_documents_deletes_stale_and_enables_rerank(tmp_path: Path) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    existing_datasets = [
        {
            "id": "existing-policy-id",
            "name": "售后制度",
            "indexing_technique": "high_quality",
            "embedding_model_provider": "old-provider",
            "embedding_model": "old-model",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
        {
            "id": "existing-handbook-id",
            "name": "客服手册",
            "indexing_technique": "high_quality",
            "embedding_model_provider": "langgenius/openai/openai",
            "embedding_model": "text-embedding-3-small",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": True,
                "reranking_mode": "reranking_model",
                "reranking_model": {
                    "reranking_provider_name": "langgenius/cohere/cohere",
                    "reranking_model_name": "rerank-multilingual-v3.0",
                },
                "top_k": 6,
                "score_threshold_enabled": False,
            },
        },
    ]
    existing_docs = {
        "existing-policy-id": [
            {"id": "doc-policy-1", "name": "售后制度.md"},
            {"id": "doc-stale-1", "name": "旧版制度.md"},
        ],
        "existing-handbook-id": [
            {"id": "doc-hb-1", "name": "客服操作手册.md"},
        ],
    }
    transport, log = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs=existing_docs,
        paginate_datasets=True,
        paginate_docs=True,
    )
    env = {
        "KB_DIFY_BASE_URL": "http://dify.test",
        "KB_DIFY_API_KEY": "dataset-key",
        "KB_EMBEDDING_PROVIDER": "langgenius/openai/openai",
        "KB_EMBEDDING_MODEL": "text-embedding-3-small",
        "KB_RERANK_PROVIDER": "langgenius/cohere/cohere",
        "KB_RERANK_MODEL": "rerank-multilingual-v3.0",
    }

    with httpx.Client(transport=transport) as client:
        result = sync.sync_knowledge_base(kb_dir, env=env, client=client, sleep_fn=lambda _: None)

    assert result == {"policy": "existing-policy-id", "handbook": "existing-handbook-id"}
    assert len(log["created_datasets"]) == 0
    # Only policy needed PATCH; handbook already matched embedding + rerank config
    assert len(log["patched_datasets"]) == 1
    patch_entry = log["patched_datasets"][0]
    assert patch_entry["dataset_id"] == "existing-policy-id"
    assert patch_entry["body"]["retrieval_model"] == {
        "search_method": "semantic_search",
        "reranking_enable": True,
        "reranking_mode": "reranking_model",
        "reranking_model": {
            "reranking_provider_name": "langgenius/cohere/cohere",
            "reranking_model_name": "rerank-multilingual-v3.0",
        },
        "top_k": 6,
        "score_threshold_enabled": False,
    }
    # 售后制度.md & 客服操作手册.md updated; 故障排查手册.md created; 旧版制度.md deleted
    assert {d["document_id"] for d in log["updated_docs"]} == {"doc-policy-1", "doc-hb-1"}
    assert len(log["created_docs"]) == 1
    assert log["deleted_docs"] == [
        {"dataset_id": "existing-policy-id", "document_id": "doc-stale-1"}
    ]


def test_sync_aborts_on_lint_failure_without_http(tmp_path: Path) -> None:
    kb_dir = _make_minimal_kb(
        tmp_path,
        "# 测试制度\n说明。\n\n## 测试制度 第 1.1 条 错误\n规则：包含 <tag> 尖括号。\n",
    )
    called = False

    def fail_transport(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(fail_transport)) as client:
        with pytest.raises(sync.SyncError, match="文档规范检查未通过"):
            sync.sync_knowledge_base(
                kb_dir,
                env={"KB_DIFY_API_KEY": "dataset-key"},
                client=client,
            )
    assert not called
    assert not (kb_dir / "kb.lock.json").exists()


def test_sync_fails_on_indexing_error_timeout_and_segment_mismatch(tmp_path: Path) -> None:
    env = {"KB_DIFY_BASE_URL": "http://dify.test", "KB_DIFY_API_KEY": "dataset-key"}

    # 1. Indexing error
    kb1 = _copy_real_kb(tmp_path / "err")
    t1, _ = _build_mock_dify_sync_handler(
        indexing_sequence={
            "售后制度.md": [{"indexing_status": "error", "error": "embedding quota exceeded"}]
        }
    )
    with httpx.Client(transport=t1) as client:
        with pytest.raises(sync.SyncError, match="embedding quota exceeded"):
            sync.sync_knowledge_base(kb1, env=env, client=client, sleep_fn=lambda _: None)
    assert not (kb1 / "kb.lock.json").exists()

    # 2. Indexing timeout
    kb2 = _copy_real_kb(tmp_path / "timeout")
    t2, _ = _build_mock_dify_sync_handler(
        indexing_sequence={
            "售后制度.md": [{"indexing_status": "indexing"}, {"indexing_status": "indexing"}]
        }
    )
    clock = iter([0.0, 100.0, 400.0]).__next__
    with httpx.Client(transport=t2) as client:
        with pytest.raises(sync.SyncError, match="索引超时"):
            sync.sync_knowledge_base(
                kb2,
                env=env,
                client=client,
                poll_timeout=300.0,
                sleep_fn=lambda _: None,
                monotonic_fn=clock,
            )

    # 3. Segment count mismatch
    kb3 = _copy_real_kb(tmp_path / "mismatch")
    t3, _ = _build_mock_dify_sync_handler(segment_override={"售后制度.md": 30})
    with httpx.Client(transport=t3) as client:
        with pytest.raises(sync.SyncError, match="预期 36 段，实际 30 段"):
            sync.sync_knowledge_base(kb3, env=env, client=client, sleep_fn=lambda _: None)
    assert not (kb3 / "kb.lock.json").exists()


def test_sync_recreates_error_disabled_and_archived_remote_docs(tmp_path: Path) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    existing_datasets = [
        {
            "id": "ds-policy",
            "name": "售后制度",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
        {
            "id": "ds-handbook",
            "name": "客服手册",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
    ]
    existing_docs = {
        "ds-policy": [
            {
                "id": "doc-policy-err",
                "name": "售后制度.md",
                "display_status": "error",
                "indexing_status": "error",
                "enabled": True,
                "archived": False,
            },
        ],
        "ds-handbook": [
            {
                "id": "doc-hb-disabled",
                "name": "客服操作手册.md",
                "display_status": "disabled",
                "indexing_status": "completed",
                "enabled": False,
                "archived": False,
            },
            {
                "id": "doc-hb-archived",
                "name": "故障排查手册.md",
                "display_status": "archived",
                "indexing_status": "completed",
                "enabled": True,
                "archived": True,
            },
        ],
    }
    transport, log = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs=existing_docs,
        strict_dify_status=True,
    )
    env = {"KB_DIFY_BASE_URL": "http://dify.test", "KB_DIFY_API_KEY": "dataset-key"}

    with httpx.Client(transport=transport) as client:
        result = sync.sync_knowledge_base(kb_dir, env=env, client=client, sleep_fn=lambda _: None)

    assert result == {"policy": "ds-policy", "handbook": "ds-handbook"}
    assert len(log["updated_docs"]) == 0
    assert {d["document_id"] for d in log["deleted_docs"]} == {
        "doc-policy-err",
        "doc-hb-disabled",
        "doc-hb-archived",
    }
    assert log["unarchived_docs"] == [
        {"dataset_id": "ds-handbook", "document_id": "doc-hb-archived"}
    ]
    assert {d["filename"] for d in log["created_docs"]} == {
        "售后制度.md",
        "客服操作手册.md",
        "故障排查手册.md",
    }
    assert [d["name"] for d in log["docs_store"]["ds-policy"]] == ["售后制度.md"]
    assert sorted(d["name"] for d in log["docs_store"]["ds-handbook"]) == [
        "客服操作手册.md",
        "故障排查手册.md",
    ]


def test_sync_waits_for_indexing_remote_doc_then_updates_or_recreates(tmp_path: Path) -> None:
    env = {"KB_DIFY_BASE_URL": "http://dify.test", "KB_DIFY_API_KEY": "dataset-key"}
    existing_datasets = [
        {
            "id": "ds-policy",
            "name": "售后制度",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
        {
            "id": "ds-handbook",
            "name": "客服手册",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
    ]

    # Case 1: policy doc finishes indexing -> available (update-by-file);
    # handbook doc finishes indexing -> error (delete + create-by-file)
    kb1 = _copy_real_kb(tmp_path / "indexing_resolve")
    existing_docs = {
        "ds-policy": [
            {
                "id": "doc-policy-idx",
                "name": "售后制度.md",
                "display_status": "indexing",
                "indexing_status": "indexing",
                "enabled": True,
                "archived": False,
            }
        ],
        "ds-handbook": [
            {
                "id": "doc-hb-idx",
                "name": "客服操作手册.md",
                "display_status": "queuing",
                "indexing_status": "waiting",
                "enabled": True,
                "archived": False,
            },
            {
                "id": "doc-ts-ok",
                "name": "故障排查手册.md",
                "display_status": "available",
                "indexing_status": "completed",
                "enabled": True,
                "archived": False,
            },
        ],
    }
    doc_detail_sequence = {
        "doc-policy-idx": [
            {"display_status": "indexing", "indexing_status": "splitting"},
            {
                "display_status": "available",
                "indexing_status": "completed",
                "enabled": True,
                "archived": False,
            },
        ],
        "doc-hb-idx": [
            {"display_status": "indexing", "indexing_status": "parsing"},
            {
                "display_status": "error",
                "indexing_status": "error",
                "enabled": True,
                "archived": False,
            },
        ],
    }
    t1, log1 = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs=existing_docs,
        doc_detail_sequence=doc_detail_sequence,
        strict_dify_status=True,
    )
    slept: list[float] = []
    with httpx.Client(transport=t1) as client:
        sync.sync_knowledge_base(
            kb1,
            env=env,
            client=client,
            poll_interval=0.1,
            sleep_fn=slept.append,
        )

    assert len(slept) == 2
    assert {d["document_id"] for d in log1["updated_docs"]} == {"doc-policy-idx", "doc-ts-ok"}
    assert log1["deleted_docs"] == [{"dataset_id": "ds-handbook", "document_id": "doc-hb-idx"}]
    assert [d["filename"] for d in log1["created_docs"]] == ["客服操作手册.md"]
    assert [d["name"] for d in log1["docs_store"]["ds-policy"]] == ["售后制度.md"]
    assert sorted(d["name"] for d in log1["docs_store"]["ds-handbook"]) == [
        "客服操作手册.md",
        "故障排查手册.md",
    ]

    # Case 2: existing indexing doc times out waiting -> raises SyncError with doc name & status
    kb2 = _copy_real_kb(tmp_path / "indexing_timeout")
    t2, _ = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs={
            "ds-policy": [
                {
                    "id": "doc-stuck",
                    "name": "售后制度.md",
                    "display_status": "indexing",
                    "indexing_status": "cleaning",
                    "enabled": True,
                    "archived": False,
                }
            ],
            "ds-handbook": [],
        },
        doc_detail_sequence={
            "doc-stuck": [
                {"display_status": "indexing", "indexing_status": "cleaning"},
                {"display_status": "indexing", "indexing_status": "cleaning"},
            ]
        },
    )
    clock = iter([0.0, 100.0, 350.0]).__next__
    with httpx.Client(transport=t2) as client:
        with pytest.raises(sync.SyncError, match=r"售后制度\.md.*display_status=indexing"):
            sync.sync_knowledge_base(
                kb2,
                env=env,
                client=client,
                poll_timeout=300.0,
                sleep_fn=lambda _: None,
                monotonic_fn=clock,
            )


def test_sync_deduplicates_same_name_remote_docs(tmp_path: Path) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    existing_datasets = [
        {
            "id": "ds-policy",
            "name": "售后制度",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
        {
            "id": "ds-handbook",
            "name": "客服手册",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
    ]
    existing_docs = {
        "ds-policy": [
            {
                "id": "doc-policy-err",
                "name": "售后制度.md",
                "display_status": "error",
                "indexing_status": "error",
                "enabled": True,
                "archived": False,
            },
            {
                "id": "doc-policy-ok",
                "name": "售后制度.md",
                "display_status": "available",
                "indexing_status": "completed",
                "enabled": True,
                "archived": False,
            },
        ],
        "ds-handbook": [
            {
                "id": "doc-hb-disabled",
                "name": "客服操作手册.md",
                "display_status": "disabled",
                "indexing_status": "completed",
                "enabled": False,
                "archived": False,
            },
            {
                "id": "doc-hb-err",
                "name": "客服操作手册.md",
                "display_status": "error",
                "indexing_status": "error",
                "enabled": True,
                "archived": False,
            },
        ],
    }
    transport, log = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs=existing_docs,
        strict_dify_status=True,
    )
    env = {"KB_DIFY_BASE_URL": "http://dify.test", "KB_DIFY_API_KEY": "dataset-key"}

    with httpx.Client(transport=transport) as client:
        sync.sync_knowledge_base(kb_dir, env=env, client=client, sleep_fn=lambda _: None)

    # Policy kept doc-policy-ok (available) and deleted doc-policy-err
    assert [d["document_id"] for d in log["updated_docs"]] == ["doc-policy-ok"]
    assert {d["document_id"] for d in log["deleted_docs"]} == {
        "doc-policy-err",
        "doc-hb-disabled",
        "doc-hb-err",
    }
    assert {d["filename"] for d in log["created_docs"]} == {"客服操作手册.md", "故障排查手册.md"}
    # Exactly one copy of each document remains remotely
    assert [d["id"] for d in log["docs_store"]["ds-policy"]] == ["doc-policy-ok"]
    assert sorted(d["name"] for d in log["docs_store"]["ds-handbook"]) == [
        "客服操作手册.md",
        "故障排查手册.md",
    ]


def test_sync_update_by_file_400_fallback_recreate_and_failure_context(tmp_path: Path) -> None:
    env = {"KB_DIFY_BASE_URL": "http://dify.test", "KB_DIFY_API_KEY": "dataset-key"}
    existing_datasets = [
        {
            "id": "ds-policy",
            "name": "售后制度",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
        {
            "id": "ds-handbook",
            "name": "客服手册",
            "indexing_technique": "high_quality",
            "retrieval_model_dict": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 8,
                "score_threshold_enabled": False,
            },
        },
    ]

    # Case 1: update-by-file returns 400 "Document is not available" -> delete + create-by-file once
    kb1 = _copy_real_kb(tmp_path / "fallback_ok")
    t1, log1 = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs={
            "ds-policy": [
                {
                    "id": "doc-policy-race",
                    "name": "售后制度.md",
                    "display_status": "available",
                    "indexing_status": "completed",
                    "enabled": True,
                    "archived": False,
                }
            ],
            "ds-handbook": [],
        },
        update_fail_responses={
            "doc-policy-race": [
                (
                    400,
                    {
                        "code": "invalid_param",
                        "message": "Document is not available",
                        "status": 400,
                    },
                )
            ]
        },
    )
    with httpx.Client(transport=t1) as client:
        sync.sync_knowledge_base(kb1, env=env, client=client, sleep_fn=lambda _: None)

    assert log1["deleted_docs"] == [{"dataset_id": "ds-policy", "document_id": "doc-policy-race"}]
    assert any(d["filename"] == "售后制度.md" for d in log1["created_docs"])
    assert [d["name"] for d in log1["docs_store"]["ds-policy"]] == ["售后制度.md"]
    assert log1["docs_store"]["ds-policy"][0]["id"] != "doc-policy-race"

    # Case 2: fallback recreate also fails -> raises SyncError with filename & remote status
    kb2 = _copy_real_kb(tmp_path / "fallback_fail")
    t2, _ = _build_mock_dify_sync_handler(
        existing_datasets=existing_datasets,
        existing_docs={
            "ds-policy": [
                {
                    "id": "doc-policy-race-2",
                    "name": "售后制度.md",
                    "display_status": "available",
                    "indexing_status": "completed",
                    "enabled": True,
                    "archived": False,
                }
            ],
            "ds-handbook": [],
        },
        update_fail_responses={
            "doc-policy-race-2": [
                (
                    400,
                    {
                        "code": "invalid_param",
                        "message": "Document is not available",
                        "status": 400,
                    },
                )
            ]
        },
        create_fail_responses={"售后制度.md": [(500, {"message": "internal error"})]},
    )
    with httpx.Client(transport=t2) as client:
        with pytest.raises(
            sync.SyncError,
            match=r"售后制度\.md.*display_status=available.*indexing_status=completed",
        ):
            sync.sync_knowledge_base(kb2, env=env, client=client, sleep_fn=lambda _: None)
    assert not (kb2 / "kb.lock.json").exists()


def test_dotenv_loading_and_config_validation(tmp_path: Path) -> None:
    kb_dir = tmp_path / "env_kb"
    kb_dir.mkdir()
    (kb_dir / ".env").write_text(
        "# comment\n"
        'KB_DIFY_BASE_URL="http://from-dotenv:8088/"\n'
        "KB_DIFY_API_KEY=from-dotenv-key\n"
        "KB_RERANK_PROVIDER=prov\n"
        "KB_RERANK_MODEL=rerank-v1\n",
        encoding="utf-8",
    )
    # Custom env overlays .env
    cfg = sync.load_env_config(kb_dir, env={"KB_DIFY_API_KEY": "override-key"})
    assert cfg.base_url == "http://from-dotenv:8088"
    assert cfg.api_key == "override-key"
    assert cfg.rerank_provider == "prov"
    assert cfg.rerank_model == "rerank-v1"

    # Missing rerank provider when rerank model is set
    with pytest.raises(sync.SyncError, match="KB_RERANK_PROVIDER"):
        sync.load_env_config(
            tmp_path,
            env={"KB_DIFY_API_KEY": "k", "KB_RERANK_MODEL": "m"},
        )

    # Mismatched embedding provider/model
    with pytest.raises(sync.SyncError, match="KB_EMBEDDING_PROVIDER"):
        sync.load_env_config(
            tmp_path,
            env={"KB_DIFY_API_KEY": "k", "KB_EMBEDDING_MODEL": "m"},
        )


# ---------------------------------------------------------------------------
# probe.py tests
# ---------------------------------------------------------------------------


def test_probe_all_hit_and_partial_miss(tmp_path: Path) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    (kb_dir / "kb.lock.json").write_text(
        json.dumps({"policy": "ds-policy", "handbook": "ds-handbook"}),
        encoding="utf-8",
    )
    # Trim probes.yaml to 2 deterministic probes for testing
    (kb_dir / "probes.yaml").write_text(
        yaml.safe_dump(
            {
                "probes": [
                    {
                        "id": "P01",
                        "dataset": "policy",
                        "query": "数码家电 质量问题 退货退款 已签收 18 天 普通客户",
                        "expect": [
                            "售后制度 第 3.2 条",
                            "售后制度 第 2.2 条",
                            "售后制度 第 5.5 条",
                        ],
                    },
                    {
                        "id": "P07",
                        "dataset": "handbook",
                        "query": "蓝牙耳机一边没有声音怎么排查",
                        "expect": ["故障排查手册 第 1.1 条"],
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    seen_requests: list[dict[str, Any]] = []

    def all_hit_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_requests.append(
            {
                "path": request.url.path,
                "auth": request.headers.get("Authorization"),
                "body": body,
            }
        )
        if request.url.path == "/v1/datasets/ds-policy/retrieve":
            return httpx.Response(
                200,
                json={
                    "query": {"content": body["query"]},
                    "records": [
                        {"segment": {"content": "售后制度 第 3.2 条 质量问题退货期限\n规则：..."}},
                        {"segment": {"content": "售后制度 第 2.2 条 超过无理由期\n规则：..."}},
                        {"segment": {"content": "售后制度 第 5.5 条 运费承担\n规则：..."}},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "query": {"content": body["query"]},
                "records": [
                    {
                        "segment": {
                            "content": (
                                "故障排查手册 第 1.1 条 蓝牙耳机单侧无声或连接异常\n现象：..."
                            )
                        }
                    }
                ],
            },
        )

    env = {
        "KB_DIFY_BASE_URL": "http://dify.test",
        "KB_DIFY_API_KEY": "dataset-probe-key",
        "KB_RERANK_PROVIDER": "langgenius/cohere/cohere",
        "KB_RERANK_MODEL": "rerank-v3",
    }
    with httpx.Client(transport=httpx.MockTransport(all_hit_handler)) as client:
        results = probe.run_probes(kb_dir, env=env, client=client)

    assert len(results) == 2
    assert all(r.passed for r in results)
    assert seen_requests[0]["auth"] == "Bearer dataset-probe-key"
    assert seen_requests[0]["body"]["retrieval_model"]["reranking_enable"] is True
    assert seen_requests[0]["body"]["retrieval_model"]["top_k"] == 6
    report_ok = probe.format_report(results)
    assert "通过 2/2 条" in report_ok

    # Now simulate partial miss on P01 (missing 5.5)
    def partial_miss_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/datasets/ds-policy/retrieve":
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"segment": {"content": "售后制度 第 3.2 条 质量问题退货期限\n规则：..."}},
                        {"segment": {"content": "售后制度 第 2.2 条 超过无理由期\n规则：..."}},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "segment": {
                            "content": (
                                "故障排查手册 第 1.1 条 蓝牙耳机单侧无声或连接异常\n现象：..."
                            )
                        }
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(partial_miss_handler)) as client:
        miss_results = probe.run_probes(kb_dir, env=env, client=client)

    assert miss_results[0].passed is False
    assert miss_results[0].missing == ["售后制度 第 5.5 条"]
    assert miss_results[1].passed is True
    report_fail = probe.format_report(miss_results)
    assert "[FAIL] P01" in report_fail
    assert "售后制度 第 5.5 条" in report_fail
    assert "通过 1/2 条" in report_fail


def test_probe_missing_lock_and_cli_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    with pytest.raises(probe.ProbeError, match="kb.lock.json"):
        probe.run_probes(kb_dir, env={"KB_DIFY_API_KEY": "k"})

    # Test CLI exit code 1 when probe fails, 0 when passes
    monkeypatch.setattr(
        probe,
        "run_probes",
        lambda _dir: [
            probe.ProbeResult(
                "P01",
                "policy",
                "q",
                ["售后制度 第 3.2 条"],
                ["售后制度 第 3.2 条"],
                [],
            )
        ],
    )
    assert probe.main(["--kb-dir", str(kb_dir)]) == 0

    monkeypatch.setattr(
        probe,
        "run_probes",
        lambda _dir: [
            probe.ProbeResult(
                "P01",
                "policy",
                "q",
                ["售后制度 第 3.2 条"],
                [],
                ["售后制度 第 3.2 条"],
            )
        ],
    )
    assert probe.main(["--kb-dir", str(kb_dir)]) == 1


def test_lint_catches_invalid_probe_references_and_sync_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _copy_real_kb(tmp_path)
    (kb_dir / "probes.yaml").write_text(
        yaml.safe_dump(
            {
                "probes": [
                    {
                        "id": "BAD1",
                        "dataset": "policy",
                        "query": "测试",
                        "expect": ["售后制度 第 9.9 条"],
                    },
                    {
                        "id": "BAD2",
                        "dataset": "policy",
                        "query": "跨库错配",
                        "expect": ["客服操作手册 第 1.1 条"],
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    errors, _ = lint.lint_all(kb_dir)
    probe_errors = [e for e in errors if e.rule == "PROBE"]
    assert len(probe_errors) == 2

    monkeypatch.setattr(
        sync,
        "sync_knowledge_base",
        lambda _dir, **_kw: {"policy": "id-1", "handbook": "id-2"},
    )
    assert sync.main(["--kb-dir", str(kb_dir)]) == 0

    def _boom(_dir: Path, **_kw: Any) -> dict[str, str]:
        raise sync.SyncError("boom")

    monkeypatch.setattr(sync, "sync_knowledge_base", _boom)
    assert sync.main(["--kb-dir", str(kb_dir)]) == 1
