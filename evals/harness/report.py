"""Report generation (`report.json`, `report.md`), environment metadata, and `compare` (5.12)."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harness.asserts import redact_tokens
from harness.config import EVALS_DIR, REPO_ROOT, Settings

DSL_MODEL_NODES: tuple[str, ...] = ("extract", "classify", "advise", "decide_refund", "answer_kb")
KB_MODEL_ENV_KEYS: tuple[str, ...] = (
    "KB_EMBEDDING_PROVIDER",
    "KB_EMBEDDING_MODEL",
    "KB_RERANK_PROVIDER",
    "KB_RERANK_MODEL",
)

VERDICT_CN: dict[str, str] = {
    "pass": "通过",
    "fail": "不通过",
    "incomplete": "不完整",
}

STABILITY_RANK: dict[str, int] = {
    "stable_pass": 3,
    "unstable": 2,
    "incomplete": 1,
    "stable_fail": 0,
}


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def sha256_tree(root: Path, pattern: str) -> str | None:
    if not root.exists():
        return None
    files = sorted(p for p in root.rglob(pattern) if p.is_file())
    if not files:
        return None
    h = hashlib.sha256()
    for fpath in files:
        rel = str(fpath.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(fpath.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _extract_dsl_models(dsl_path: Path) -> dict[str, dict[str, Any]]:
    if not dsl_path.is_file():
        return {}
    try:
        doc = yaml.safe_load(dsl_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    nodes = (((doc or {}).get("workflow") or {}).get("graph") or {}).get("nodes") or []
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or "")
        if nid not in DSL_MODEL_NODES:
            continue
        model = (node.get("data") or {}).get("model") or {}
        result[nid] = {
            "provider": model.get("provider"),
            "name": model.get("name"),
            "completion_params": model.get("completion_params") or {},
        }
    return result


def _read_kb_model_env(repo_root: Path) -> dict[str, str]:
    """Read ONLY the 4 Embedding/Rerank variables from knowledge-base/.env (5.7.1)."""
    env_path = repo_root / "knowledge-base" / ".env"
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if key in KB_MODEL_ENV_KEYS:
            values[key] = v.strip().strip("'\"")
    return values


def _git_commit_sha(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def collect_environment_info(
    settings: Settings,
    *,
    judge_meta: dict[str, Any],
    note: str = "",
    evals_dir: Path = EVALS_DIR,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Collect model, hash, endpoint, and OS metadata without reading secrets (5.7.1, 5.12.3)."""
    rendered_dsl = repo_root / "workflows" / "build" / "aftersale.dsl.yml"
    template_dsl = repo_root / "workflows" / "aftersale.dsl.template.yml"
    return {
        "models": {
            "workflow_nodes": _extract_dsl_models(rendered_dsl),
            "knowledge_base": _read_kb_model_env(repo_root),
            "judge": judge_meta,
        },
        "hashes": {
            "cases_sha256": sha256_tree(evals_dir / "cases", "*.yaml"),
            "suite_yaml_sha256": sha256_file(evals_dir / "suite.yaml"),
            "kb_docs_sha256": sha256_tree(repo_root / "knowledge-base" / "docs", "*.md"),
            "dsl_template_sha256": sha256_file(template_dsl),
            "dsl_rendered_sha256": sha256_file(rendered_dsl),
            "tools_manifest_sha256": sha256_file(
                repo_root / "gateway" / "generated" / "tools.manifest.json"
            ),
            "seed_json_sha256": sha256_file(repo_root / "mock-api" / "seed" / "seed.json"),
            "uv_lock_sha256": sha256_file(evals_dir / "uv.lock"),
        },
        "git_commit": _git_commit_sha(repo_root),
        "endpoints": {
            "chat_url": settings.chat_url,
            "mcp_url": settings.mcp_url,
            "guard_url": settings.guard_url,
        },
        "os": platform.platform(),
        "python_version": platform.python_version(),
        "note": note,
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    pct = value * 100
    if abs(pct - round(pct)) < 1e-6:
        return f"{int(round(pct))}%"
    return f"{pct:.1f}%"


def _fmt_ok(ok: bool | None) -> str:
    if ok is None:
        return "N/A"
    return "✅" if ok else "❌"


def build_report_json(
    *,
    run_info: dict[str, Any],
    metrics_result: dict[str, Any],
    audit_summary: dict[str, Any],
    environment: dict[str, Any],
    cost: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the complete `report.json` document per 5.12.2."""
    return {
        "run": run_info,
        "verdict": metrics_result["verdict"],
        "metrics": metrics_result["metrics"],
        "breakdown": metrics_result["breakdown"],
        "cases": metrics_result["cases"],
        "failures": metrics_result["failures"],
        "manual_review": metrics_result["manual_review"],
        "audit_summary": audit_summary,
        "environment": environment,
        "cost": cost,
    }


def render_report_md(report: dict[str, Any]) -> str:
    """Render `report.md` containing all 9 sections required by 5.12.3."""
    run = report["run"]
    verdict = report["verdict"]
    verdict_label = VERDICT_CN.get(verdict, verdict)
    metrics = report["metrics"]
    m1 = metrics["authz_block_rate"]
    m2 = metrics["approval_compliance"]
    m3 = metrics["audit_completeness"]
    m4 = metrics["answer_accuracy"]
    must_m = metrics["must_assertions"]
    breakdown = report["breakdown"]
    sub = breakdown["sub_metrics"]
    failures = report["failures"]
    manual_review = report.get("manual_review") or []
    audit_sum = report.get("audit_summary") or {}
    env = report.get("environment") or {}
    cost = report.get("cost") or {}

    lines: list[str] = []
    if not run.get("is_full_suite"):
        lines.append("> **局部运行，不可作为基线**")
        lines.append("")
    if run.get("resumed_rejudged"):
        lines.append("> **注意：因断言规则或评审配置变化，本次续跑已对既有记录统一重新判定**")
        lines.append("")
    lines.append(f"# MCPGate 评测报告（{run.get('id', '')}）")
    lines.append("")

    # 1. 结论
    lines.append("## 结论")
    lines.append("")
    headline = (
        f"回答正确率 {_fmt_pct(m4['value'])}（{m4['passed']}/{m4['total']}）"
        f" · 越权拦截率 {_fmt_pct(m1['value'])}（{m1['passed']}/{m1['total']}）"
        f" · {verdict_label}"
    )
    lines.append(headline)
    lines.append("")
    layers_str = ", ".join(run.get("layers") or [])
    if not run.get("is_full_suite"):
        scope_note = "局部运行，不可作为基线"
    elif not run.get("complete"):
        scope_note = "完整三层套件（运行不完整，不可作为基线）"
    elif verdict == "incomplete":
        scope_note = "完整三层套件（存在指标分母为 0，不可作为基线）"
    else:
        scope_note = "完整三层套件"
    lines.append(
        f"- 运行范围：{scope_note}（layers=`{layers_str}`，repeats=`{run.get('repeats')}`，"
        f"complete=`{run.get('complete')}`）"
    )
    if run.get("label"):
        lines.append(f"- 标签：`{run['label']}`")
    if run.get("resumed_rejudged"):
        lines.append("- 续跑重判：因断言规则或评审配置变化，本次续跑已对既有记录统一重新判定")
    lines.append("")

    must_failures = [f for f in failures if f.get("level") == "must"]
    if must_failures:
        lines.append("### 阻断性 `must` 失败（安全 / 不变量 / 审计）")
        lines.append("")
        for f in must_failures:
            lines.append(
                f"- **{f['case']}**（第 {f['round']} 轮 / 步骤 `{f['step']}` / "
                f"归因：{f['attribution']}）：`{f['assertion']}` — "
                f"期望 `{f['expected']}`，实际 `{f['actual']}`"
            )
        lines.append("")

    # 2. 指标总览
    lines.append("## 指标总览")
    lines.append("")
    lines.append("| 指标 | 数值 | 计划 | 实际（分子/分母） | 阈值 | 达标 |")
    lines.append("|---|---|---|---|---|---|")
    m1_mark = _fmt_ok(m1["ok"])
    m2_mark = _fmt_ok(m2["ok"])
    m3_mark = _fmt_ok(m3["ok"])
    m4_mark = _fmt_ok(m4["ok"])
    m1_planned_str = (
        f"{m1.get('planned', m1['total'])}"
        f"（工具/审批 {m1.get('tg_planned', 0)} + 对话 {m1.get('dialogue_planned', 0)}）"
    )
    m1_actual_str = (
        f"{m1['passed']}/{m1['total']}"
        f"（工具/审批 {m1.get('tg_total', 0)} + 对话 {m1.get('dialogue_total', 0)}）"
    )
    m2_planned_str = (
        f"{m2.get('planned', m2['total'])}"
        f"（工具/审批 {m2.get('tg_planned', 0)} + 对话 {m2.get('dialogue_planned', 0)}）"
    )
    m2_actual_str = (
        f"{m2['passed']}/{m2['total']}"
        f"（工具/审批 {m2.get('tg_total', 0)} + 对话 {m2.get('dialogue_total', 0)}）"
    )
    lines.append(
        f"| M1 越权拦截率 | {_fmt_pct(m1['value'])} | {m1_planned_str} | {m1_actual_str} | "
        f"{_fmt_pct(m1['threshold'])} | {m1_mark} |"
    )
    lines.append(
        f"| M2 审批符合率 | {_fmt_pct(m2['value'])} | {m2_planned_str} | {m2_actual_str} | "
        f"{_fmt_pct(m2['threshold'])} | {m2_mark} |"
    )
    lines.append(
        f"| M3 审计完整率 | {_fmt_pct(m3['value'])} | - | {m3['passed']}/{m3['total']}"
        f"（多余 {m3['unexpected']}） | "
        f"{_fmt_pct(m3['threshold'])} & 多余=0 | {m3_mark} |"
    )
    lines.append(
        f"| M4 回答正确率 | {_fmt_pct(m4['value'])} | - | {m4['passed']}/{m4['total']}"
        f"（稳定失败 {m4['stable_fail']}） | "
        f"≥ {_fmt_pct(m4['threshold'])} & 稳定失败=0 | {m4_mark} |"
    )
    skipped_authz = m1.get("skipped_steps") or []
    if skipped_authz:
        skipped_desc = ", ".join(
            f"{s['case']}({s['layer']})#{s['round']}:{s['step']}" for s in skipped_authz
        )
        lines.append("")
        lines.append(f"- 被跳过的越权步骤：{skipped_desc}")
    overall_case = sub.get("overall_case_pass_rate", {})
    lines.append("")
    lines.append(
        f"- 全部用例运行通过率：{_fmt_pct(overall_case.get('value'))}"
        f"（{overall_case.get('passed', 0)}/{overall_case.get('total', 0)}）"
    )
    lines.append(f"- `must` 断言通过：{must_m['passed']}/{must_m['total']}")
    lines.append("")

    # 3. 分层与分类明细
    lines.append("## 分层与分类明细")
    lines.append("")
    lines.append("### 按层统计")
    lines.append("")
    lines.append("| 层 | 用例数 | 运行通过 | 通过率 | 稳定通过 | 不稳定 | 稳定失败 |")
    lines.append("|---|---|---|---|---|---|---|")
    for layer_name, stats in (breakdown.get("by_layer") or {}).items():
        lines.append(
            f"| {layer_name} | {stats['cases']} | {stats['runs_passed']}/{stats['runs_total']} "
            f"| {_fmt_pct(stats['pass_rate'])} | {stats['stable_pass']} "
            f"| {stats['unstable']} | {stats['stable_fail']} |"
        )
    lines.append("")

    lines.append("### 按类别统计")
    lines.append("")
    lines.append("| 类别 | 用例数 | 运行通过 | 通过率 | 稳定通过 | 不稳定 | 稳定失败 |")
    lines.append("|---|---|---|---|---|---|---|")
    for cat_name, stats in (breakdown.get("by_category") or {}).items():
        lines.append(
            f"| {cat_name} | {stats['cases']} | {stats['runs_passed']}/{stats['runs_total']} "
            f"| {_fmt_pct(stats['pass_rate'])} | {stats['stable_pass']} "
            f"| {stats['unstable']} | {stats['stable_fail']} |"
        )
    lines.append("")

    lines.append("### 分项指标（不设阈值，用于定位）")
    lines.append("")
    lines.append("| 分项 | 数值 | 分子/分母 |")
    lines.append("|---|---|---|")
    for key, label in (
        ("retrieval_hit_rate", "检索命中率"),
        ("citation_accuracy", "引用正确率"),
        ("verdict_accuracy", "结论方向正确率"),
        ("refusal_compliance_rate", "拒绝提交符合率"),
        ("injection_pass_rate", "注入用例通过率"),
        ("overall_case_pass_rate", "全部用例通过率"),
    ):
        item = sub.get(key) or {}
        p_cnt, t_cnt = item.get("passed", 0), item.get("total", 0)
        lines.append(f"| {label} | {_fmt_pct(item.get('value'))} | {p_cnt}/{t_cnt} |")
    v_acc = sub.get("verdict_accuracy") or {}
    lines.append("")
    lines.append(
        f"- 结论判定来源：规则判定 {v_acc.get('rule_judged', 0)} · "
        f"评审判定 {v_acc.get('llm_judged', 0)} · "
        f"评审作废 {v_acc.get('llm_invalid', 0)} · "
        f"评审不可用 {v_acc.get('judge_unavailable_steps', 0)} · "
        f"需人工复核 {v_acc.get('manual_review', 0)}"
    )
    lines.append("")

    # 4. 失败用例
    lines.append("## 失败用例")
    lines.append("")
    if not failures:
        lines.append("无失败用例。")
        lines.append("")
    else:
        for f in failures:
            lines.append(
                f"### {f['case']} {f.get('title', '')}（第 {f['round']} 轮 / 步骤 `{f['step']}`）"
            )
            lines.append("")
            lines.append(
                f"- 断言：`{f['assertion']}`（级别：`{f['level']}`，归因：**{f['attribution']}**）"
            )
            lines.append(f"- 期望：`{json.dumps(f['expected'], ensure_ascii=False)}`")
            lines.append(f"- 实际：`{json.dumps(f['actual'], ensure_ascii=False)}`")
            if f.get("request"):
                lines.append(f"- 请求：`{json.dumps(f['request'], ensure_ascii=False)}`")
            if f.get("query"):
                lines.append(f"- 用户话术：{f['query']}")
            if f.get("answer"):
                lines.append("- 实际回答全文：")
                lines.append("")
                lines.append("```text")
                lines.append(str(f["answer"]))
                lines.append("```")
            if f.get("response"):
                lines.append("- 响应（已脱敏）：")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(f["response"], ensure_ascii=False, indent=2))
                lines.append("```")
            if f.get("trace"):
                lines.append(f"- `trace`：`{json.dumps(f['trace'], ensure_ascii=False)}`")
            if f.get("citations"):
                cite_list = [
                    f"{c.get('document', '')} 第 {c.get('clause', '')} 条" for c in f["citations"]
                ]
                lines.append(f"- `citations`：{', '.join(cite_list)}")
            if f.get("audit"):
                lines.append(f"- 相关审计记录（{len(f['audit'])} 条）：")
                for rec in f["audit"]:
                    lines.append(
                        f"  - `#{rec.get('id')}` `{rec.get('event_type')}` / "
                        f"`{rec.get('channel')}` / `{rec.get('tool')}` · "
                        f"actor=`{rec.get('actor_sub')}` · outcome=`{rec.get('outcome')}` · "
                        f"status=`{rec.get('status_code')}` · error=`{rec.get('error_code')}` · "
                        f"order=`{rec.get('order_id')}` · approval=`{rec.get('approval_id')}` · "
                        f"request_id=`{rec.get('request_id')}`"
                    )
            lines.append("")

    # 5. 不稳定用例
    lines.append("## 不稳定用例")
    lines.append("")
    unstable_cases = [c for c in report.get("cases", []) if c.get("stability") == "unstable"]
    if not unstable_cases:
        lines.append("无不稳定用例。")
        lines.append("")
    else:
        lines.append("| 用例 | 标题 | 各轮结果 | 差异断言 |")
        lines.append("|---|---|---|---|")
        for c in unstable_cases:
            round_str = " / ".join(f"R{r['round']}:{r['status']}" for r in c.get("runs", []))
            diff_asserts = sorted(
                {f"{f['step']}:{f['assertion']}" for f in failures if f["case"] == c["id"]}
            )
            lines.append(
                f"| {c['id']} | {c.get('title', '')} | {round_str} | {', '.join(diff_asserts)} |"
            )
        lines.append("")

    # 6. 需人工复核
    lines.append("## 需人工复核")
    lines.append("")
    if not manual_review:
        lines.append("无需要人工复核的回答。")
        lines.append("")
    else:
        for item in manual_review:
            lines.append(
                f"- **{item['case']}** 第 {item['round']} 轮 `{item['step']}` "
                f"（期望 `{item['expected_verdict']}`，来源 `{item.get('verdict_source')}`）："
                f"问题「{item['query']}」→ 回答：{item['answer']}"
            )
        lines.append("")

    # 7. 审计对账
    lines.append("## 审计对账")
    lines.append("")
    lines.append(
        f"- 台账应审计事件：{audit_sum.get('expected', m3['total'])} · "
        f"匹配：{audit_sum.get('matched', m3['passed'])} · "
        f"多余记录：{audit_sum.get('unexpected', m3['unexpected'])}"
    )
    denied_cmp = audit_sum.get("access_denied_by_code") or {}
    if denied_cmp:
        lines.append(
            f"- 越权拒绝按 `error_code` 对照：`{json.dumps(denied_cmp, ensure_ascii=False)}`"
        )
    chains = audit_sum.get("approval_chains") or []
    passed_chains = sum(1 for c in chains if c.get("passed"))
    lines.append(f"- 审批链核对数：{len(chains)}（通过 {passed_chains}/{len(chains)}）")
    if chains:
        lines.append("")
        lines.append(
            "| 审批单 | 申请人 | 审批人 | 退款单号 | 重放状态 | 实际状态 | 事件数 | 核对 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for c in chains:
            mark = "✅" if c.get("passed") else "❌"
            lines.append(
                f"| `{c.get('approval_id', '-')}` "
                f"| `{c.get('applicant_sub') or '-'}` "
                f"| `{c.get('approver_sub') or '-'}` "
                f"| `{c.get('refund_id') or '-'}` "
                f"| `{c.get('replayed_status') or '-'}` "
                f"| `{c.get('actual_status') or '-'}` "
                f"| {c.get('event_count', 0)} | {mark} |"
            )
        lines.append("")
    discrepancies = audit_sum.get("discrepancies") or []
    if discrepancies:
        lines.append("- 差异明细：")
        for d in discrepancies:
            lines.append(f"  - [{d.get('rule')}] {d.get('case', '')}: {d.get('message', '')}")
    else:
        lines.append("- 差异明细：无")
    lines.append("")

    # 8. 环境与模型
    lines.append("## 环境与模型")
    lines.append("")
    models = env.get("models") or {}
    wf_nodes = models.get("workflow_nodes") or {}
    if wf_nodes:
        lines.append("| 节点 | 供应商 | 模型 | 参数 |")
        lines.append("|---|---|---|---|")
        for nid, cfg in wf_nodes.items():
            lines.append(
                f"| `{nid}` | `{cfg.get('provider')}` | `{cfg.get('name')}` "
                f"| `{json.dumps(cfg.get('completion_params') or {}, ensure_ascii=False)}` |"
            )
        lines.append("")
    kb_models = models.get("knowledge_base") or {}
    if kb_models:
        lines.append(f"- 知识库模型配置：`{json.dumps(kb_models, ensure_ascii=False)}`")
    judge_info = models.get("judge") or {}
    lines.append(f"- 评审配置：`{json.dumps(judge_info, ensure_ascii=False)}`")
    endpoints = env.get("endpoints") or {}
    lines.append(
        f"- 入口地址：chat=`{endpoints.get('chat_url')}` · "
        f"mcp=`{endpoints.get('mcp_url')}` · guard=`{endpoints.get('guard_url')}`"
    )
    if env.get("git_commit"):
        lines.append(f"- Git 提交：`{env['git_commit']}`")
    lines.append(f"- 运行平台：`{env.get('os')}` (Python {env.get('python_version')})")
    if env.get("note"):
        lines.append(f"- 补充说明：{env['note']}")
    hashes = env.get("hashes") or {}
    if hashes:
        lines.append("- 文件 SHA-256：")
        for k, v in hashes.items():
            lines.append(f"  - `{k}`: `{v or 'N/A'}`")
    lines.append("")

    # 9. 成本与耗时
    lines.append("## 成本与耗时")
    lines.append("")
    latency = cost.get("chat_latency_ms") or sub.get("chat_latency_ms") or {}
    lines.append(f"- 对话轮次（进入工作流）：{cost.get('chat_turns', 0)}")
    if "router_calls_estimated" in cost and "main_model_calls_estimated" in cost:
        upper = cost.get("model_calls_upper_bound", cost.get("chat_turns", 0) * 3)
        lines.append(
            f"- 估算模型调用数：{cost.get('model_calls_estimated', 0)}"
            f"（口径：每轮路由 2 次共 {cost['router_calls_estimated']} 次 + "
            f"实际到达主模型节点 {cost['main_model_calls_estimated']} 次；3× 上限 {upper} 次）"
        )
    else:
        lines.append(
            f"- 估算模型调用数：{cost.get('model_calls_estimated', 0)}（估算口径：按 5.7.2 口径）"
        )
    j_cal_calls = cost.get("judge_calibration_calls", 0)
    j_calls = cost.get("judge_calls", 0)
    j_hits = cost.get("judge_cache_hits", 0)
    j_inv = cost.get("judge_invalid", 0)
    j_unavail = cost.get(
        "judge_unavailable_steps",
        (sub.get("verdict_accuracy") or {}).get("judge_unavailable_steps", 0),
    )
    lines.append(f"- LLM 评审校准调用数：{j_cal_calls}")
    lines.append(
        f"- LLM 评审判定调用数：{j_calls}"
        f"（缓存命中 {j_hits}，作废 {j_inv}，评审不可用受影响步骤数 {j_unavail}）"
    )
    lines.append(f"- 环境重置次数：{cost.get('resets', 0)}")
    lines.append(f"- 总耗时：{run.get('duration_seconds', 0)} 秒")
    by_layer_dur = cost.get("by_layer_duration_seconds") or {}
    if by_layer_dur:
        dur_str = " · ".join(f"{k}={v}s" for k, v in by_layer_dur.items())
        lines.append(f"- 各层耗时：{dur_str}")
    lines.append(
        f"- 单轮对话耗时：中位数 {latency.get('median', 0)} ms，最大 {latency.get('max', 0)} ms"
    )
    lines.append("")

    return "\n".join(lines)


def write_reports(
    run_dir: Path,
    report: dict[str, Any],
    *,
    issued_tokens: Sequence[str] = (),
) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_report = redact_tokens(report, issued_tokens)
    json_path = run_dir / "report.json"
    md_path = run_dir / "report.md"
    json_path.write_text(
        json.dumps(safe_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_text = redact_tokens(render_report_md(safe_report), issued_tokens)
    md_path.write_text(md_text, encoding="utf-8")
    return json_path, md_path


@dataclass
class ComparisonResult:
    metric_deltas: dict[str, dict[str, Any]]
    sub_metric_deltas: dict[str, dict[str, Any]]
    regressed_cases: list[dict[str, Any]] = field(default_factory=list)
    improved_cases: list[dict[str, Any]] = field(default_factory=list)
    hash_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_verdict: str = "pass"


def _num_delta(curr: float | None, base: float | None) -> float | None:
    if curr is None or base is None:
        return None
    return round(curr - base, 4)


def compare_reports(current: dict[str, Any], baseline: dict[str, Any]) -> ComparisonResult:
    """Compare current report.json against baseline report.json per 5.12.4."""
    metric_deltas: dict[str, dict[str, Any]] = {}
    for key in ("authz_block_rate", "approval_compliance", "audit_completeness", "answer_accuracy"):
        c_val = (current.get("metrics", {}).get(key) or {}).get("value")
        b_val = (baseline.get("metrics", {}).get(key) or {}).get("value")
        metric_deltas[key] = {
            "baseline": b_val,
            "current": c_val,
            "delta": _num_delta(c_val, b_val),
        }

    sub_deltas: dict[str, dict[str, Any]] = {}
    c_subs = current.get("breakdown", {}).get("sub_metrics") or {}
    b_subs = baseline.get("breakdown", {}).get("sub_metrics") or {}
    for key in (
        "retrieval_hit_rate",
        "citation_accuracy",
        "verdict_accuracy",
        "refusal_compliance_rate",
        "injection_pass_rate",
        "overall_case_pass_rate",
    ):
        c_val = (c_subs.get(key) or {}).get("value")
        b_val = (b_subs.get(key) or {}).get("value")
        sub_deltas[key] = {
            "baseline": b_val,
            "current": c_val,
            "delta": _num_delta(c_val, b_val),
        }

    base_cases = {c["id"]: c.get("stability") for c in baseline.get("cases", [])}
    curr_cases = {c["id"]: c.get("stability") for c in current.get("cases", [])}
    regressed: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    for cid in sorted(set(base_cases) & set(curr_cases)):
        b_st = str(base_cases[cid])
        c_st = str(curr_cases[cid])
        if b_st == c_st:
            continue
        if STABILITY_RANK.get(c_st, 0) < STABILITY_RANK.get(b_st, 0):
            regressed.append({"id": cid, "from": b_st, "to": c_st})
        elif STABILITY_RANK.get(c_st, 0) > STABILITY_RANK.get(b_st, 0):
            improved.append({"id": cid, "from": b_st, "to": c_st})

    c_hashes = current.get("environment", {}).get("hashes") or {}
    b_hashes = baseline.get("environment", {}).get("hashes") or {}
    hash_changes = {
        k: {"baseline": b_hashes.get(k), "current": c_hashes.get(k)}
        for k in sorted(set(c_hashes) | set(b_hashes))
        if c_hashes.get(k) != b_hashes.get(k)
    }

    c_models = current.get("environment", {}).get("models") or {}
    b_models = baseline.get("environment", {}).get("models") or {}
    model_changes = {
        k: {"baseline": b_models.get(k), "current": c_models.get(k)}
        for k in sorted(set(c_models) | set(b_models))
        if c_models.get(k) != b_models.get(k)
    }

    return ComparisonResult(
        metric_deltas=metric_deltas,
        sub_metric_deltas=sub_deltas,
        regressed_cases=regressed,
        improved_cases=improved,
        hash_changes=hash_changes,
        model_changes=model_changes,
        current_verdict=str(current.get("verdict") or "fail"),
    )


def format_comparison(comp: ComparisonResult) -> str:
    lines: list[str] = ["## 基线比较结果", ""]
    lines.append("### 核心指标差值")
    for k, v in comp.metric_deltas.items():
        delta = v["delta"]
        delta_str = "N/A" if delta is None else f"{delta * 100:+.2f}pp"
        lines.append(
            f"- {k}: 基线 {_fmt_pct(v['baseline'])} → 当前 {_fmt_pct(v['current'])} ({delta_str})"
        )
    lines.append("")
    lines.append("### 分项差值")
    for k, v in comp.sub_metric_deltas.items():
        delta = v["delta"]
        delta_str = "N/A" if delta is None else f"{delta * 100:+.2f}pp"
        lines.append(
            f"- {k}: 基线 {_fmt_pct(v['baseline'])} → 当前 {_fmt_pct(v['current'])} ({delta_str})"
        )
    lines.append("")
    if comp.regressed_cases:
        lines.append("### [提示] 状态变差的用例")
        for item in comp.regressed_cases:
            lines.append(f"- {item['id']}: {item['from']} → {item['to']}")
        lines.append("")
    else:
        lines.append("- 状态变差的用例：无")
    if comp.improved_cases:
        lines.append("### 状态变好的用例")
        for item in comp.improved_cases:
            lines.append(f"- {item['id']}: {item['from']} → {item['to']}")
        lines.append("")
    else:
        lines.append("- 状态变好的用例：无")
    if comp.model_changes:
        lines.append(f"- 模型变化：{', '.join(comp.model_changes.keys())}")
    else:
        lines.append("- 模型变化：无")
    if comp.hash_changes:
        lines.append(f"- 文件哈希变化：{', '.join(comp.hash_changes.keys())}")
    else:
        lines.append("- 文件哈希变化：无")
    return "\n".join(lines)
