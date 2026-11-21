from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from check_dsl import (
    CODE_DIR,
    CODE_NODE_FILES,
    DEFAULT_MANIFEST,
    DEFAULT_OPENAPI,
    DEFAULT_TEMPLATE,
    REPO_ROOT,
    WORKFLOWS_DIR,
    check_dsl,
    dump_dsl_yaml,
    render_code_file,
)

DEFAULT_KB_LOCK = REPO_ROOT / "knowledge-base/kb.lock.json"
DEFAULT_KB_ENV = REPO_ROOT / "knowledge-base/.env"
DEFAULT_WORKFLOWS_ENV = WORKFLOWS_DIR / ".env"
DEFAULT_OUTPUT = WORKFLOWS_DIR / "build/aftersale.dsl.yml"
DEFAULT_MCP_URL = "http://higress:8080/aftersale/mcp"


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


def _get_var(name: str, env_file: dict[str, str], default: str = "") -> str:
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    if name in env_file and env_file[name] != "":
        return env_file[name]
    return default


def render_dsl_data(
    template: dict[str, Any],
    *,
    kb_lock: dict[str, str],
    llm_provider: str,
    llm_model: str,
    router_provider: str = "",
    router_model: str = "",
    mcp_url: str = DEFAULT_MCP_URL,
    rerank_provider: str = "",
    rerank_model: str = "",
    code_dir: Path = CODE_DIR,
) -> dict[str, Any]:
    if not llm_provider or not llm_model:
        raise ValueError("必须提供 WORKFLOWS_LLM_PROVIDER 与 WORKFLOWS_LLM_MODEL")
    if "policy" not in kb_lock or "handbook" not in kb_lock:
        raise ValueError("kb.lock.json 必须包含 'policy' 与 'handbook' 两个知识库 ID")

    eff_router_provider = router_provider or llm_provider
    eff_router_model = router_model or llm_model
    rerank_enabled = bool(rerank_model.strip())
    top_k = 6 if rerank_enabled else 8

    doc = copy.deepcopy(template)
    wf = doc["workflow"]

    for env_var in wf.get("environment_variables", []):
        if isinstance(env_var, dict) and env_var.get("value") == "__MCP_URL__":
            env_var["value"] = mcp_url

    dataset_map = {
        "__DATASET_POLICY__": kb_lock["policy"],
        "__DATASET_HANDBOOK__": kb_lock["handbook"],
    }
    model_map = {
        "__LLM_PROVIDER__": llm_provider,
        "__LLM_MODEL__": llm_model,
        "__ROUTER_PROVIDER__": eff_router_provider,
        "__ROUTER_MODEL__": eff_router_model,
    }

    for node in wf.get("graph", {}).get("nodes", []):
        data = node.get("data", {})
        ntype = data.get("type")

        if ntype == "knowledge-retrieval":
            data["dataset_ids"] = [dataset_map.get(x, x) for x in data.get("dataset_ids", [])]
            mrc = data.get("multiple_retrieval_config")
            if isinstance(mrc, dict):
                mrc["top_k"] = top_k
                mrc["reranking_enable"] = rerank_enabled
                mrc["reranking_mode"] = "reranking_model"
                mrc["reranking_model"] = {
                    "provider": rerank_provider if rerank_enabled else "",
                    "model": rerank_model if rerank_enabled else "",
                }

        model_cfg = data.get("model")
        if isinstance(model_cfg, dict):
            if model_cfg.get("provider") in model_map:
                model_cfg["provider"] = model_map[model_cfg["provider"]]
            if model_cfg.get("name") in model_map:
                model_cfg["name"] = model_map[model_cfg["name"]]

        if ntype == "code":
            code_val = str(data.get("code") or "")
            if code_val.startswith("__CODE:") and code_val.endswith("__"):
                fname = code_val[len("__CODE:") : -2]
                data["code"] = render_code_file(fname, code_dir=code_dir)
            elif node.get("id") in CODE_NODE_FILES:
                data["code"] = render_code_file(CODE_NODE_FILES[node["id"]], code_dir=code_dir)

    return doc


def render_dsl_file(
    *,
    template_path: Path = DEFAULT_TEMPLATE,
    kb_lock_path: Path = DEFAULT_KB_LOCK,
    output_path: Path = DEFAULT_OUTPUT,
    workflows_env_path: Path = DEFAULT_WORKFLOWS_ENV,
    kb_env_path: Path = DEFAULT_KB_ENV,
    manifest_path: Path = DEFAULT_MANIFEST,
    openapi_path: Path = DEFAULT_OPENAPI,
    code_dir: Path = CODE_DIR,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    router_provider: str | None = None,
    router_model: str | None = None,
    mcp_url: str | None = None,
    rerank_provider: str | None = None,
    rerank_model: str | None = None,
) -> Path:
    if not kb_lock_path.exists():
        raise FileNotFoundError(
            f"找不到 {kb_lock_path}，请先在 knowledge-base/ 运行 scripts/sync.py 生成知识库锁定文件"
        )

    wf_env = load_dotenv(workflows_env_path)
    kb_env = load_dotenv(kb_env_path)
    kb_lock = json.loads(kb_lock_path.read_text(encoding="utf-8"))
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))

    rendered = render_dsl_data(
        template,
        kb_lock=kb_lock,
        llm_provider=llm_provider
        if llm_provider is not None
        else _get_var("WORKFLOWS_LLM_PROVIDER", wf_env),
        llm_model=llm_model if llm_model is not None else _get_var("WORKFLOWS_LLM_MODEL", wf_env),
        router_provider=router_provider
        if router_provider is not None
        else _get_var("WORKFLOWS_ROUTER_PROVIDER", wf_env),
        router_model=router_model
        if router_model is not None
        else _get_var("WORKFLOWS_ROUTER_MODEL", wf_env),
        mcp_url=mcp_url
        if mcp_url is not None
        else _get_var("WORKFLOWS_MCP_URL", wf_env, DEFAULT_MCP_URL),
        rerank_provider=rerank_provider
        if rerank_provider is not None
        else _get_var("KB_RERANK_PROVIDER", kb_env),
        rerank_model=rerank_model
        if rerank_model is not None
        else _get_var("KB_RERANK_MODEL", kb_env),
        code_dir=code_dir,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dump_dsl_yaml(rendered), encoding="utf-8")

    errors = check_dsl(
        output_path,
        mode="rendered",
        manifest_path=manifest_path,
        openapi_path=openapi_path,
        code_dir=code_dir,
    )
    if errors:
        detail = "\n".join(f"  - {e}" for e in errors)
        raise RuntimeError(f"渲染产物未通过静态检查:\n{detail}")

    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将 DSL 模板渲染为可导入 Dify 的 YAML 文件")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--kb-lock", type=Path, default=DEFAULT_KB_LOCK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workflows-env", type=Path, default=DEFAULT_WORKFLOWS_ENV)
    parser.add_argument("--kb-env", type=Path, default=DEFAULT_KB_ENV)
    args = parser.parse_args(argv)

    try:
        out = render_dsl_file(
            template_path=args.template,
            kb_lock_path=args.kb_lock,
            output_path=args.output,
            workflows_env_path=args.workflows_env,
            kb_env_path=args.kb_env,
        )
    except Exception as exc:
        print(f"渲染失败: {exc}", file=sys.stderr)
        return 1

    print(f"已渲染并校验通过: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
