from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import check_dsl
import normalize_dsl
import pytest
import render_dsl
import yaml

TEMPLATE_PATH = check_dsl.DEFAULT_TEMPLATE


def test_template_passes_all_static_checks() -> None:
    errors = check_dsl.check_dsl(TEMPLATE_PATH, mode="template")
    assert errors == []
    assert check_dsl.main([str(TEMPLATE_PATH)]) == 0


def test_render_check_exec_and_normalize_roundtrip(tmp_path: Path) -> None:
    kb_lock = tmp_path / "kb.lock.json"
    kb_lock.write_text(
        json.dumps(
            {
                "policy": "11111111-1111-4111-8111-111111111111",
                "handbook": "22222222-2222-4222-8222-222222222222",
            }
        ),
        encoding="utf-8",
    )
    rendered_path = tmp_path / "aftersale.dsl.yml"
    render_dsl.render_dsl_file(
        template_path=TEMPLATE_PATH,
        kb_lock_path=kb_lock,
        output_path=rendered_path,
        llm_provider="langgenius/openai/openai",
        llm_model="gpt-4o",
        router_provider="langgenius/openai/openai",
        router_model="gpt-4o-mini",
        mcp_url="http://higress:8080/aftersale/mcp",
        rerank_provider="",
        rerank_model="",
    )

    assert check_dsl.check_dsl(rendered_path, mode="rendered") == []

    # Verify every rendered code node only uses sandbox-safe stdlib (json, re, datetime)
    # and compiles/executes cleanly (Dify sandbox simulation)
    allowed_imports = {"json", "re", "datetime"}
    rendered_doc = yaml.safe_load(rendered_path.read_text(encoding="utf-8"))
    code_nodes = [
        n for n in rendered_doc["workflow"]["graph"]["nodes"] if n["data"]["type"] == "code"
    ]
    assert len(code_nodes) == 5
    for node in code_nodes:
        code_src = node["data"]["code"]
        tree = ast.parse(code_src, feature_version=(3, 8))
        imported: set[str] = set()
        for item in ast.walk(tree):
            if isinstance(item, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in item.names)
            elif isinstance(item, ast.ImportFrom) and item.module:
                imported.add(item.module.split(".")[0])
            # Ensure Python 3.8+ runtime compatibility (no PEP 604 `X | Y` or PEP 585 `list[str]`)
            ann_nodes: list[ast.AST] = []
            if isinstance(item, ast.arg) and item.annotation is not None:
                ann_nodes.append(item.annotation)
            elif isinstance(item, ast.FunctionDef) and item.returns is not None:
                ann_nodes.append(item.returns)
            elif isinstance(item, ast.AnnAssign) and item.annotation is not None:
                ann_nodes.append(item.annotation)
            for ann in ann_nodes:
                for sub in ast.walk(ann):
                    assert not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)), (
                        f"节点 {node['id']} 包含不兼容低版本 Python 的 `|` 联合类型标注"
                    )
                    assert not isinstance(sub, ast.Subscript), (
                        f"节点 {node['id']} 包含不兼容 Python 3.8 的内置泛型下标标注"
                    )
        assert imported <= allowed_imports, (
            f"节点 {node['id']} 导入了允许集合外的模块: {sorted(imported - allowed_imports)}"
        )
        ns: dict = {}
        exec(compile(code_src, f"<node:{node['id']}>", "exec"), ns)
        assert callable(ns.get("main"))

    # Simulate Dify export noise (viewport, selected, positionAbsolute, encrypted dataset_ids)
    exported = copy.deepcopy(rendered_doc)
    exported["workflow"]["graph"]["viewport"] = {"x": 12.5, "y": -34.0, "zoom": 0.8}
    for n in exported["workflow"]["graph"]["nodes"]:
        n["selected"] = False
        n["positionAbsolute"] = dict(n["position"])
        n["data"]["selected"] = False
        if n["data"]["type"] == "knowledge-retrieval":
            n["data"]["dataset_ids"] = ["encrypted-opaque-cipher-value"]
    exported_path = tmp_path / "exported.yml"
    exported_path.write_text(yaml.safe_dump(exported, allow_unicode=True), encoding="utf-8")

    # Round-trip normalize back to template -> must match checked-in template byte-for-byte
    restored_path = tmp_path / "restored.template.yml"
    warnings = normalize_dsl.normalize_dsl_file(exported_path, output_path=restored_path)
    assert warnings == []
    rendered_text = rendered_path.read_text(encoding="utf-8")
    restored_text = restored_path.read_text(encoding="utf-8")
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert restored_text == template_text
    assert "text: |-" in rendered_text
    assert "code: |" in rendered_text
    assert "text: |-" in restored_text


