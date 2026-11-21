from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import yaml
from check_dsl import (
    CODE_DIR,
    CODE_NODE_FILES,
    DATASET_PLACEHOLDERS,
    DEFAULT_MANIFEST,
    DEFAULT_OPENAPI,
    DEFAULT_TEMPLATE,
    MODEL_NODE_PLACEHOLDERS,
    check_dsl,
    dump_dsl_yaml,
    render_code_file,
)


def normalize_dsl_data(
    exported: dict[str, Any],
    *,
    code_dir: Path = CODE_DIR,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize an exported/rendered Dify DSL back into the template representation.

    Returns (normalized_doc, warnings). Raises ValueError on code drift.
    """
    doc = copy.deepcopy(exported)
    warnings: list[str] = []

    wf = doc.get("workflow")
    if not isinstance(wf, dict):
        raise ValueError("DSL 缺少 workflow 段")

    for env_var in wf.get("environment_variables", []):
        if isinstance(env_var, dict) and env_var.get("name") == "MCP_URL":
            env_var["value"] = "__MCP_URL__"

    graph = wf.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("DSL 缺少 workflow.graph 段")

    # Strip canvas viewport noise
    graph.pop("viewport", None)

    for edge in graph.get("edges", []):
        if isinstance(edge, dict):
            edge.pop("selected", None)

    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or "")
        if nid.isdigit():
            warnings.append(f"发现数字 ID 节点 {nid!r}，建议改用可读节点 ID 后再提交模板")

        node.pop("selected", None)
        node.pop("positionAbsolute", None)
        node.pop("dragging", None)

        data = node.get("data")
        if not isinstance(data, dict):
            continue
        data.pop("selected", None)

        if nid in DATASET_PLACEHOLDERS:
            data["dataset_ids"] = list(DATASET_PLACEHOLDERS[nid])
            mrc = data.get("multiple_retrieval_config")
            if isinstance(mrc, dict):
                mrc["top_k"] = "__RERANK_TOP_K__"
                mrc["score_threshold"] = None
                mrc["reranking_enable"] = "__RERANK_ENABLE__"
                mrc["reranking_mode"] = "reranking_model"
                mrc["reranking_model"] = {
                    "provider": "__RERANK_PROVIDER__",
                    "model": "__RERANK_MODEL__",
                }

        if nid in MODEL_NODE_PLACEHOLDERS:
            prov_ph, model_ph = MODEL_NODE_PLACEHOLDERS[nid]
            model_cfg = data.get("model")
            if isinstance(model_cfg, dict):
                model_cfg["provider"] = prov_ph
                model_cfg["name"] = model_ph

        if nid in CODE_NODE_FILES:
            fname = CODE_NODE_FILES[nid]
            actual_code = str(data.get("code") or "")
            expected_code = render_code_file(fname, code_dir=code_dir)
            if actual_code != f"__CODE:{fname}__":
                if actual_code.strip() != expected_code.strip():
                    raise ValueError(
                        f"节点 {nid} 的代码与 workflows/code/{fname} 不一致；"
                        "代码请仅在 workflows/code/ 中修改后再重新渲染"
                    )
            data["code"] = f"__CODE:{fname}__"

    return doc, warnings


def normalize_dsl_file(
    source_path: Path,
    *,
    output_path: Path = DEFAULT_TEMPLATE,
    code_dir: Path = CODE_DIR,
    manifest_path: Path = DEFAULT_MANIFEST,
    openapi_path: Path = DEFAULT_OPENAPI,
) -> list[str]:
    exported = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(exported, dict):
        raise ValueError("输入文件不是合法的 YAML 映射")

    normalized, warnings = normalize_dsl_data(exported, code_dir=code_dir)
    output_path.write_text(dump_dsl_yaml(normalized), encoding="utf-8")

    errors = check_dsl(
        output_path,
        mode="template",
        manifest_path=manifest_path,
        openapi_path=openapi_path,
        code_dir=code_dir,
    )
    if errors:
        detail = "\n".join(f"  - {e}" for e in errors)
        raise RuntimeError(f"归一化结果未通过模板静态检查:\n{detail}")

    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将 Dify 导出的 DSL 归一化回模板文件")
    parser.add_argument("source", type=Path, help="从 Dify 导出的 YAML 文件路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help="输出模板路径（默认覆盖 aftersale.dsl.template.yml）",
    )
    args = parser.parse_args(argv)

    try:
        warnings = normalize_dsl_file(args.source, output_path=args.output)
    except Exception as exc:
        print(f"归一化失败: {exc}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    print(f"已归一化并校验通过: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
