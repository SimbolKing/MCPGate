"""CLI entry point: `python -m harness <lint|preflight|run|score|compare|judge-check>` (5.11.1)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness.clients import ChatClient, GuardClient, McpClient
from harness.config import EVALS_DIR, REPO_ROOT, Settings
from harness.environment import EnvironmentManager
from harness.judge import VerdictJudge
from harness.lint import run_lint
from harness.loader import (
    filter_cases,
    load_calibration,
    load_cases,
    load_suite,
    load_tools_manifest,
)
from harness.report import compare_reports, format_comparison
from harness.runner import EvalRunner, compute_suite_hash, rescore_run_dir
from harness.schema import LAYER_VALUES
from harness.tokens import TokenProvider

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ENV_ERROR = 2
EXIT_USAGE_OR_LINT = 3


def _split_csv_args(values: Sequence[str] | None) -> list[str] | None:
    if not values:
        return None
    out: list[str] = []
    for item in values:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                out.append(cleaned)
    return out or None


def _verdict_to_exit_code(verdict: str) -> int:
    if verdict == "pass":
        return EXIT_PASS
    if verdict == "incomplete":
        return EXIT_ENV_ERROR
    return EXIT_FAIL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness",
        description="MCPGate 评测命令行工具（第 5 章）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # lint
    sub.add_parser("lint", help="静态一致性检查 L1–L9（不需要启动任何服务）")

    # preflight
    p_pre = sub.add_parser("preflight", help="环境自检 P1–P6（--full 含 P7）")
    p_pre.add_argument("--full", action="store_true", help="包含 workflows/scripts/e2e.py (P7)")
    p_pre.add_argument(
        "--layer",
        action="append",
        help="要自检的层（逗号分隔或重复传入，默认 tool,guard,dialogue）",
    )

    # run
    p_run = sub.add_parser("run", help="执行评测并生成报告")
    p_run.add_argument(
        "--layer",
        action="append",
        help="要跑的层（逗号分隔，默认 tool,guard,dialogue）",
    )
    p_run.add_argument("--category", action="append", help="按类别键筛选（可多值）")
    p_run.add_argument("--case", action="append", help="按用例 ID 筛选（可多值）")
    p_run.add_argument("--repeats", type=int, default=None, help="对话层重复轮数 (1–5)")
    p_run.add_argument(
        "--preflight",
        choices=("quick", "full", "skip"),
        default="quick",
        help="评测前自检模式（默认 quick）",
    )
    p_run.add_argument(
        "--judge",
        choices=("auto", "off"),
        default="auto",
        help="LLM 评审模式（默认 auto）",
    )
    p_run.add_argument("--label", default="", help="追加到运行目录名的标签")
    p_run.add_argument("--note", default="", help="写入报告的环境补充说明")
    p_run.add_argument("--resume", default=None, help="续跑指定的运行目录")
    p_run.add_argument("--keep-state", action="store_true", help="结束时不重置环境")

    # score
    p_score = sub.add_parser("score", help="对已保存的运行目录离线重评分")
    p_score.add_argument("run_dir", help="运行目录路径")
    p_score.add_argument("--judge", choices=("auto", "off"), default="auto")

    # compare
    p_cmp = sub.add_parser("compare", help="与基线报告比较")
    p_cmp.add_argument("run_dir", help="当前运行目录或 report.json 路径")
    p_cmp.add_argument(
        "--against",
        default=None,
        help="基线目录或 report.json 路径（默认 evals/reports/baseline/）",
    )

    # judge-check
    sub.add_parser("judge-check", help="仅运行 LLM 评审校准集")

    return parser


def _cmd_lint(evals_dir: Path, repo_root: Path) -> int:
    issues = run_lint(evals_dir=evals_dir, repo_root=repo_root)
    if issues:
        for issue in issues:
            print(issue.format(), file=sys.stderr)
        print(f"lint 失败：共 {len(issues)} 项问题。", file=sys.stderr)
        return EXIT_USAGE_OR_LINT
    cases = load_cases(evals_dir)
    print(f"lint 通过：共校验 {len(cases)} 条用例，L1–L9 全部满足。")
    return EXIT_PASS


def _init_judge(
    settings: Settings,
    mode: str,
    evals_dir: Path,
) -> VerdictJudge | None:
    if mode == "off" or not settings.judge_configured:
        return None
    judge = VerdictJudge(settings, evals_dir=evals_dir)
    samples = load_calibration(evals_dir)
    cal = judge.calibrate(samples, use_cache=False)
    if not cal.passed:
        print(
            f"[warn] 评审校准集未全部通过 ({cal.correct}/{cal.total})，本次运行停用 LLM 评审。",
            file=sys.stderr,
        )
    return judge


def _cmd_judge_check(settings: Settings, evals_dir: Path) -> int:
    if not settings.judge_configured:
        print(
            "未配置 EVALS_JUDGE_BASE_URL / EVALS_JUDGE_API_KEY / EVALS_JUDGE_MODEL。",
            file=sys.stderr,
        )
        return EXIT_ENV_ERROR
    judge = VerdictJudge(settings, evals_dir=evals_dir)
    try:
        samples = load_calibration(evals_dir)
        cal = judge.calibrate(samples, use_cache=False)
    finally:
        judge.close()
    for item in cal.items:
        mark = "PASS" if item.passed else "FAIL"
        extra = f" ({item.error})" if item.error else ""
        print(
            f"[{mark}] {item.id}: expected={item.expected_label} "
            f"predicted={item.predicted_label}{extra}"
        )
    print(f"校准结果：{cal.correct}/{cal.total}")
    return EXIT_PASS if cal.passed else EXIT_FAIL


def _cmd_preflight(
    args: argparse.Namespace,
    settings: Settings,
    evals_dir: Path,
    repo_root: Path,
) -> int:
    layers = _split_csv_args(args.layer) or list(LAYER_VALUES)
    unknown = [x for x in layers if x not in LAYER_VALUES]
    if unknown:
        print(f"未知的 layer: {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE_OR_LINT

    suite = load_suite(evals_dir)
    manifest = load_tools_manifest(repo_root)
    write_tools = {name for name, item in manifest.items() if item.get("write")}

    mcp = McpClient(
        settings.mcp_url, write_tools=write_tools, timeout=settings.http_timeout_seconds
    )
    guard = GuardClient(settings.guard_url, timeout=settings.http_timeout_seconds)
    chat = ChatClient(settings.chat_url, timeout=settings.chat_timeout_seconds)
    tokens = TokenProvider(settings, repo_root=repo_root)
    try:
        env = EnvironmentManager(
            settings,
            suite,
            mcp_client=mcp,
            guard_client=guard,
            chat_client=chat,
            token_provider=tokens,
            repo_root=repo_root,
        )
        rep = env.run_preflight(mode="full" if args.full else "quick", layers=set(layers))
    finally:
        mcp.close()
        guard.close()
        chat.close()

    for item in rep.items:
        mark = "PASS" if item.passed else "FAIL"
        print(f"[{mark}] {item.id} {item.name}: {item.detail}")
    return EXIT_PASS if rep.passed else EXIT_ENV_ERROR


def _resolve_resume_scope(
    resume_path: Path,
    *,
    cli_layers: list[str] | None,
    cli_categories: list[str] | None,
    cli_case_ids: list[str] | None,
    cli_repeats: int | None,
    default_repeats: int,
    evals_dir: Path,
) -> tuple[list[str], list[str] | None, list[str] | None, int]:
    """Load original run scope from run_meta.json and reject conflicting CLI overrides (L7-1)."""
    if not resume_path.is_dir():
        raise ValueError(f"--resume 指定的运行目录不存在: {resume_path}")
    meta_path = resume_path / "run_meta.json"
    if not meta_path.is_file():
        raise ValueError(f"--resume 运行目录缺少 run_meta.json: {resume_path}")
    try:
        prev_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"读取 run_meta.json 失败: {exc}") from exc
    if not isinstance(prev_meta, dict) or not isinstance(prev_meta.get("run"), dict):
        raise ValueError(f"run_meta.json 缺少有效的 run 元数据: {meta_path}")

    orig_run = prev_meta["run"]
    orig_layers = orig_run.get("layers")
    orig_categories = orig_run.get("categories")
    orig_cases = orig_run.get("cases")
    orig_repeats = orig_run.get("repeats")
    orig_suite_hash = orig_run.get("suite_hash")

    current_hash = compute_suite_hash(evals_dir)
    if orig_suite_hash and orig_suite_hash != current_hash:
        raise ValueError("--resume 与原运行的套件内容不一致（suite_hash 不匹配），请开启新运行")

    if cli_layers is not None and orig_layers is not None and set(cli_layers) != set(orig_layers):
        raise ValueError(f"--resume 与原运行的 --layer 冲突: 原={orig_layers}，本次={cli_layers}")
    if cli_categories is not None and set(cli_categories) != set(orig_categories or []):
        raise ValueError(
            f"--resume 与原运行的 --category 冲突: 原={orig_categories}，本次={cli_categories}"
        )
    if cli_case_ids is not None and set(cli_case_ids) != set(orig_cases or []):
        raise ValueError(f"--resume 与原运行的 --case 冲突: 原={orig_cases}，本次={cli_case_ids}")
    if cli_repeats is not None and orig_repeats is not None and cli_repeats != int(orig_repeats):
        raise ValueError(
            f"--resume 与原运行的 --repeats 冲突: 原={orig_repeats}，本次={cli_repeats}"
        )

    layers = list(orig_layers) if orig_layers is not None else (cli_layers or list(LAYER_VALUES))
    categories = list(orig_categories) if orig_categories is not None else cli_categories
    case_ids = list(orig_cases) if orig_cases is not None else cli_case_ids
    repeats = (
        int(orig_repeats)
        if orig_repeats is not None
        else (cli_repeats if cli_repeats is not None else default_repeats)
    )
    return layers, categories, case_ids, repeats


def _cmd_run(
    args: argparse.Namespace,
    settings: Settings,
    evals_dir: Path,
    repo_root: Path,
) -> int:
    suite = load_suite(evals_dir)
    all_cases = [c for _, c in load_cases(evals_dir)]

    cli_layers = _split_csv_args(args.layer)
    cli_categories = _split_csv_args(args.category)
    cli_case_ids = _split_csv_args(args.case)
    cli_repeats = args.repeats

    resume_path: Path | None = None
    if args.resume:
        resume_path = Path(args.resume)
        try:
            layers, categories, case_ids, repeats = _resolve_resume_scope(
                resume_path,
                cli_layers=cli_layers,
                cli_categories=cli_categories,
                cli_case_ids=cli_case_ids,
                cli_repeats=cli_repeats,
                default_repeats=suite.repeats,
                evals_dir=evals_dir,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE_OR_LINT
    else:
        layers = cli_layers or list(LAYER_VALUES)
        categories = cli_categories
        case_ids = cli_case_ids
        repeats = cli_repeats if cli_repeats is not None else suite.repeats

    if not (1 <= repeats <= 5):
        print(f"--repeats 必须在 1–5 之间，实际: {repeats}", file=sys.stderr)
        return EXIT_USAGE_OR_LINT

    try:
        selected = filter_cases(
            all_cases,
            layers=layers,
            categories=categories,
            case_ids=case_ids,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_OR_LINT

    if not selected:
        print("筛选后没有任何用例可执行。", file=sys.stderr)
        return EXIT_USAGE_OR_LINT

    is_full_suite = (
        set(layers) == set(LAYER_VALUES)
        and categories is None
        and case_ids is None
        and repeats == suite.repeats
    )

    manifest = load_tools_manifest(repo_root)
    write_tools = {name for name, item in manifest.items() if item.get("write")}

    mcp = McpClient(
        settings.mcp_url, write_tools=write_tools, timeout=settings.http_timeout_seconds
    )
    guard = GuardClient(settings.guard_url, timeout=settings.http_timeout_seconds)
    chat = ChatClient(settings.chat_url, timeout=settings.chat_timeout_seconds)
    tokens = TokenProvider(settings, repo_root=repo_root)
    judge: VerdictJudge | None = None

    try:
        env = EnvironmentManager(
            settings,
            suite,
            mcp_client=mcp,
            guard_client=guard,
            chat_client=chat,
            token_provider=tokens,
            repo_root=repo_root,
        )
        if args.preflight != "skip":
            pre = env.run_preflight(mode=args.preflight, layers=set(layers))
            for item in pre.items:
                mark = "PASS" if item.passed else "FAIL"
                print(f"[{mark}] {item.id} {item.name}: {item.detail}")
            if not pre.passed:
                return EXIT_ENV_ERROR

        judge = _init_judge(settings, args.judge, evals_dir)
        runner = EvalRunner(
            settings,
            suite,
            mcp_client=mcp,
            guard_client=guard,
            chat_client=chat,
            token_provider=tokens,
            env_manager=env,
            tools_manifest=manifest,
            judge=judge,
            evals_dir=evals_dir,
            repo_root=repo_root,
        )
        run_dir, report = runner.run(
            selected,
            repeats=repeats,
            layers=layers,
            is_full_suite=is_full_suite,
            categories=categories,
            case_ids=case_ids,
            label=args.label,
            note=args.note,
            keep_state=args.keep_state,
            resume_dir=resume_path,
        )
    finally:
        mcp.close()
        guard.close()
        chat.close()
        if judge is not None:
            judge.close()

    print(f"报告已生成：{run_dir / 'report.md'}")
    print(f"总体结论：{report['verdict']}")
    return _verdict_to_exit_code(report["verdict"])


def _cmd_score(
    args: argparse.Namespace,
    settings: Settings,
    evals_dir: Path,
    repo_root: Path,
) -> int:
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"运行目录不存在: {run_dir}", file=sys.stderr)
        return EXIT_USAGE_OR_LINT
    judge = _init_judge(settings, args.judge, evals_dir)
    try:
        report = rescore_run_dir(
            run_dir,
            settings=settings,
            judge=judge,
            evals_dir=evals_dir,
            repo_root=repo_root,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_OR_LINT
    finally:
        if judge is not None:
            judge.close()

    print(f"重评分完成：{run_dir / 'report.md'}（verdict={report['verdict']}）")
    return _verdict_to_exit_code(report["verdict"])


def _resolve_report_json(path_str: str | Path) -> Path:
    path = Path(path_str)
    if path.is_dir():
        return path / "report.json"
    return path


def _cmd_compare(args: argparse.Namespace, evals_dir: Path) -> int:
    curr_path = _resolve_report_json(args.run_dir)
    base_arg = args.against if args.against else (evals_dir / "reports" / "baseline")
    base_path = _resolve_report_json(base_arg)

    if not curr_path.is_file():
        print(f"找不到当前报告: {curr_path}", file=sys.stderr)
        return EXIT_USAGE_OR_LINT
    if not base_path.is_file():
        print(f"找不到基线报告: {base_path}", file=sys.stderr)
        return EXIT_USAGE_OR_LINT

    try:
        curr_data: dict[str, Any] = json.loads(curr_path.read_text(encoding="utf-8"))
        base_data: dict[str, Any] = json.loads(base_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"读取报告失败: {exc}", file=sys.stderr)
        return EXIT_USAGE_OR_LINT

    comp = compare_reports(curr_data, base_data)
    print(format_comparison(comp))
    return _verdict_to_exit_code(comp.current_verdict)


def main(
    argv: Sequence[str] | None = None,
    *,
    evals_dir: Path = EVALS_DIR,
    repo_root: Path = REPO_ROOT,
    settings: Settings | None = None,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE_OR_LINT
        return EXIT_PASS if code == 0 else EXIT_USAGE_OR_LINT

    cfg = settings or Settings()

    if args.command == "lint":
        return _cmd_lint(evals_dir, repo_root)
    if args.command == "preflight":
        return _cmd_preflight(args, cfg, evals_dir, repo_root)
    if args.command == "run":
        return _cmd_run(args, cfg, evals_dir, repo_root)
    if args.command == "score":
        return _cmd_score(args, cfg, evals_dir, repo_root)
    if args.command == "compare":
        return _cmd_compare(args, evals_dir)
    if args.command == "judge-check":
        return _cmd_judge_check(cfg, evals_dir)
    return EXIT_USAGE_OR_LINT  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
