from __future__ import annotations

import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = WORKFLOWS_DIR.parent
CODE_DIR = WORKFLOWS_DIR / "code"
DEFAULT_TEMPLATE = WORKFLOWS_DIR / "aftersale.dsl.template.yml"
DEFAULT_MANIFEST = REPO_ROOT / "gateway/generated/tools.manifest.json"
DEFAULT_OPENAPI = REPO_ROOT / "mock-api/openapi.json"

INLINE_MARKER_RE = re.compile(
    r"# === INLINE:_mcp_common\.py START ===.*?# === INLINE:_mcp_common\.py END ===",
    re.DOTALL,
)

CODE_NODE_FILES: dict[str, str] = {
    "resolve": "resolve_context.py",
    "parse_order": "parse_order.py",
    "refund_args": "build_refund_args.py",
    "parse_refund": "parse_refund.py",
    "parse_query": "parse_query.py",
}

DATASET_PLACEHOLDERS: dict[str, list[str]] = {
    "retrieve_policy": ["__DATASET_POLICY__"],
    "retrieve_kb": ["__DATASET_POLICY__", "__DATASET_HANDBOOK__"],
}

MODEL_NODE_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "extract": ("__ROUTER_PROVIDER__", "__ROUTER_MODEL__"),
    "classify": ("__ROUTER_PROVIDER__", "__ROUTER_MODEL__"),
    "advise": ("__LLM_PROVIDER__", "__LLM_MODEL__"),
    "decide_refund": ("__LLM_PROVIDER__", "__LLM_MODEL__"),
    "answer_kb": ("__LLM_PROVIDER__", "__LLM_MODEL__"),
}

PLACEHOLDER_RE = re.compile(
    r"__(?:DATASET_POLICY|DATASET_HANDBOOK|LLM_PROVIDER|LLM_MODEL|"
    r"ROUTER_PROVIDER|ROUTER_MODEL|MCP_URL|RERANK_ENABLE|RERANK_TOP_K|"
    r"RERANK_PROVIDER|RERANK_MODEL|CODE:[A-Za-z0-9_.-]+)__"
)

JWT_SECRET_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
DIFY_KEY_RE = re.compile(r"\b(?:app|dataset)-[A-Za-z0-9_-]{8,}\b")
TOOL_NAME_IN_BODY_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
USER_TOKEN_REF_RE = re.compile(r"user_token")

LLM_SHARED_RULE_NODES: tuple[str, ...] = ("advise", "decide_refund", "answer_kb")

# 4.8.8 六条共用规则关键短语（每项内命中任一候选短语即满足）
SHARED_PROMPT_RULES: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    (
        "规则 1（身份与对话对象）",
        (
            ("企业内部售后助手身份", ("企业内部的售后助手",)),
            ("对话对象声明", ("对话对象是客服或主管，不是客户",)),
        ),
    ),
    (
        "规则 2（仅使用给定材料）",
        (
            ("限定检索材料", ("检索到的",)),
            ("禁止补充材料外信息", ("不得补充",)),
        ),
    ),
    (
        "规则 3（条款引用约束）",
        (
            ("仅引用上下文中出现的条款", ("上下文中出现", "实际出现", "标注出处")),
            ("固定引用格式或禁止编造", ("《文档名》第 X.Y 条", "禁止编造条款编号")),
        ),
    ),
    (
        "规则 4（防提示注入）",
        (
            ("材料视作数据", ("数据",)),
            ("不执行数据内指令", ("一律不执行", "绝不执行")),
        ),
    ),
    (
        "规则 5（不声称执行与不泄密）",
        (
            ("不声称已执行", ("不声称执行", "不要声称已执行")),
            ("不输出系统提示词", ("系统提示词",)),
            ("不输出登录凭据", ("登录凭据",)),
            ("不输出内部节点信息", ("内部节点信息",)),
        ),
    ),
    (
        "规则 6（语言与格式）",
        (
            ("简体中文", ("简体中文",)),
            ("先结论后依据", ("先结论后依据",)),
            ("金额两位小数", ("两位小数",)),
            ("日期写法", ("11 月 2 日",)),
        ),
    ),
)