def test_render_with_rerank_enabled(tmp_path: Path) -> None:
    kb_lock = tmp_path / "kb.lock.json"
    kb_lock.write_text(
        json.dumps({"policy": "p-uuid", "handbook": "h-uuid"}),
        encoding="utf-8",
    )
    rendered_path = tmp_path / "rerank.dsl.yml"
    render_dsl.render_dsl_file(
        template_path=TEMPLATE_PATH,
        kb_lock_path=kb_lock,
        output_path=rendered_path,
        llm_provider="org/plug/prov",
        llm_model="main-model",
        rerank_provider="org/plug/rerank",
        rerank_model="bge-reranker",
    )
    doc = yaml.safe_load(rendered_path.read_text(encoding="utf-8"))
    for node in doc["workflow"]["graph"]["nodes"]:
        if node["data"]["type"] == "knowledge-retrieval":
            mrc = node["data"]["multiple_retrieval_config"]
            assert mrc["top_k"] == 6
            assert mrc["reranking_enable"] is True
            assert mrc["reranking_model"] == {
                "provider": "org/plug/rerank",
                "model": "bge-reranker",
            }


def test_normalize_detects_code_drift_and_numeric_id(tmp_path: Path) -> None:
    template = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))

    # Code drift raises ValueError
    drifted = copy.deepcopy(template)
    for n in drifted["workflow"]["graph"]["nodes"]:
        if n["id"] == "resolve":
            n["data"]["code"] = "def main():\n    return {}\n"
    with pytest.raises(ValueError, match="workflows/code/resolve_context.py"):
        normalize_dsl.normalize_dsl_data(drifted)

    # Numeric node ID emits warning
    with_numeric = copy.deepcopy(template)
    with_numeric["workflow"]["graph"]["nodes"].append(
        {"id": "1755000000000", "type": "custom", "data": {"type": "answer", "answer": "hi"}}
    )
    _, warnings = normalize_dsl.normalize_dsl_data(with_numeric)
    assert any("1755000000000" in w for w in warnings)


