from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.export_openapi import export_openapi_to_file
from app.main import create_app
from app.openapi import build_openapi, downgrade_openapi

OP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
PRIMITIVE_TYPES = {"string", "integer", "number", "boolean"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
EXPECTED_OPS = {"get_order", "list_orders", "get_refund", "list_refunds", "create_refund"}


def _walk_check_schema_descriptions_and_types(schema: dict[str, Any], path: str) -> None:
    assert "type" in schema and isinstance(schema["type"], str), f"{path} 缺少合法 type"
    assert "title" not in schema, f"{path} 不应包含 title"
    assert "nullable" not in schema, f"{path} 不应包含 nullable"
    if schema["type"] == "object":
        props = schema.get("properties", {})
        for prop_name, prop_schema in props.items():
            assert prop_schema.get("description"), f"{path}.properties.{prop_name} 缺少 description"
            _walk_check_schema_descriptions_and_types(prop_schema, f"{path}.properties.{prop_name}")
    elif schema["type"] == "array":
        items = schema.get("items")
        assert isinstance(items, dict), f"{path}.items 必须为对象"
        _walk_check_schema_descriptions_and_types(items, f"{path}.items")


def _assert_no_forbidden_constructs(node: Any, path: str = "$") -> None:
    if isinstance(node, list):
        for idx, item in enumerate(node):
            _assert_no_forbidden_constructs(item, f"{path}[{idx}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            assert key not in {"$ref", "anyOf", "oneOf", "allOf", "nullable"}, (
                f"{path} 残留禁止字段 {key}"
            )
            if key == "type":
                assert not isinstance(value, list), f"{path}.type 不得为数组"
                assert value != "null", f"{path}.type 不得为 null"
            _assert_no_forbidden_constructs(value, f"{path}.{key}")


def test_openapi_document_meets_all_contracts(app: FastAPI, client: TestClient) -> None:
    generated = build_openapi(app)
    runtime = client.get("/openapi.json").json()
    committed_path = PROJECT_ROOT / "openapi.json"
    committed = json.loads(committed_path.read_text(encoding="utf-8"))

    assert generated == runtime
    assert committed == generated, "入库 openapi.json 与代码生成结果不一致，请重新导出"

    # 基本元信息
    assert generated["openapi"] == "3.0.3"
    assert generated["info"]["title"] == "售后订单系统 API"
    assert generated["info"]["version"] == "1.0.0"
    assert generated["servers"] == [{"url": "http://mock-api:8000"}]

    # 安全方案
    sec_schemes = generated["components"]["securitySchemes"]
    assert sec_schemes["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert "schemas" not in generated["components"]

    # 禁止结构自检
    _assert_no_forbidden_constructs(generated)

    # 非业务端点不出现在 paths
    for non_business in ("/healthz", "/openapi.json", "/docs"):
        assert non_business not in generated["paths"]

    # 遍历全部操作
    seen_op_ids: set[str] = set()
    for path, path_item in generated["paths"].items():
        for method, op in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            op_id = op["operationId"]
            assert OP_ID_RE.match(op_id), f"operationId 格式非法: {op_id}"
            assert op_id not in seen_op_ids, f"重复的 operationId: {op_id}"
            seen_op_ids.add(op_id)

            assert op.get("summary"), f"{op_id} 缺少 summary"
            assert op.get("description"), f"{op_id} 缺少 description"
            assert op.get("security") == [{"BearerAuth": []}], f"{op_id} 缺少操作级 security"

            # 读写规范（G7 & 1.11）
            if method.lower() == "get":
                assert op_id.startswith(("get_", "list_")), f"GET 操作前缀不符: {op_id}"
            else:
                assert op_id.startswith(("create_", "update_", "cancel_", "delete_"))
                assert op["summary"].endswith("【写操作】")
                assert op["description"].startswith("【写操作】")

            # 参数均为基础类型且带 description
            for param in op.get("parameters", []):
                assert param.get("description"), f"{op_id} 参数 {param['name']} 缺少 description"
                assert param["schema"]["type"] in PRIMITIVE_TYPES

            # 请求体为扁平对象且每个属性为基础类型 + description
            if "requestBody" in op:
                req_schema = op["requestBody"]["content"]["application/json"]["schema"]
                assert req_schema["type"] == "object"
                for prop_name, prop_schema in req_schema["properties"].items():
                    assert prop_schema.get("description"), (
                        f"{op_id} 请求体字段 {prop_name} 缺少 description"
                    )
                    assert prop_schema["type"] in PRIMITIVE_TYPES

            # 恰好一个 2xx 响应，根为 object，所有属性有 description 与 type
            responses = op["responses"]
            two_xx = [code for code in responses if code.startswith("2")]
            assert len(two_xx) == 1, f"{op_id} 必须恰好声明一个 2xx 响应"
            ok_content = responses[two_xx[0]]["content"]
            assert list(ok_content.keys()) == ["application/json"]
            ok_schema = ok_content["application/json"]["schema"]
            assert ok_schema["type"] == "object"
            _walk_check_schema_descriptions_and_types(ok_schema, f"{path}.{method}.2xx")

            # 422 响应符合 G8
            if "422" in responses:
                err_422 = responses["422"]["content"]["application/json"]["schema"]
                assert set(err_422["required"]) == {"code", "message", "request_id", "details"}

    assert seen_op_ids == EXPECTED_OPS


def test_downgrade_openapi_rules_unit() -> None:
    raw: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "old", "version": "0.1"},
        "paths": {
            "/items": {
                "post": {
                    "operationId": "create_item",
                    "parameters": [
                        {
                            "name": "tag",
                            "in": "query",
                            "required": False,
                            "description": "标签",
                            "schema": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "default": None,
                                "title": "Tag",
                            },
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ItemIn"}}
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "title": "ItemOut",
                                        "properties": {
                                            "kind": {
                                                "const": "widget",
                                                "type": "string",
                                                "description": "类型",
                                            },
                                            "score": {
                                                "type": "number",
                                                "exclusiveMinimum": 0,
                                                "exclusiveMaximum": 100,
                                                "examples": [50],
                                                "description": "分值",
                                            },
                                            "note": {
                                                "type": ["string", "null"],
                                                "default": None,
                                                "description": "备注",
                                            },
                                            "nested": {
                                                "allOf": [{"$ref": "#/components/schemas/Meta"}],
                                                "description": "字段级覆盖描述",
                                            },
                                        },
                                        "required": ["kind", "score", "note", "nested"],
                                    }
                                }
                            },
                        },
                        "422": {
                            "description": "Validation Error",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                                }
                            },
                        },
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "ItemIn": {
                    "type": "object",
                    "title": "ItemIn",
                    "properties": {
                        "opt": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "description": "可选值",
                        }
                    },
                    "required": ["opt"],
                },
                "Meta": {
                    "type": "object",
                    "title": "Meta",
                    "description": "原始模型描述",
                    "properties": {"v": {"type": "string", "title": "V", "description": "版本"}},
                    "required": ["v"],
                },
                "HTTPValidationError": {
                    "type": "object",
                    "properties": {
                        "detail": {
                            "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                            "type": "array",
                        }
                    },
                },
            }
        },
    }

    out = downgrade_openapi(raw, public_base_url="http://mock-api:8000")
    assert out["openapi"] == "3.0.3"
    assert out["servers"] == [{"url": "http://mock-api:8000"}]
    assert "components" not in out

    op = out["paths"]["/items"]["post"]
    # 参数 anyOf+null 被降级且移除 default: null 和 title
    assert op["parameters"][0]["schema"] == {"type": "string"}

    # 请求体 $ref 被内联且唯一属性变为可选后 required 被移除
    req_schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert req_schema["properties"]["opt"] == {"type": "integer", "description": "可选值"}
    assert "required" not in req_schema

    # 响应 const / exclusiveMinimum / exclusiveMaximum / examples / type数组 / allOf 描述覆盖
    resp_schema = op["responses"]["201"]["content"]["application/json"]["schema"]
    assert resp_schema["properties"]["kind"]["enum"] == ["widget"]
    assert "const" not in resp_schema["properties"]["kind"]
    assert resp_schema["properties"]["score"]["minimum"] == 0
    assert resp_schema["properties"]["score"]["exclusiveMinimum"] is True
    assert resp_schema["properties"]["score"]["maximum"] == 100
    assert resp_schema["properties"]["score"]["exclusiveMaximum"] is True
    assert resp_schema["properties"]["score"]["example"] == 50
    assert "examples" not in resp_schema["properties"]["score"]
    assert resp_schema["properties"]["note"] == {"type": "string", "description": "备注"}
    assert "note" not in resp_schema["required"]
    assert resp_schema["properties"]["nested"]["description"] == "字段级覆盖描述"
    assert "allOf" not in resp_schema["properties"]["nested"]


def test_downgrade_self_check_rejects_residual_forbidden_schema() -> None:
    bad_raw = {
        "openapi": "3.1.0",
        "info": {"title": "x", "version": "1"},
        "paths": {
            "/bad": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"anyOf": [{"type": "string"}, {"type": "integer"}]}
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    with pytest.raises(ValueError, match="self-check failed"):
        downgrade_openapi(bad_raw, public_base_url="http://mock-api:8000")


def test_export_openapi_offline_writes_identical_file(tmp_path: Path) -> None:
    out_file = tmp_path / "openapi.json"
    exported = export_openapi_to_file(out_file, settings=Settings())
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert written == exported
    app = create_app(settings=Settings(), init_database=False)
    assert written == build_openapi(app)
