"""Offline linter for MCPGate knowledge-base Markdown documents (W1-W12 + W13 废止条款识别).

检查范围说明：
- W1-W12：逐条机械检查（W10 按方案 1.5.3/1.5.4/1.10/3.6.2 维护英文枚举值与业务字段名对照表，
  要求「中文（英文）」全角括号配对，并对全角/半角括号内未登记英文记号报错，仅放行最小化系统错误码
  白名单与符合 1.5/3.6.1 编号规则的示例单号，不带括号的普通英文词不检查；W11 检查含「N 天 / N 日」
  与期限措辞的条款句具备「已签收天数 / 已发货天数」口径及与期限数字一致的明确边界说明）。
- W13：编号跨版本稳定性（发布后不重排、不复用、新增追加在章末）靠评审保证；本脚本仅机械识别
  「本条已废止」条款（放行 W9 行首标签检查），并检查单份文档内编号唯一且严格递增。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MAX_CLAUSE_CHARS = 600

GENERAL_LABELS = ("适用：", "规则：", "处理：", "例外：")
TROUBLESHOOT_LABELS = ("现象：", "排查：", "认定：")
DEPRECATED_TEXTS = {"本条已废止", "本条已废止。"}

# Mirrors upstream/dify/api/core/tools/utils/text_processing_utils.py::remove_leading_symbols
DIFY_LEADING_SYMBOL_RE = re.compile(
    r"""
    ^
    (?:
        [\u2000-\u2025]
      | [\u2027-\u206F]
      | [\u2E00-\u2E7F]
      | [\u3000-\u300F]
      | [\u3012-\u303F]
      | ["#$%&'()*+,./:;<=>?@^_`~]
    )+
    """,
    re.VERBOSE,
)

H1_RE = re.compile(r"^# (.+)$")
H2_RE = re.compile(r"^## (?P<doc>\S+) 第 (?P<chap>\d+)\.(?P<num>\d+) 条 (?P<title>.+)$")
ANY_HEADING_RE = re.compile(r"^(#{1,6})(?:\s|$)")

MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
MARKDOWN_TABLE_SEP_RE = re.compile(r"\|?\s*:?-{3,}:?\s*\|")

RELATIVE_REF_PHRASES = (
    "上一条",
    "下一条",
    "如前所述",
    "如上所述",
    "见前条",
    "见上条",
    "前一条",
    "后一条",
    "前述条款",
    "上述条款",
    "前款",
    "上款",
)

CROSS_DOC_REF_RE = re.compile(
    r"《(?P<doc>[^》]+)》\s*(?P<chain>第\s*\d+\.\d+\s*条(?:[、和及与或至]\s*第\s*\d+\.\d+\s*条)*)"
)
CLAUSE_NUM_RE = re.compile(r"第\s*(?P<cid>\d+\.\d+)\s*条")
PROBE_EXPECT_RE = re.compile(r"^(?P<doc>[^\s《》]+)\s+第\s*(?P<cid>\d+\.\d+)\s*条$")

# W10: 英文枚举值/业务字段名 -> 中文标注（仅保留 1.5.3/1.5.4/1.10/3.6.2 真实存在的取值与字段名）
W10_TERM_ZH_MAP: dict[str, tuple[str, ...]] = {
    # 商品类目 category (1.5.3)
    "electronics": ("数码家电",),
    "clothing": ("服饰鞋包",),
    "home": ("家居日用",),
    "fresh_food": ("生鲜食品",),
    "custom_made": ("定制商品",),
    # 售后原因 reason_category (1.5.3)
    "quality_issue": ("质量问题",),
    "no_reason": ("无理由",),
    "wrong_item": ("发错货",),
    "damaged_in_transit": ("运输破损",),
    "not_received": ("未收到货",),
    "other": ("其他",),
    # 客户等级 level (1.5.3)
    "regular": ("普通", "普通客户"),
    "vip": ("会员", "会员客户"),
    # 工单申请类型 type (1.5.2 / 1.10.3)
    "return_refund": ("退货退款",),
    "refund_only": ("仅退款",),
    # 订单状态 status (1.5.4)
    "pending_payment": ("待支付",),
    "paid": ("已支付未发货", "已支付"),
    "shipped": ("已发货未签收", "已发货"),
    "delivered": ("已签收",),
    "completed": ("交易完成", "已完成"),
    "cancelled": ("已取消",),
    # 订单退款进度 refund_status (1.5.4)
    "none": ("未退款", "无退款"),
    "partial": ("部分退款", "已部分退款"),
    "full": ("已全额退款", "全额退款"),
    # 退款单状态 status (1.5.4)
    "processing": ("处理中",),
    "succeeded": ("已成功", "退款成功"),
    "failed": ("退款失败", "已失败", "失败"),
    # 售后工单状态 status (1.5.4) & 审批单状态 status (3.6.2)
    "open": ("待处理",),
    "resolved": ("已解决", "已关闭", "已处理"),
    "pending": ("待审批",),
    "executing": ("执行中",),
    "executed": ("已执行", "已同意且执行成功"),
    "execution_failed": ("已同意但执行失败", "执行失败"),
    "rejected": ("已拒绝", "主管拒绝", "已驳回", "被驳回"),
    "expired": ("已过期", "超过有效期未审批"),
    # 区域 region (1.5.2 / 1.10.3) & 角色 role (G2 / 1.10.5)
    "east": ("华东",),
    "north": ("华北",),
    "south": ("华南",),
    "agent": ("客服",),
    "supervisor": ("主管",),
    # 业务字段名 (1.10 / 3.6.2)
    "days_since_delivery": ("已签收天数",),
    "reason_category": ("原因类别", "售后原因"),
    "category": ("商品类目", "类目"),
    "level": ("客户等级", "等级"),
    "status": ("订单状态", "状态", "工单状态", "退款单状态", "审批状态"),
    "type": ("申请类型", "工单类型"),
    "refund_status": ("退款进度", "订单退款进度"),
    "refundable_amount": ("可退金额",),
    "total_amount": ("实付金额", "订单金额"),
    "refunded_amount": ("已退金额",),
    "region": ("区域", "归属区域"),
}
# 全角括号内除对照表外明确允许的合法系统错误码白名单（最小化，对应操作手册第 3 章）
W10_ALLOWED_PAREN_TOKENS: frozenset[str] = frozenset(
    {
        "order_access_denied",
        "refund_access_denied",
        "approval_access_denied",
        "insufficient_role",
        "invalid_claims",
        "order_not_found",
        "refund_not_found",
        "approval_not_found",
        "token_expired",
        "invalid_token",
        "missing_token",
        "order_not_refundable",
        "approval_pending_exists",
    }
)
# 文档示例单号（按 1.5/3.6.1：订单号 [A-Z]\d{4}，含客户号 C\d{4}、工单号 T\d{4}；
# 审批单号 AP\d{6}；退款单号 R\d{6}）
W10_SAMPLE_ID_RE = re.compile(r"^(?:[A-Z]\d{4}|(?:AP|R)\d{6})$")
W10_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_]*(?![A-Za-z0-9_-])")

# W11: 期限口径与边界检查（支持「日」作为「天」的同义写法并校验边界数字一致性；排除日历日期）
W11_SENTENCE_SPLIT_RE = re.compile(r"[。；！？]+")
W11_DEADLINE_EXPR_RE = re.compile(
    r"(?:"
    r"(?P<prefix>不超过|不大于|不小于|不少于|不多于|超过|超出|达到|不满|未满|已满|满|大于|小于|少于|多于|至多|至少)"
    r"\s*(?P<p_num>\d+)\s*(?P<p_unit>[天日])"
    r"(?:\s*(?P<p_suffix>及以上|及以下|及以内|以上|以下|以内|之内|内|之后|以后|后))?"
    r"|"
    r"(?<!\d)(?P<s_num>\d+)\s*(?P<s_unit>[天日])\s*"
    r"(?P<s_suffix>及以上|及以下|及以内|以上|以下|以内|之内|内|之后|以后|后)"
    r")"
)
W11_CALENDAR_DATE_PREFIX_RE = re.compile(r"[年月]\s*$")
W11_METRIC_PHRASES = ("已签收天数", "已发货天数")
W11_BOUNDARY_TOKEN_RE = re.compile(
    r"(?P<excl>不)?包?含第?\s*(?P<inc_num>\d+)\s*[天日]"
    r"|(?P<range_num>\d+)\s*[天日]及(?P<range_dir>以上|以下|以内)"
)
W11_TRAILING_PAREN_RE = re.compile(r"\s*（([^（）]*)）")
W11_TRAILING_COMMA_BOUNDARY_RE = re.compile(r"\s*，\s*(不?包?含第?\s*\d+\s*[天日])")

REQUIRED_CLAUSES: dict[str, tuple[str, ...]] = {
    "售后制度": (
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "1.5",
        "2.1",
        "2.2",
        "2.3",
        "2.4",
        "3.1",
        "3.2",
        "3.3",
        "3.4",
        "3.5",
        "3.6",
        "3.7",
        "4.1",
        "4.2",
        "4.3",
        "5.1",
        "5.2",
        "5.3",
        "5.4",
        "5.5",
        "6.1",
        "6.2",
        "7.1",
        "7.2",
        "7.3",
        "7.4",
        "7.5",
        "7.6",
        "8.1",
        "8.2",
        "8.3",
    ),
    "客服操作手册": (
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "2.1",
        "2.2",
        "2.3",
        "2.4",
        "2.5",
        "3.1",
        "3.2",
        "3.3",
        "3.4",
        "3.5",
        "3.6",
        "3.7",
        "4.1",
        "4.2",
    ),
    "故障排查手册": (
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "1.5",
        "2.1",
        "2.2",
        "3.1",
        "3.2",
        "4.1",
        "4.2",
        "4.3",
    ),
}


@dataclass(frozen=True)
class LintError:
    file: str
    line: int
    rule: str
    message: str

    def format(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line > 0 else self.file
        return f"[{self.rule}] {loc}: {self.message}"


@dataclass
class ClauseInfo:
    clause_id: str
    chapter: int
    number: int
    title: str
    heading_line: int
    body_lines: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class DocumentReport:
    path: Path
    doc_name: str
    clauses: list[ClauseInfo] = field(default_factory=list)
    h1_body_lines: list[tuple[int, str]] = field(default_factory=list)

    @property
    def clause_count(self) -> int:
        return len(self.clauses)

    @property
    def clause_ids(self) -> set[str]:
        return {c.clause_id for c in self.clauses}


def count_h2_headings(path: Path) -> int:
    """Count level-2 Markdown headings (## ) in a UTF-8 Markdown document."""
    text = path.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.startswith("## "))


def _has_leading_symbol(text: str) -> bool:
    return bool(DIFY_LEADING_SYMBOL_RE.match(text))


def _check_w8_line(rel_path: str, line_no: int, line: str, errors: list[LintError]) -> None:
    if "<" in line or ">" in line:
        errors.append(LintError(rel_path, line_no, "W8", "禁止使用尖括号或 HTML 标签（< / >）"))
    if "![" in line:
        errors.append(LintError(rel_path, line_no, "W8", "禁止使用图片语法（![）"))
    if "](" in line:
        errors.append(LintError(rel_path, line_no, "W8", "禁止使用 Markdown 链接语法（](）"))
    if MARKDOWN_TABLE_ROW_RE.match(line) or MARKDOWN_TABLE_SEP_RE.search(line):
        errors.append(LintError(rel_path, line_no, "W8", "禁止使用 Markdown 表格"))


def _paren_spans(line: str, open_ch: str, close_ch: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for idx, ch in enumerate(line):
        if ch == open_ch:
            stack.append(idx)
        elif ch == close_ch and stack:
            spans.append((stack.pop(), idx))
    return spans


def _check_w10_line(rel_path: str, line_no: int, line: str, errors: list[LintError]) -> None:
    fullwidth_spans = _paren_spans(line, "（", "）")
    halfwidth_spans = _paren_spans(line, "(", ")")
    for match in W10_TOKEN_RE.finditer(line):
        token = match.group(0)
        start, end = match.span()
        expected_zh = W10_TERM_ZH_MAP.get(token)
        if expected_zh is None:
            inside_halfwidth = any(
                p_start < start and end <= p_end for p_start, p_end in halfwidth_spans
            )
            inside_fullwidth = any(
                p_start < start and end <= p_end for p_start, p_end in fullwidth_spans
            )
            if not inside_fullwidth and not inside_halfwidth:
                continue

            is_allowed = token in W10_ALLOWED_PAREN_TOKENS or bool(W10_SAMPLE_ID_RE.match(token))
            if inside_halfwidth:
                if is_allowed:
                    msg = f"半角括号内英文记号「{token}」请改用全角括号「（）」"
                elif token.lower() in W10_TERM_ZH_MAP:
                    lower = token.lower()
                    msg = (
                        f"半角括号内英文记号「{token}」大小写不符且应改用全角括号，"
                        f"规范写法应为「{lower}」"
                    )
                else:
                    msg = (
                        f"半角括号内出现未登记的英文记号「{token}」，"
                        f"请改用全角括号并核对是否为真实接口字段/枚举取值或移除英文标注"
                    )
                errors.append(LintError(rel_path, line_no, "W10", msg))
                continue

            if not is_allowed:
                lower = token.lower()
                if lower in W10_TERM_ZH_MAP:
                    msg = f"全角括号内英文记号「{token}」大小写不符，规范写法应为「{lower}」"
                else:
                    msg = (
                        f"全角括号内出现未登记的英文记号「{token}」，"
                        f"请核对是否为真实接口字段/枚举取值或移除英文标注"
                    )
                errors.append(LintError(rel_path, line_no, "W10", msg))
            continue

        has_fullwidth_parens = (
            start >= 1 and line[start - 1] == "（" and end < len(line) and line[end] == "）"
        )
        has_halfwidth_parens = (
            start >= 1 and line[start - 1] == "(" and end < len(line) and line[end] == ")"
        )
        prefix = line[: start - 1] if has_fullwidth_parens else ""
        has_matching_zh = has_fullwidth_parens and any(prefix.endswith(zh) for zh in expected_zh)

        if not has_matching_zh:
            sample = f"{expected_zh[0]}（{token}）"
            allowed = " / ".join(expected_zh)
            if has_halfwidth_parens:
                msg = (
                    f"英文取值/字段「{token}」使用了半角括号，"
                    f"请改用全角括号包住并紧跟在对应中文（{allowed}）后，如「{sample}」"
                )
            elif not has_fullwidth_parens:
                msg = (
                    f"英文取值/字段「{token}」必须紧跟在对应中文（{allowed}）后并用全角括号包住，"
                    f"如「{sample}」"
                )
            else:
                msg = (
                    f"英文取值/字段「{token}」前缺少对应中文标注（应为：{allowed}），如「{sample}」"
                )
            errors.append(LintError(rel_path, line_no, "W10", msg))


def _classify_deadline(prefix: str, suffix: str) -> str:
    if prefix in {"不超过", "不大于", "不多于", "至多"}:
        return "le"
    if prefix in {"不满", "未满", "小于", "少于"}:
        return "lt"
    if prefix in {"超过", "超出", "大于", "多于"}:
        return "gt"
    if prefix in {"达到", "不小于", "不少于", "至少", "已满", "满"}:
        return "ge"
    if suffix == "及以上":
        return "ge"
    if suffix in {"及以下", "及以内"}:
        return "le"
    if suffix in {"之后", "以后", "后"}:
        return "gt"
    if suffix == "以上":
        return "lower_ambig"
    return "upper_ambig"


def _extract_boundary_tokens(text: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    for m in W11_BOUNDARY_TOKEN_RE.finditer(text):
        if m.group("inc_num") is not None:
            kind = "exclusive" if m.group("excl") else "inclusive"
            tokens.append((m.group(0), kind, int(m.group("inc_num"))))
        else:
            rdir = m.group("range_dir")
            kind = "ge" if rdir == "以上" else "le"
            tokens.append((m.group(0), kind, int(m.group("range_num"))))
    return tokens


def _is_valid_boundary(deadline_kind: str, n: int, boundary_kind: str, m: int) -> bool:
    if deadline_kind == "le":
        return (boundary_kind in {"inclusive", "le"} and m == n) or (
            boundary_kind == "exclusive" and m == n + 1
        )
    if deadline_kind == "lt":
        return (boundary_kind == "exclusive" and m == n) or (
            n >= 1 and boundary_kind in {"inclusive", "le"} and m == n - 1
        )
    if deadline_kind == "ge":
        return (boundary_kind in {"inclusive", "ge"} and m == n) or (
            n >= 1 and boundary_kind == "exclusive" and m == n - 1
        )
    if deadline_kind == "gt":
        return (boundary_kind == "exclusive" and m == n) or (
            boundary_kind in {"inclusive", "ge"} and m == n + 1
        )
    if deadline_kind == "upper_ambig":
        return (boundary_kind in {"inclusive", "exclusive", "le"} and m == n) or (
            n >= 1 and boundary_kind == "le" and m == n - 1
        )
    return (boundary_kind in {"inclusive", "exclusive", "ge"} and m == n) or (
        boundary_kind == "ge" and m == n + 1
    )


def _check_w11_line(rel_path: str, line_no: int, line: str, errors: list[LintError]) -> None:
    body = line
    for label in (*GENERAL_LABELS, *TROUBLESHOOT_LABELS):
        if body.startswith(label):
            body = body[len(label) :]
            break

    for sentence in W11_SENTENCE_SPLIT_RE.split(body):
        sentence = sentence.strip()
        if not sentence:
            continue

        deadlines: list[tuple[str, bool, list[str]]] = []
        pos = 0
        while True:
            match = W11_DEADLINE_EXPR_RE.search(sentence, pos)
            if match is None:
                break
            expr = match.group(0)
            prefix = match.group("prefix") or ""
            if prefix:
                num = int(match.group("p_num"))
                unit = match.group("p_unit")
                suffix = match.group("p_suffix") or ""
            else:
                num = int(match.group("s_num"))
                unit = match.group("s_unit")
                suffix = match.group("s_suffix") or ""

            if (
                not prefix
                and unit == "日"
                and W11_CALENDAR_DATE_PREFIX_RE.search(sentence[: match.start()])
            ):
                pos = match.end()
                continue

            deadline_kind = _classify_deadline(prefix, suffix)

            after = sentence[match.end() :]
            paren = W11_TRAILING_PAREN_RE.match(after)
            paren_tokens = _extract_boundary_tokens(paren.group(1)) if paren else []
            comma = W11_TRAILING_COMMA_BOUNDARY_RE.match(after)
            comma_tokens = _extract_boundary_tokens(comma.group(1)) if comma else []
            self_has_boundary = bool(W11_BOUNDARY_TOKEN_RE.search(expr))

            trailing_tokens = paren_tokens or comma_tokens
            mismatches = [
                raw_tok
                for raw_tok, b_kind, b_num in trailing_tokens
                if not _is_valid_boundary(deadline_kind, num, b_kind, b_num)
            ]
            has_boundary = self_has_boundary or bool(trailing_tokens)
            deadlines.append((expr, has_boundary, mismatches))
            if paren and paren_tokens:
                pos = match.end() + paren.end()
            elif comma and comma_tokens:
                pos = match.end() + comma.end()
            else:
                pos = match.end()

        if not deadlines:
            continue

        if not any(metric in sentence for metric in W11_METRIC_PHRASES):
            errors.append(
                LintError(
                    rel_path,
                    line_no,
                    "W11",
                    f"期限表述缺少「已签收天数」或「已发货天数」口径：「{sentence}」",
                )
            )
        for expr, has_boundary, mismatches in deadlines:
            if not has_boundary:
                errors.append(
                    LintError(
                        rel_path,
                        line_no,
                        "W11",
                        f"期限表述「{expr}」缺少明确边界说明"
                        f"（如「含第 N 天」「不含第 N 天」「N 天及以上」）：「{sentence}」",
                    )
                )
            for bad_tok in mismatches:
                errors.append(
                    LintError(
                        rel_path,
                        line_no,
                        "W11",
                        f"期限表述「{expr}」的边界说明「{bad_tok}」与期限数字不一致：「{sentence}」",
                    )
                )


def _check_w12_line(rel_path: str, line_no: int, line: str, errors: list[LintError]) -> None:
    for phrase in RELATIVE_REF_PHRASES:
        if phrase in line:
            errors.append(
                LintError(
                    rel_path,
                    line_no,
                    "W12",
                    f"禁止使用相对引用「{phrase}」，请改用明确条款编号「第 X.Y 条」",
                )
            )


def _check_clause_labels(
    rel_path: str,
    doc_name: str,
    clause: ClauseInfo,
    errors: list[LintError],
) -> None:
    if not clause.body_lines:
        errors.append(
            LintError(
                rel_path,
                clause.heading_line,
                "W3",
                f"条款 {clause.clause_id} 缺少正文内容",
            )
        )
        return

    # W13: deprecated clause support ("本条已废止")
    if len(clause.body_lines) == 1 and clause.body_lines[0][1].strip() in DEPRECATED_TEXTS:
        return

    if doc_name == "故障排查手册" and clause.chapter in {1, 2, 3}:
        allowed_labels = TROUBLESHOOT_LABELS
    else:
        allowed_labels = GENERAL_LABELS

    seen_labels: set[str] = set()
    for line_no, text in clause.body_lines:
        matched_label = next((lbl for lbl in allowed_labels if text.startswith(lbl)), None)
        if matched_label is None:
            expected = " / ".join(allowed_labels)
            errors.append(
                LintError(
                    rel_path,
                    line_no,
                    "W9",
                    f"条款 {clause.clause_id} 正文行首必须使用固定标签（{expected}）",
                )
            )
            continue
        if matched_label in seen_labels:
            errors.append(
                LintError(
                    rel_path,
                    line_no,
                    "W9",
                    f"条款 {clause.clause_id} 重复出现行首标签「{matched_label}」",
                )
            )
        seen_labels.add(matched_label)
        content_after = text[len(matched_label) :].strip()
        if not content_after:
            errors.append(
                LintError(
                    rel_path,
                    line_no,
                    "W9",
                    f"条款 {clause.clause_id} 标签「{matched_label}」后缺少内容",
                )
            )


def lint_document(path: Path, kb_dir: Path) -> tuple[list[LintError], DocumentReport | None]:
    """Lint a single Markdown document for W1-W12 and W13 deprecated-clause recognition."""
    errors: list[LintError] = []
    try:
        rel_path = str(path.relative_to(kb_dir))
    except ValueError:
        rel_path = str(path)

    # W1: extension and UTF-8 encoding
    if path.suffix.lower() != ".md":
        errors.append(LintError(rel_path, 0, "W1", "知识库文档必须使用 .md 扩展名"))
        return errors, None

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        errors.append(LintError(rel_path, 0, "W1", f"无法读取文件：{exc}"))
        return errors, None

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        errors.append(LintError(rel_path, 1, "W1", "文件不得包含 UTF-8 BOM 头"))
        return errors, None

    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(LintError(rel_path, 0, "W1", f"文件不是合法的 UTF-8 编码：{exc}"))
        return errors, None

    if "\r" in content:
        errors.append(LintError(rel_path, 1, "W1", "请统一使用 LF 换行符，不要包含 CR（\\r）"))

    # Strip one trailing newline so split("\n") gives exact logical lines.
    if content.endswith("\n"):
        content = content[:-1]
    lines = content.split("\n")

    if not lines or not any(line.strip() for line in lines):
        errors.append(LintError(rel_path, 1, "W2", "文档为空"))
        return errors, None

    # W6: no leading or trailing blank lines
    if lines[0] == "":
        errors.append(LintError(rel_path, 1, "W6", "文档开头不得有空行"))
    if lines[-1] == "":
        errors.append(LintError(rel_path, len(lines), "W6", "文档末尾不得有多余空行"))

    # First pass: check headings, blank lines (W6), W4, W8, W10, W12
    h1_indices: list[int] = []
    for idx, line in enumerate(lines):
        line_no = idx + 1
        _check_w8_line(rel_path, line_no, line, errors)

        if line.strip() == "":
            if line != "":
                errors.append(LintError(rel_path, line_no, "W6", "空行不得包含空白字符"))
            if idx > 0 and lines[idx - 1].strip() == "":
                errors.append(
                    LintError(rel_path, line_no, "W6", "条款之间只能空一行，不得出现连续空行")
                )
            elif idx + 1 < len(lines) and not lines[idx + 1].startswith("## "):
                errors.append(
                    LintError(rel_path, line_no, "W6", "条款内部（或一级标题说明内部）不得留空行")
                )
            continue

        _check_w10_line(rel_path, line_no, line, errors)

        heading_match = ANY_HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            if level == 1:
                h1_indices.append(idx)
            elif level == 2:
                if idx == 0 or lines[idx - 1] != "":
                    errors.append(
                        LintError(rel_path, line_no, "W6", "每条条款标题前必须恰好空一行")
                    )
            else:
                errors.append(
                    LintError(
                        rel_path,
                        line_no,
                        "W4",
                        f"禁止使用 {level} 级标题（仅允许一个一级标题与条款二级标题）",
                    )
                )
        else:
            _check_w12_line(rel_path, line_no, line, errors)

    # W2: exactly one H1 at line 1
    if len(h1_indices) == 0:
        errors.append(LintError(rel_path, 1, "W2", "缺少一级标题（# <文档名>）"))
        return errors, None
    if len(h1_indices) > 1:
        errors.append(LintError(rel_path, h1_indices[1] + 1, "W2", "只能有一个一级标题（文档名）"))
    if h1_indices[0] != 0:
        errors.append(LintError(rel_path, h1_indices[0] + 1, "W2", "一级标题必须位于文档第 1 行"))

    h1_match = H1_RE.match(lines[h1_indices[0]])
    if not h1_match:
        errors.append(
            LintError(rel_path, h1_indices[0] + 1, "W2", "一级标题格式必须为「# <文档名>」")
        )
        return errors, None

    doc_name = h1_match.group(1).strip()
    if not doc_name:
        errors.append(LintError(rel_path, h1_indices[0] + 1, "W2", "一级标题文档名不能为空"))
        return errors, None
    if "《" in doc_name or "》" in doc_name:
        errors.append(LintError(rel_path, h1_indices[0] + 1, "W5", "一级标题文档名不得加书名号"))
    if _has_leading_symbol(doc_name):
        errors.append(LintError(rel_path, h1_indices[0] + 1, "W5", "一级标题不得以标点符号开头"))

    # Parse sections (H1 description + each H2 clause)
    report = DocumentReport(path=path, doc_name=doc_name)
    current_clause: ClauseInfo | None = None
    in_h1 = False
    h1_block_lines: list[str] = []
    clause_block_lines: list[tuple[ClauseInfo, list[str]]] = []
    current_raw_block: list[str] = []

    for idx, line in enumerate(lines):
        line_no = idx + 1
        if idx == h1_indices[0]:
            in_h1 = True
            h1_block_lines = [line]
            continue

        if line == "":
            continue

        if line.startswith("## "):
            if current_clause is not None:
                clause_block_lines.append((current_clause, current_raw_block))
            in_h1 = False
            h2_match = H2_RE.match(line)
            if not h2_match:
                errors.append(
                    LintError(
                        rel_path,
                        line_no,
                        "W3",
                        f"二级标题格式必须为「## {doc_name} 第 <章>.<条> 条 <条款标题>」",
                    )
                )
                current_clause = None
                current_raw_block = []
                continue

            h2_doc = h2_match.group("doc")
            chap = int(h2_match.group("chap"))
            num = int(h2_match.group("num"))
            title = h2_match.group("title")
            cid = f"{chap}.{num}"

            if "《" in h2_doc or "》" in h2_doc or "《" in title or "》" in title:
                errors.append(
                    LintError(rel_path, line_no, "W5", "二级标题文档名与条款标题不得包含书名号")
                )
            if h2_doc != doc_name:
                errors.append(
                    LintError(
                        rel_path,
                        line_no,
                        "W3",
                        f"二级标题文档名「{h2_doc}」与一级标题「{doc_name}」不一致",
                    )
                )
            heading_text = line[3:]
            if (
                _has_leading_symbol(heading_text)
                or _has_leading_symbol(title)
                or title != title.strip()
            ):
                errors.append(
                    LintError(rel_path, line_no, "W5", "二级标题与条款标题不得以标点或多余空格开头")
                )

            current_clause = ClauseInfo(
                clause_id=cid,
                chapter=chap,
                number=num,
                title=title,
                heading_line=line_no,
            )
            report.clauses.append(current_clause)
            current_raw_block = [line]
            continue

        if ANY_HEADING_RE.match(line):
            # Already reported under W2/W4 above
            continue

        # Body line
        if _has_leading_symbol(line):
            errors.append(
                LintError(rel_path, line_no, "W5", "正文行首不得以 Dify 会裁剪的标点符号开头")
            )

        if in_h1:
            report.h1_body_lines.append((line_no, line))
            h1_block_lines.append(line)
        elif current_clause is not None:
            current_clause.body_lines.append((line_no, line))
            current_raw_block.append(line)

    if current_clause is not None:
        clause_block_lines.append((current_clause, current_raw_block))

    # W2: H1 must have description text and fit W7 length
    if not report.h1_body_lines:
        errors.append(
            LintError(rel_path, h1_indices[0] + 1, "W2", "一级标题下必须有说明文档用途与效力的正文")
        )
    h1_full_text = "\n".join(h1_block_lines)
    if len(h1_full_text) > MAX_CLAUSE_CHARS:
        errors.append(
            LintError(
                rel_path,
                h1_indices[0] + 1,
                "W7",
                f"一级标题说明段长度为 {len(h1_full_text)} 字，超过上限 {MAX_CLAUSE_CHARS} 字",
            )
        )

    # Check each clause: W7 length, W9 labels, numbering uniqueness & monotonic order
    seen_ids: dict[str, int] = {}
    prev_chap: int | None = None
    prev_num: int | None = None

    for clause, raw_lines in clause_block_lines:
        full_text = "\n".join(raw_lines)
        if len(full_text) > MAX_CLAUSE_CHARS:
            msg = (
                f"条款 {clause.clause_id} 长度为 {len(full_text)} 字，"
                f"超过上限 {MAX_CLAUSE_CHARS} 字"
            )
            errors.append(LintError(rel_path, clause.heading_line, "W7", msg))

        _check_clause_labels(rel_path, doc_name, clause, errors)
        is_deprecated = (
            len(clause.body_lines) == 1 and clause.body_lines[0][1].strip() in DEPRECATED_TEXTS
        )
        if not is_deprecated:
            for body_line_no, body_text in clause.body_lines:
                _check_w11_line(rel_path, body_line_no, body_text, errors)

        if clause.clause_id in seen_ids:
            first_line = seen_ids[clause.clause_id]
            errors.append(
                LintError(
                    rel_path,
                    clause.heading_line,
                    "NUM",
                    f"条款编号 {clause.clause_id} 重复（首次出现于第 {first_line} 行）",
                )
            )
        else:
            seen_ids[clause.clause_id] = clause.heading_line

        if prev_chap is not None:
            if clause.chapter < prev_chap:
                errors.append(
                    LintError(
                        rel_path,
                        clause.heading_line,
                        "NUM",
                        f"章号未按顺序排列：{clause.clause_id} 出现在第 {prev_chap} 章之后",
                    )
                )
            elif clause.chapter == prev_chap and prev_num is not None and clause.number <= prev_num:
                errors.append(
                    LintError(
                        rel_path,
                        clause.heading_line,
                        "NUM",
                        f"同章内条款编号未严格递增：{clause.clause_id} 紧跟 {prev_chap}.{prev_num}",
                    )
                )
        prev_chap = clause.chapter
        prev_num = clause.number

    # Check completeness against 4.7.3 / 4.7.4 required clause inventory
    if doc_name in REQUIRED_CLAUSES:
        missing = [cid for cid in REQUIRED_CLAUSES[doc_name] if cid not in report.clause_ids]
        if missing:
            errors.append(
                LintError(
                    rel_path,
                    0,
                    "COMPLETENESS",
                    f"《{doc_name}》缺少规范要求的条款编号：{', '.join(missing)}",
                )
            )

    return errors, report


def _check_cross_references(
    reports: list[DocumentReport],
    kb_dir: Path,
    errors: list[LintError],
) -> None:
    clauses_by_doc: dict[str, set[str]] = {r.doc_name: r.clause_ids for r in reports}

    for report in reports:
        try:
            rel_path = str(report.path.relative_to(kb_dir))
        except ValueError:
            rel_path = str(report.path)

        all_body_lines: list[tuple[int, str]] = list(report.h1_body_lines)
        for clause in report.clauses:
            all_body_lines.extend(clause.body_lines)

        for line_no, text in all_body_lines:
            covered_spans: list[tuple[int, int]] = []
            for match in CROSS_DOC_REF_RE.finditer(text):
                covered_spans.append(match.span())
                target_doc = match.group("doc")
                chain_text = match.group("chain")
                if target_doc not in clauses_by_doc:
                    errors.append(
                        LintError(
                            rel_path,
                            line_no,
                            "REF",
                            f"交叉引用了不存在的文档《{target_doc}》",
                        )
                    )
                    continue
                for cid_match in CLAUSE_NUM_RE.finditer(chain_text):
                    cid = cid_match.group("cid")
                    if cid not in clauses_by_doc[target_doc]:
                        errors.append(
                            LintError(
                                rel_path,
                                line_no,
                                "REF",
                                f"交叉引用了不存在的条款：《{target_doc}》第 {cid} 条",
                            )
                        )

            for cid_match in CLAUSE_NUM_RE.finditer(text):
                start, end = cid_match.span()
                if any(s <= start and end <= e for s, e in covered_spans):
                    continue
                cid = cid_match.group("cid")
                if cid not in clauses_by_doc[report.doc_name]:
                    errors.append(
                        LintError(
                            rel_path,
                            line_no,
                            "REF",
                            f"引用了本文件不存在的条款：第 {cid} 条",
                        )
                    )


def _check_probes(
    kb_dir: Path,
    kb_config: dict[str, Any] | None,
    reports: list[DocumentReport],
    errors: list[LintError],
) -> None:
    probes_path = kb_dir / "probes.yaml"
    if not probes_path.exists():
        return

    try:
        raw = yaml.safe_load(probes_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(LintError("probes.yaml", 0, "PROBE", f"无法解析 probes.yaml：{exc}"))
        return

    probes = raw.get("probes") if isinstance(raw, dict) else raw
    if not isinstance(probes, list):
        errors.append(LintError("probes.yaml", 0, "PROBE", "probes.yaml 必须包含 probes 列表"))
        return

    report_by_rel: dict[str, DocumentReport] = {}
    for report in reports:
        try:
            report_by_rel[str(report.path.relative_to(kb_dir))] = report
        except ValueError:
            pass

    dataset_docs: dict[str, set[str]] = {}
    if kb_config and isinstance(kb_config.get("datasets"), list):
        for ds in kb_config["datasets"]:
            key = ds.get("key")
            docs = ds.get("documents", [])
            doc_names: set[str] = set()
            for rel in docs:
                if rel in report_by_rel:
                    doc_names.add(report_by_rel[rel].doc_name)
            if isinstance(key, str):
                dataset_docs[key] = doc_names

    clauses_by_doc = {r.doc_name: r.clause_ids for r in reports}

    for idx, item in enumerate(probes, start=1):
        if not isinstance(item, dict):
            errors.append(LintError("probes.yaml", 0, "PROBE", f"第 {idx} 条探针必须为对象"))
            continue
        pid = item.get("id", f"#{idx}")
        ds_key = item.get("dataset")
        expect = item.get("expect")
        if dataset_docs and ds_key not in dataset_docs:
            errors.append(
                LintError("probes.yaml", 0, "PROBE", f"探针 {pid} 引用了未知 dataset「{ds_key}」")
            )
            continue
        if not isinstance(expect, list) or not expect:
            errors.append(LintError("probes.yaml", 0, "PROBE", f"探针 {pid} 缺少 expect 列表"))
            continue
        for exp in expect:
            m = PROBE_EXPECT_RE.match(str(exp).strip())
            if not m:
                errors.append(
                    LintError(
                        "probes.yaml",
                        0,
                        "PROBE",
                        f"探针 {pid} 的期望条款「{exp}」格式应为「<文档名> 第 X.Y 条」",
                    )
                )
                continue
            doc_name = m.group("doc")
            cid = m.group("cid")
            if doc_name not in clauses_by_doc or cid not in clauses_by_doc[doc_name]:
                errors.append(
                    LintError(
                        "probes.yaml",
                        0,
                        "PROBE",
                        f"探针 {pid} 期望的条款不存在：{doc_name} 第 {cid} 条",
                    )
                )
            elif dataset_docs and doc_name not in dataset_docs.get(str(ds_key), set()):
                errors.append(
                    LintError(
                        "probes.yaml",
                        0,
                        "PROBE",
                        f"探针 {pid} 的期望文档「{doc_name}」不属于 dataset「{ds_key}」",
                    )
                )


def lint_all(kb_dir: Path) -> tuple[list[LintError], list[DocumentReport]]:
    """Run W1-W12, W13 deprecated-clause, completeness, and cross-reference checks under kb_dir."""
    kb_dir = kb_dir.resolve()
    errors: list[LintError] = []
    kb_config: dict[str, Any] | None = None
    doc_paths: list[Path] = []
    seen_paths: set[Path] = set()

    kb_yaml_path = kb_dir / "kb.yaml"
    if kb_yaml_path.exists():
        try:
            loaded = yaml.safe_load(kb_yaml_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                kb_config = loaded
                for ds in loaded.get("datasets", []):
                    for rel in ds.get("documents", []):
                        p = (kb_dir / rel).resolve()
                        if not p.exists():
                            errors.append(
                                LintError("kb.yaml", 0, "W1", f"清单声明的文档不存在：{rel}")
                            )
                            continue
                        if p not in seen_paths:
                            seen_paths.add(p)
                            doc_paths.append(p)
        except Exception as exc:
            errors.append(LintError("kb.yaml", 0, "W1", f"无法解析 kb.yaml：{exc}"))

    docs_root = kb_dir / "docs"
    if docs_root.exists():
        for p in sorted(docs_root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            resolved = p.resolve()
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                doc_paths.append(resolved)

    if not doc_paths:
        errors.append(LintError("docs", 0, "W1", "未找到任何知识库文档"))
        return errors, []

    reports: list[DocumentReport] = []
    for doc_path in doc_paths:
        doc_errors, report = lint_document(doc_path, kb_dir)
        errors.extend(doc_errors)
        if report is not None:
            reports.append(report)

    _check_cross_references(reports, kb_dir, errors)
    _check_probes(kb_dir, kb_config, reports, errors)
    return errors, reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查知识库 Markdown 文档是否符合写作规范（W1-W12 及 W13 废止条款识别）"
    )
    parser.add_argument(
        "--kb-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="knowledge-base 目录路径（默认为脚本上级目录）",
    )
    args = parser.parse_args(argv)

    errors, reports = lint_all(args.kb_dir)
    if errors:
        print(f"知识库检查失败，共发现 {len(errors)} 处错误：", file=sys.stderr)
        for err in errors:
            print(f"  - {err.format()}", file=sys.stderr)
        return 1

    total_clauses = sum(r.clause_count for r in reports)
    print(f"知识库检查通过：共 {len(reports)} 份文档、{total_clauses} 条条款。")
    for report in reports:
        rel = report.path.relative_to(args.kb_dir.resolve())
        expected_segs = report.clause_count + 1
        print(
            f"  - {report.doc_name}（{rel}）：{report.clause_count} 条条款"
            f"（预期 {expected_segs} 个分段）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