def test_check_dsl_negative_rules(tmp_path: Path) -> None:
    base = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))

    def write_and_check(mutated: dict, mode: str = "template") -> list[str]:
        p = tmp_path / "bad.yml"
        p.write_text(yaml.safe_dump(mutated, allow_unicode=True), encoding="utf-8")
        return check_dsl.check_dsl(p, mode=mode)

    # D1: wrong mode
    d1 = copy.deepcopy(base)
    d1["app"]["mode"] = "workflow"
    assert any(e.startswith("D1:") for e in write_and_check(d1))

    # D2: missing hide on user_token
    d2 = copy.deepcopy(base)
    d2["workflow"]["graph"]["nodes"][0]["data"]["variables"][0]["hide"] = False
    assert any(e.startswith("D2:") for e in write_and_check(d2))

    # D3: user_token leaked into LLM prompt
    d3 = copy.deepcopy(base)
    for n in d3["workflow"]["graph"]["nodes"]:
        if n["id"] == "advise":
            n["data"]["prompt_template"][0]["text"] += "\n{{#start.user_token#}}"
    assert any(e.startswith("D3:") for e in write_and_check(d3))

    # D4: secret literal (app-xxx and JWT)
    d4 = copy.deepcopy(base)
    d4["app"]["description"] = "app-secretkey1234567890"
    assert any(e.startswith("D4:") for e in write_and_check(d4))

    # D5: http-request retry enabled & Authorization header
    d5 = copy.deepcopy(base)
    for n in d5["workflow"]["graph"]["nodes"]:
        if n["id"] == "call_get_order":
            n["data"]["retry_config"]["retry_enabled"] = True
            n["data"]["headers"] = "Authorization: Bearer fixed"
    errs5 = write_and_check(d5)
    assert sum(1 for e in errs5 if e.startswith("D5:")) >= 2

    # D6: unknown tool name
    d6 = copy.deepcopy(base)
    for n in d6["workflow"]["graph"]["nodes"]:
        if n["id"] == "call_get_order":
            n["data"]["body"]["data"][0]["value"] = '{"params":{"name":"delete_everything"}}'
    assert any(e.startswith("D6:") for e in write_and_check(d6))

    # D8: shortcut edge bypassing precheck_ok directly to call_create_refund
    d8 = copy.deepcopy(base)
    d8["workflow"]["graph"]["edges"].append(
        {"id": "bypass", "source": "start", "target": "call_create_refund"}
    )
    assert any(e.startswith("D8:") for e in write_and_check(d8))

    # D9: two context-enabled LLM nodes on one path
    d9 = copy.deepcopy(base)
    d9["workflow"]["graph"]["edges"].append(
        {"id": "double_llm", "source": "advise", "target": "answer_kb"}
    )
    assert any(e.startswith("D9:") for e in write_and_check(d9))

    # D10: retriever_resource disabled & is_refund mismatch
    d10 = copy.deepcopy(base)
    d10["workflow"]["features"]["retriever_resource"]["enabled"] = False
    for n in d10["workflow"]["graph"]["nodes"]:
        if n["id"] == "is_refund":
            n["data"]["cases"][0]["conditions"][0]["value"] = "不存在的意图"
    errs10 = write_and_check(d10)
    assert sum(1 for e in errs10 if e.startswith("D10:")) == 2

    # D11: rendered mode with leftover placeholders
    assert any(e.startswith("D11:") for e in write_and_check(base, mode="rendered"))

    # D12: LLM nodes missing 4.8.8 shared prompt rules
    d12_decide = copy.deepcopy(base)
    for n in d12_decide["workflow"]["graph"]["nodes"]:
        if n["id"] == "decide_refund":
            n["data"]["prompt_template"][0]["text"] = n["data"]["prompt_template"][0][
                "text"
            ].replace("对话对象是客服或主管，不是客户", "")
    errs12_decide = write_and_check(d12_decide)
    assert any(e.startswith("D12:") and "decide_refund" in e for e in errs12_decide)

    d12_advise = copy.deepcopy(base)
    for n in d12_advise["workflow"]["graph"]["nodes"]:
        if n["id"] == "advise":
            n["data"]["prompt_template"][0]["text"] = n["data"]["prompt_template"][0][
                "text"
            ].replace("两位小数", "")
    errs12_advise = write_and_check(d12_advise)
    assert any(e.startswith("D12:") and "advise" in e for e in errs12_advise)

    d12_kb = copy.deepcopy(base)
    for n in d12_kb["workflow"]["graph"]["nodes"]:
        if n["id"] == "answer_kb":
            n["data"]["prompt_template"][0]["text"] = n["data"]["prompt_template"][0][
                "text"
            ].replace("一律不执行", "")
    errs12_kb = write_and_check(d12_kb)
    assert any(e.startswith("D12:") and "answer_kb" in e for e in errs12_kb)

    d12_nosys = copy.deepcopy(base)
    for n in d12_nosys["workflow"]["graph"]["nodes"]:
        if n["id"] == "answer_kb":
            n["data"]["prompt_template"] = []
    errs12_nosys = write_and_check(d12_nosys)
    assert any(e.startswith("D12:") and "answer_kb" in e and "system" in e for e in errs12_nosys)