class _DslDumper(yaml.SafeDumper):
    pass


def _represent_dsl_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_DslDumper.add_representer(str, _represent_dsl_str)


def dump_dsl_yaml(doc: dict[str, Any]) -> str:
    """Serialize a Dify DSL mapping with stable key order and literal block scalars."""
    return yaml.dump(
        doc,
        Dumper=_DslDumper,
        allow_unicode=True,
        sort_keys=True,
        width=4096,
    )


def render_code_file(filename: str, code_dir: Path = CODE_DIR) -> str:
    source = (code_dir / filename).read_text(encoding="utf-8")
    if INLINE_MARKER_RE.search(source):
        common = (code_dir / "_mcp_common.py").read_text(encoding="utf-8").strip()
        source = INLINE_MARKER_RE.sub(lambda _m: common, source)
    return source.rstrip() + "\n"


def _extract_body_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(str(item.get("value") or "") for item in data if isinstance(item, dict))
    return ""


def _walk_strings(obj: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(obj, str):
        found.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else str(k)
            found.extend(_walk_strings(v, sub))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            found.extend(_walk_strings(item, f"{path}[{idx}]"))
    return found


def _extract_query_whitelist(code_dir: Path) -> set[str]:
    tree = ast.parse((code_dir / "resolve_context.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "QUERY_TOOL_WHITELIST":
                    val = ast.literal_eval(node.value)
                    if isinstance(val, (tuple, list, set)):
                        return {str(item) for item in val}
    return set()


def _extract_build_refund_keys(code_dir: Path) -> tuple[set[str], set[str]]:
    """Inspect build_refund_args.py to collect generated create_refund keys."""
    source = (code_dir / "build_refund_args.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    required_keys: set[str] = set()
    optional_keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "args" and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        required_keys.add(key.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "args":
                    if isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                required_keys.add(key.value)
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "args"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    optional_keys.add(target.slice.value)
    return required_keys, required_keys | optional_keys


def _all_simple_paths(
    adj: dict[str, list[str]],
    start: str,
    target_set: set[str],
) -> list[list[str]]:
    paths: list[list[str]] = []

    def dfs(node: str, path: list[str], visited: set[str]) -> None:
        if node in target_set:
            paths.append(list(path))
            return
        for nxt in adj.get(node, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            path.append(nxt)
            dfs(nxt, path, visited)
            path.pop()
            visited.remove(nxt)

    dfs(start, [start], {start})
    return paths


def check_dsl(
    dsl_path: Path,
    *,
    mode: str = "auto",
    manifest_path: Path = DEFAULT_MANIFEST,
    openapi_path: Path = DEFAULT_OPENAPI,
    code_dir: Path = CODE_DIR,
) -> list[str]:
    """Run static checks D1-D12 on a template or rendered Dify Chatflow DSL."""
    errors: list[str] = []
    raw_text = dsl_path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        return [f"YAML 解析失败: {exc}"]

    if not isinstance(doc, dict):
        return ["DSL 顶层必须是 YAML 映射对象"]

    if mode == "auto":
        is_template = dsl_path.name.endswith(".template.yml") or ".template." in dsl_path.name
    elif mode in ("template", "rendered"):
        is_template = mode == "template"
    else:
        return [f"不支持的检查模式: {mode}"]

    # D1: kind == app, app.mode == advanced-chat
    if doc.get("kind") != "app":
        errors.append(f"D1: kind 必须为 'app'，当前为 {doc.get('kind')!r}")
    app_sec = doc.get("app") if isinstance(doc.get("app"), dict) else {}
    if app_sec.get("mode") != "advanced-chat":
        errors.append(f"D1: app.mode 必须为 'advanced-chat'，当前为 {app_sec.get('mode')!r}")

    wf = doc.get("workflow") if isinstance(doc.get("workflow"), dict) else {}
    graph = wf.get("graph") if isinstance(wf.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []

    nodes_by_id: dict[str, dict[str, Any]] = {}
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get("id"), str):
            nodes_by_id[n["id"]] = n

    # D2: start node has exactly one input variable user_token (paragraph, required, hide)
    start_nodes = [
        n for n in nodes if isinstance(n, dict) and n.get("data", {}).get("type") == "start"
    ]
    if len(start_nodes) != 1:
        errors.append(f"D2: 必须恰好有 1 个 start 节点，当前为 {len(start_nodes)}")
    else:
        start_vars = start_nodes[0].get("data", {}).get("variables") or []
        if len(start_vars) != 1:
            errors.append(f"D2: start 节点必须恰好有 1 个输入变量，当前为 {len(start_vars)}")
        else:
            uv = start_vars[0]
            if (
                uv.get("variable") != "user_token"
                or uv.get("type") != "paragraph"
                or uv.get("required") is not True
                or uv.get("hide") is not True
            ):
                errors.append(
                    "D2: start 节点变量必须为 user_token"
                    "（type=paragraph, required=true, hide=true）"
                )

    # D3: user_token only referenced in http-request authorization.config.api_key
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", ""))
        ndata = n.get("data") if isinstance(n.get("data"), dict) else {}
        ntype = ndata.get("type")
        for path, text in _walk_strings(ndata):
            if not USER_TOKEN_REF_RE.search(text):
                continue
            if ntype == "start" and (path.startswith("variables[0].") or path == "desc"):
                continue
            if ntype == "http-request" and path == "authorization.config.api_key":
                continue
            errors.append(f"D3: 节点 {nid} ({ntype}) 在不允许的位置 {path} 引用了 user_token")

    # D4: No JWT / app- / dataset- secret literals anywhere in file
    if JWT_SECRET_RE.search(raw_text):
        errors.append("D4: DSL 中检测到疑似 JWT 令牌字面量")
    if DIFY_KEY_RE.search(raw_text):
        errors.append("D4: DSL 中检测到疑似 Dify API Key（app-/dataset-）字面量")

    # D5: Every http-request node conforms to 4.8.7
    http_nodes = [
        n for n in nodes if isinstance(n, dict) and n.get("data", {}).get("type") == "http-request"
    ]
    if not http_nodes:
        errors.append("D5: 未找到任何 http-request 节点")
    for n in http_nodes:
        nid = n.get("id")
        d = n.get("data", {})
        if str(d.get("method", "")).lower() != "post":
            errors.append(f"D5: http-request 节点 {nid} 的 method 必须为 post")
        if d.get("url") != "{{#env.MCP_URL#}}":
            errors.append(f"D5: http-request 节点 {nid} 的 url 必须为 '{{{{#env.MCP_URL#}}}}'")
        auth = d.get("authorization") if isinstance(d.get("authorization"), dict) else {}
        auth_cfg = auth.get("config") if isinstance(auth.get("config"), dict) else {}
        if (
            auth.get("type") != "api-key"
            or auth_cfg.get("type") != "bearer"
            or auth_cfg.get("api_key") != "{{#start.user_token#}}"
        ):
            errors.append(
                f"D5: http-request 节点 {nid} 必须使用 Bearer 鉴权并引用 start.user_token"
            )
        headers_str = str(d.get("headers") or "")
        if re.search(r"(?im)^authorization\s*:", headers_str):
            errors.append(f"D5: http-request 节点 {nid} 的 headers 中不得包含 Authorization")
        retry_cfg = d.get("retry_config") if isinstance(d.get("retry_config"), dict) else {}
        if retry_cfg.get("retry_enabled") is not False:
            errors.append(f"D5: http-request 节点 {nid} 必须关闭重试（retry_enabled: false）")
        if d.get("error_strategy"):
            errors.append(f"D5: http-request 节点 {nid} 不得配置 error_strategy")

    # D6: Tools used in workflow align with gateway/generated/tools.manifest.json
    if not manifest_path.exists():
        errors.append(f"D6: 找不到工具清单文件 {manifest_path}")
    else:
        manifest_items = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_by_name = {item["name"]: item for item in manifest_items}
        for n in http_nodes:
            nid = n.get("id")
            body_text = _extract_body_text(n.get("data", {}).get("body"))
            match = TOOL_NAME_IN_BODY_RE.search(body_text)
            if not match:
                errors.append(f"D6: http-request 节点 {nid} 请求体中缺少工具名")
                continue
            tool_name = match.group(1)
            if tool_name.startswith("{{#") and tool_name.endswith("#}}"):
                if nid != "call_query":
                    errors.append(f"D6: 仅 call_query 允许动态工具名，发现于 {nid}")
                continue
            if tool_name not in manifest_by_name:
                errors.append(
                    f"D6: 节点 {nid} 引用的工具 {tool_name!r} 不在 tools.manifest.json 中"
                )
            elif manifest_by_name[tool_name].get("write") is True and nid != "call_create_refund":
                errors.append(
                    f"D6: 写工具 {tool_name!r} 只能出现在 call_create_refund，发现于 {nid}"
                )

        whitelist = _extract_query_whitelist(code_dir)
        if not whitelist:
            errors.append("D6: 无法从 resolve_context.py 读取 QUERY_TOOL_WHITELIST")
        for tool_name in sorted(whitelist):
            if tool_name not in manifest_by_name:
                errors.append(
                    f"D6: resolve_context.py 白名单工具 {tool_name!r} 不在 tools.manifest.json 中"
                )
            elif manifest_by_name[tool_name].get("write") is True:
                errors.append(f"D6: resolve_context.py 白名单中不允许包含写工具 {tool_name!r}")

    # D7: build_refund_args.py keys subset of OpenAPI create_refund requestBody
    if not openapi_path.exists():
        errors.append(f"D7: 找不到 OpenAPI 契约文件 {openapi_path}")
    else:
        openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
        schema = (
            openapi.get("paths", {})
            .get("/api/v1/refunds", {})
            .get("post", {})
            .get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        allowed_props = set((schema.get("properties") or {}).keys())
        openapi_required = set(schema.get("required") or [])
        gen_required, gen_all = _extract_build_refund_keys(code_dir)
        extra = gen_all - allowed_props
        missing = openapi_required - gen_required
        if extra:
            errors.append(f"D7: build_refund_args.py 生成了 OpenAPI 未定义的字段: {sorted(extra)}")
        if missing:
            errors.append(
                f"D7: build_refund_args.py 缺少 create_refund 必填字段: {sorted(missing)}"
            )

    # Build adjacency graph for D8 and D9
    adj: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if isinstance(edge, dict) and edge.get("source") and edge.get("target"):
            adj[str(edge["source"])].append(str(edge["target"]))

    # D8: Every path from start to call_create_refund passes call_get_order, precheck_ok, can_submit
    if "start" not in nodes_by_id or "call_create_refund" not in nodes_by_id:
        errors.append("D8: 缺少 start 或 call_create_refund 节点")
    else:
        refund_paths = _all_simple_paths(adj, "start", {"call_create_refund"})
        if not refund_paths:
            errors.append("D8: 从 start 到 call_create_refund 不存在可达路径")
        for path in refund_paths:
            for req_node in ("call_get_order", "precheck_ok", "can_submit"):
                if req_node not in path:
                    chain = " -> ".join(path)
                    errors.append(
                        f"D8: 存在绕过 {req_node} 到达 call_create_refund 的路径: {chain}"
                    )

    # D9: Every path from start to any answer node has at most 1 LLM node with context enabled
    answer_ids = {
        nid for nid, n in nodes_by_id.items() if n.get("data", {}).get("type") == "answer"
    }
    if "start" in nodes_by_id and answer_ids:
        for path in _all_simple_paths(adj, "start", answer_ids):
            ctx_llms = [
                nid
                for nid in path
                if nodes_by_id[nid].get("data", {}).get("type") == "llm"
                and (nodes_by_id[nid].get("data", {}).get("context") or {}).get("enabled") is True
            ]
            if len(ctx_llms) > 1:
                chain = " -> ".join(path)
                errors.append(f"D9: 路径 {chain} 上启用了 context 的 LLM 节点超过 1 个: {ctx_llms}")

    # D10: features.retriever_resource.enabled == true; is_refund matches classifier class name
    features = wf.get("features") if isinstance(wf.get("features"), dict) else {}
    rr = (
        features.get("retriever_resource")
        if isinstance(features.get("retriever_resource"), dict)
        else {}
    )
    if rr.get("enabled") is not True:
        errors.append("D10: workflow.features.retriever_resource.enabled 必须为 true")

    classify_node = nodes_by_id.get("classify")
    is_refund_node = nodes_by_id.get("is_refund")
    if not classify_node or not is_refund_node:
        errors.append("D10: 缺少 classify 或 is_refund 节点")
    else:
        class_names = {
            c.get("name")
            for c in (classify_node.get("data", {}).get("classes") or [])
            if isinstance(c, dict)
        }
        cases = is_refund_node.get("data", {}).get("cases") or []
        cond_val = None
        if cases and isinstance(cases[0], dict):
            conds = cases[0].get("conditions") or []
            if conds and isinstance(conds[0], dict):
                cond_val = conds[0].get("value")
        if cond_val not in class_names:
            allowed_names = sorted(str(x) for x in class_names)
            errors.append(
                f"D10: is_refund 比较值 {cond_val!r} 不在 classify 类别名 {allowed_names} 中"
            )

    # D11: Template vs rendered placeholders and code consistency
    if is_template:
        for nid, expected_ids in DATASET_PLACEHOLDERS.items():
            node = nodes_by_id.get(nid)
            actual = node.get("data", {}).get("dataset_ids") if node else None
            if actual != expected_ids:
                errors.append(
                    f"D11: 模板节点 {nid} 的 dataset_ids 应为 {expected_ids}，当前为 {actual}"
                )
        for nid, (exp_prov, exp_model) in MODEL_NODE_PLACEHOLDERS.items():
            node = nodes_by_id.get(nid)
            m = node.get("data", {}).get("model", {}) if node else {}
            if m.get("provider") != exp_prov or m.get("name") != exp_model:
                errors.append(
                    f"D11: 模板节点 {nid} 的模型应为 ({exp_prov}, {exp_model})，"
                    f"当前为 ({m.get('provider')!r}, {m.get('name')!r})"
                )
        for nid, fname in CODE_NODE_FILES.items():
            node = nodes_by_id.get(nid)
            actual_code = node.get("data", {}).get("code") if node else None
            if actual_code != f"__CODE:{fname}__":
                errors.append(
                    f"D11: 模板代码节点 {nid} 应为占位符 '__CODE:{fname}__'，当前为 {actual_code!r}"
                )
    else:
        leftover = sorted(set(PLACEHOLDER_RE.findall(raw_text)))
        if leftover:
            errors.append(f"D11: 渲染产物中残留未替换占位符: {leftover}")
        for nid, fname in CODE_NODE_FILES.items():
            node = nodes_by_id.get(nid)
            actual_code = (node.get("data", {}).get("code") or "") if node else ""
            expected_code = render_code_file(fname, code_dir=code_dir)
            if actual_code.strip() != expected_code.strip():
                errors.append(f"D11: 渲染产物代码节点 {nid} 与 workflows/code/{fname} 不一致")

    # D12: All 3 LLM nodes (advise, decide_refund, answer_kb) include 4.8.8 six shared rules
    for nid in LLM_SHARED_RULE_NODES:
        node = nodes_by_id.get(nid)
        if not node:
            errors.append(f"D12: 缺少 LLM 节点 {nid}")
            continue
        prompts = node.get("data", {}).get("prompt_template") or []
        sys_texts = [
            str(p.get("text") or "")
            for p in prompts
            if isinstance(p, dict) and p.get("role") == "system"
        ]
        if not sys_texts:
            errors.append(f"D12: LLM 节点 {nid} 缺少 system 提示词")
            continue
        sys_prompt = "\n".join(sys_texts)
        for rule_label, checks in SHARED_PROMPT_RULES:
            missing_items = [
                item_label
                for item_label, phrases in checks
                if not any(phrase in sys_prompt for phrase in phrases)
            ]
            if missing_items:
                errors.append(
                    f"D12: LLM 节点 {nid} 的系统提示词缺少 4.8.8 {rule_label} 关键短语: "
                    f"{', '.join(missing_items)}"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = "auto"
    target = DEFAULT_TEMPLATE
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1
    if positional:
        target = Path(positional[0])

    errors = check_dsl(target, mode=mode)
    if errors:
        print(f"DSL 静态检查失败 ({target}):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"DSL 静态检查通过: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
