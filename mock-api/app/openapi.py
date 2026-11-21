from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

API_TITLE = "售后订单系统 API"
API_VERSION = "1.0.0"

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
_FORBIDDEN_KEYS = {"$ref", "anyOf", "oneOf", "allOf"}


def _validation_error_422_response() -> dict[str, Any]:
    return {
        "description": "参数校验失败",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "机器可读错误码"},
                        "message": {"type": "string", "description": "中文错误说明"},
                        "request_id": {"type": "string", "description": "请求追踪 ID"},
                        "details": {
                            "type": "array",
                            "description": "字段级校验错误明细",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string", "description": "出错字段"},
                                    "message": {
                                        "type": "string",
                                        "description": "字段校验失败说明",
                                    },
                                },
                                "required": ["field", "message"],
                            },
                        },
                    },
                    "required": ["code", "message", "request_id", "details"],
                }
            }
        },
    }


def _resolve_ref_target(ref: str, schemas: dict[str, Any]) -> dict[str, Any]:
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        raise ValueError(f"Unsupported $ref target: {ref}")
    name = ref[len(prefix) :]
    if name not in schemas:
        raise ValueError(f"Schema not found for $ref: {ref}")
    return copy.deepcopy(schemas[name])


def _inline_refs(node: Any, schemas: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline_refs(item, schemas) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        resolved = _inline_refs(_resolve_ref_target(node["$ref"], schemas), schemas)
        merged = dict(resolved)
        for key, value in node.items():
            if key != "$ref":
                merged[key] = _inline_refs(value, schemas)
        return merged

    if "allOf" in node and isinstance(node["allOf"], list) and len(node["allOf"]) == 1:
        inner = _inline_refs(node["allOf"][0], schemas)
        if isinstance(inner, dict):
            merged = dict(inner)
            for key, value in node.items():
                if key != "allOf":
                    merged[key] = _inline_refs(value, schemas)
            return merged

    return {key: _inline_refs(value, schemas) for key, value in node.items()}


def _normalize_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    node = dict(schema)
    was_nullable = False

    # 步骤 4：含 {"type": "null"} 分支的 anyOf
    if "anyOf" in node and isinstance(node["anyOf"], list):
        branches = node["anyOf"]
        non_null = [b for b in branches if not (isinstance(b, dict) and b == {"type": "null"})]
        if len(non_null) == 1 and len(non_null) < len(branches):
            was_nullable = True
            base = dict(non_null[0])
            for key, value in node.items():
                if key == "anyOf":
                    continue
                if key == "default" and value is None:
                    continue
                base[key] = value
            node = base

    # 兼容数组形式 type: ["string", "null"]
    if isinstance(node.get("type"), list):
        types = node["type"]
        non_null_types = [t for t in types if t != "null"]
        if len(non_null_types) == 1 and len(non_null_types) < len(types):
            was_nullable = True
            node["type"] = non_null_types[0]
            if node.get("default") is None:
                node.pop("default", None)

    # 步骤 5：const / examples / 数值形式 exclusiveMinimum / exclusiveMaximum
    if "const" in node:
        const_val = node.pop("const")
        node["enum"] = [const_val]

    if "examples" in node and isinstance(node["examples"], list):
        examples = node.pop("examples")
        if examples and "example" not in node:
            node["example"] = examples[0]

    excl_min = node.get("exclusiveMinimum")
    if isinstance(excl_min, (int, float)) and not isinstance(excl_min, bool):
        node["minimum"] = excl_min
        node["exclusiveMinimum"] = True

    excl_max = node.get("exclusiveMaximum")
    if isinstance(excl_max, (int, float)) and not isinstance(excl_max, bool):
        node["maximum"] = excl_max
        node["exclusiveMaximum"] = True

    # 步骤 7：删除 schema 中的 title（减少噪声）
    node.pop("title", None)

    # 递归规范化子属性，并把因去 null 而变为可选的属性从 required 移除
    if isinstance(node.get("properties"), dict):
        new_props: dict[str, Any] = {}
        optional_props: set[str] = set()
        for prop_name, prop_schema in node["properties"].items():
            if isinstance(prop_schema, dict):
                norm_prop, prop_nullable = _normalize_schema(prop_schema)
                new_props[prop_name] = norm_prop
                if prop_nullable:
                    optional_props.add(prop_name)
            else:
                new_props[prop_name] = prop_schema
        node["properties"] = new_props
        if optional_props and isinstance(node.get("required"), list):
            remaining = [name for name in node["required"] if name not in optional_props]
            if remaining:
                node["required"] = remaining
            else:
                node.pop("required", None)

    if isinstance(node.get("items"), dict):
        node["items"], _ = _normalize_schema(node["items"])

    if isinstance(node.get("additionalProperties"), dict):
        node["additionalProperties"], _ = _normalize_schema(node["additionalProperties"])

    return node, was_nullable


def _self_check(node: Any, path: str = "$") -> None:
    if isinstance(node, list):
        for idx, item in enumerate(node):
            _self_check(item, f"{path}[{idx}]")
        return
    if not isinstance(node, dict):
        return

    for key, value in node.items():
        if key in _FORBIDDEN_KEYS:
            raise ValueError(f"OpenAPI 3.0.3 self-check failed: forbidden key '{key}' at {path}")
        if key == "type":
            if isinstance(value, list):
                raise ValueError(f"OpenAPI 3.0.3 self-check failed: array 'type' at {path}")
            if value == "null":
                raise ValueError(f"OpenAPI 3.0.3 self-check failed: null 'type' at {path}")
        _self_check(value, f"{path}.{key}")


def downgrade_openapi(raw_schema: dict[str, Any], *, public_base_url: str) -> dict[str, Any]:
    spec = copy.deepcopy(raw_schema)

    # 步骤 1：版本与基础信息
    spec["openapi"] = "3.0.3"
    info = spec.setdefault("info", {})
    info["title"] = API_TITLE
    info["version"] = API_VERSION

    # 步骤 2：servers
    spec["servers"] = [{"url": public_base_url}]

    # 步骤 6：在展开引用前替换 FastAPI 自动生成的 422 响应，彻底消除 HTTPValidationError
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if isinstance(responses, dict) and "422" in responses:
                responses["422"] = _validation_error_422_response()

    # 步骤 3：内联所有 $ref（含单元素 allOf），然后删除 components.schemas
    components = spec.get("components")
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    if isinstance(spec.get("paths"), dict):
        spec["paths"] = _inline_refs(spec["paths"], schemas)
    if isinstance(components, dict) and "schemas" in components:
        del components["schemas"]
        if not components:
            del spec["components"]

    # 步骤 4、5、7：遍历所有参数、请求体与响应中的 schema 做降级规范化
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue

            for param in operation.get("parameters", []):
                if isinstance(param, dict) and isinstance(param.get("schema"), dict):
                    param["schema"], _ = _normalize_schema(param["schema"])

            request_body = operation.get("requestBody")
            if isinstance(request_body, dict):
                for media in request_body.get("content", {}).values():
                    if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                        media["schema"], _ = _normalize_schema(media["schema"])

            for resp in operation.get("responses", {}).values():
                if isinstance(resp, dict):
                    for media in resp.get("content", {}).values():
                        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                            media["schema"], _ = _normalize_schema(media["schema"])

    # 步骤 8：严格自检
    _self_check(spec)
    return spec


def build_openapi(app: FastAPI) -> dict[str, Any]:
    raw = get_openapi(
        title=API_TITLE,
        version=API_VERSION,
        routes=app.routes,
    )
    public_base_url = app.state.settings.public_base_url
    return downgrade_openapi(raw, public_base_url=public_base_url)
