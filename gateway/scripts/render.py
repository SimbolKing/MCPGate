#!/usr/bin/env python3
"""Render Higress WasmPlugin manifests and tools.manifest.json with C1–C11 contract checks."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
VALID_SCHEMA_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})
RSA_PRIVATE_KEY_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})
DNS_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$")

DEFAULT_JWT_ISSUER = "http://dev-idp:8010"
DEFAULT_JWT_AUDIENCE = "mcpgate"
DEFAULT_MCP_SERVER_PLUGIN_VERSION = "2.0.3"
DEFAULT_JWT_AUTH_PLUGIN_VERSION = "2.0.0"
DEFAULT_JWKS_FILE = "jwt/keys/jwks.json"


class ContractError(Exception):
    """Raised when one or more C1–C11 contract checks fail."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


class LiteralDumper(yaml.SafeDumper):
    """Deterministic SafeDumper using block style for multiline strings and no YAML anchors."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


LiteralDumper.add_representer(str, _represent_str)


@dataclass(frozen=True)
class RenderConfig:
    openapi_path: Path
    raw_mcp_path: Path
    approval_tools_path: Path
    mcp_plugin_template_path: Path
    jwt_plugin_template_path: Path
    ingress_path: Path
    mcpbridge_path: Path
    jwks_path: Path
    output_dir: Path
    mcp_server_plugin_version: str = DEFAULT_MCP_SERVER_PLUGIN_VERSION
    jwt_auth_plugin_version: str = DEFAULT_JWT_AUTH_PLUGIN_VERSION
    jwt_issuer: str = DEFAULT_JWT_ISSUER
    jwt_audience: str = DEFAULT_JWT_AUDIENCE


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from an env file without executing shell code."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def dump_yaml(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=LiteralDumper,
        sort_keys=False,
        allow_unicode=True,
        width=1000,
    )


def _effective_authority(url: str) -> tuple[str, str, int | None] | None:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        if scheme == "http":
            port = 80
        elif scheme == "https":
            port = 443
    return (scheme, hostname, port)


def build_approval_list_schema(approval_schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap Approval object schema in the paginated list response schema for list_approvals."""
    return {
        "type": "object",
        "required": ["items", "total", "page", "page_size"],
        "properties": {
            "items": {
                "type": "array",
                "description": "退款申请（审批单）列表",
                "items": copy.deepcopy(approval_schema),
            },
            "total": {
                "type": "integer",
                "description": "符合条件且可见的退款申请总数",
            },
            "page": {
                "type": "integer",
                "description": "当前页码",
            },
            "page_size": {
                "type": "integer",
                "description": "每页条数",
            },
        },
    }


def _check_c11_approval_schema(approval_tools: dict[str, Any], errors: list[str]) -> None:
    schema = approval_tools.get("approvalSchema")
    if not isinstance(schema, dict):
        errors.append("C11: approval-tools.yaml 缺少合法的 approvalSchema 对象")
        return
    if schema.get("type") != "object":
        errors.append(f"C11: approvalSchema.type 必须为 'object'，实际为 {schema.get('type')!r}")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        errors.append("C11: approvalSchema.properties 必须为非空对象")
        return
    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            errors.append(f"C11: approvalSchema.properties.{prop_name} 必须是对象")
            continue
        prop_type = prop_def.get("type")
        if not isinstance(prop_type, str) or prop_type not in VALID_SCHEMA_TYPES:
            errors.append(f"C11: approvalSchema.properties.{prop_name}.type 非法: {prop_type!r}")
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            errors.append("C11: approvalSchema.required 必须是字符串列表")
        else:
            seen: set[str] = set()
            for field in required:
                if not isinstance(field, str) or not field.strip():
                    errors.append(f"C11: approvalSchema.required 包含非法字段名: {field!r}")
                    continue
                if field in seen:
                    errors.append(f"C11: approvalSchema.required 包含重复字段: {field}")
                seen.add(field)
                if field not in properties:
                    errors.append(
                        f"C11: approvalSchema.required 中的字段 {field!r} 不在 properties 中"
                    )


def _check_c7_jwks(jwks_path: Path, errors: list[str]) -> str:
    if not jwks_path.exists():
        errors.append(f"C7: JWKS 文件不存在: {jwks_path}")
        return ""
    raw_text = jwks_path.read_text(encoding="utf-8")
    try:
        jwks = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        errors.append(f"C7: JWKS 文件不是合法 JSON ({jwks_path}): {exc}")
        return ""
    if not isinstance(jwks, dict):
        errors.append("C7: JWKS 顶层必须是 JSON 对象")
        return ""
    keys = jwks.get("keys")
    if not isinstance(keys, list) or len(keys) == 0:
        errors.append("C7: JWKS 必须包含至少一把密钥（keys 为非空数组）")
        return ""
    for idx, key in enumerate(keys):
        if not isinstance(key, dict):
            errors.append(f"C7: JWKS keys[{idx}] 必须是对象")
            continue
        if key.get("kty") != "RSA":
            errors.append(f"C7: JWKS keys[{idx}].kty 必须为 'RSA'，实际为 {key.get('kty')!r}")
        kid = key.get("kid")
        if not isinstance(kid, str) or not kid.strip():
            errors.append(f"C7: JWKS keys[{idx}] 缺少非空 kid")
        if key.get("alg") != "RS256":
            errors.append(f"C7: JWKS keys[{idx}].alg 必须为 'RS256'，实际为 {key.get('alg')!r}")
        for pub_field in ("n", "e"):
            val = key.get(pub_field)
            if not isinstance(val, str) or not val.strip():
                errors.append(f"C7: JWKS keys[{idx}] 缺少 RSA 公钥字段 {pub_field!r}")
        private_fields = sorted(RSA_PRIVATE_KEY_FIELDS.intersection(key.keys()))
        if private_fields:
            errors.append(f"C7: JWKS keys[{idx}] 不能包含私钥字段: {', '.join(private_fields)}")
    return raw_text.rstrip("\n") + "\n"


def _check_c6_c8_routing(
    ingress: dict[str, Any],
    mcpbridge: dict[str, Any],
    upstream_tools: list[dict[str, Any]],
    errors: list[str],
) -> None:
    annotations = (ingress.get("metadata") or {}).get("annotations") or {}

    # C8: explicit retry disable
    tries = annotations.get("higress.io/proxy-next-upstream-tries")
    if tries != "0":
        errors.append(
            f'C8: Ingress 必须带注解 higress.io/proxy-next-upstream-tries: "0"，实际为 {tries!r}'
        )

    # C6 part 1: Ingress destination matches McpBridge guard entry
    registries = (mcpbridge.get("spec") or {}).get("registries") or []
    guard_entry = next(
        (r for r in registries if isinstance(r, dict) and r.get("name") == "guard"),
        None,
    )
    if guard_entry is None:
        errors.append("C6: McpBridge 中缺少 name='guard' 的服务来源条目")
    else:
        if guard_entry.get("type") != "dns":
            errors.append(
                f"C6: McpBridge guard 条目的 type 必须为 'dns'，实际为 {guard_entry.get('type')!r}"
            )
        domain = guard_entry.get("domain")
        if not isinstance(domain, str) or not DNS_DOMAIN_RE.match(domain):
            errors.append(
                f"C6: McpBridge guard 条目的 domain 必须是带字母顶级域的域名，实际为 {domain!r}"
            )
        port = guard_entry.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            errors.append(f"C6: McpBridge guard 条目的 port 非法: {port!r}")
        else:
            expected_dest = f"{guard_entry['name']}.dns:{port}"
            actual_dest = annotations.get("higress.io/destination")
            if actual_dest != expected_dest:
                errors.append(
                    f"C6: Ingress higress.io/destination ({actual_dest!r}) "
                    f"与 McpBridge guard 条目 ({expected_dest!r}) 不一致"
                )

    # C6 part 2: all upstream tools share the same scheme, host, port
    authorities: dict[tuple[str, str, int | None], list[str]] = {}
    for tool in upstream_tools:
        name = tool.get("name", "<unnamed>")
        url = (tool.get("requestTemplate") or {}).get("url", "")
        auth = _effective_authority(url) if isinstance(url, str) else None
        if auth is None:
            errors.append(f"C6: 上游工具 {name} 的 requestTemplate.url 非法: {url!r}")
            continue
        authorities.setdefault(auth, []).append(name)
    if not upstream_tools:
        errors.append("C6: 上游工具列表为空，无法确认上游地址一致性")
    elif len(authorities) > 1:
        details = ", ".join(
            f"{s}://{h}:{p} ({','.join(names)})" for (s, h, p), names in authorities.items()
        )
        errors.append(f"C6: 上游工具指向了多个不同的后端地址: {details}")


def _extract_openapi_operations(
    openapi: dict[str, Any], errors: list[str]
) -> dict[str, tuple[str, str]]:
    servers = openapi.get("servers")
    base_url = ""
    if (
        isinstance(servers, list)
        and servers
        and isinstance(servers[0], dict)
        and isinstance(servers[0].get("url"), str)
        and servers[0]["url"].strip()
    ):
        base_url = servers[0]["url"].rstrip("/")
    else:
        errors.append("C2: openapi.json 缺少合法的 servers[0].url")

    ops: dict[str, tuple[str, str]] = {}
    paths = openapi.get("paths") or {}
    if not isinstance(paths, dict):
        errors.append("C1: openapi.json 的 paths 必须是对象")
        return ops

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            if not isinstance(op_id, str) or not op_id.strip():
                errors.append(f"C1: OpenAPI 操作 {method.upper()} {path} 缺少 operationId")
                continue
            if op_id in ops:
                errors.append(f"C1: OpenAPI 中存在重复的 operationId: {op_id}")
                continue
            normalized_path = path if path.startswith("/") else f"/{path}"
            ops[op_id] = (method.upper(), f"{base_url}{normalized_path}")
    return ops


def _check_c3_c4_c5(
    server_cfg: dict[str, Any],
    merged_tools: list[dict[str, Any]],
    errors: list[str],
) -> None:
    schemes = server_cfg.get("securitySchemes") or []
    if not isinstance(schemes, list):
        errors.append("C3: server.securitySchemes 必须是列表")
        schemes = []

    has_bearer = any(
        isinstance(s, dict)
        and s.get("id") == "BearerAuth"
        and s.get("type") == "http"
        and s.get("scheme") == "bearer"
        for s in schemes
    )
    if not has_bearer:
        errors.append(
            "C3: server.securitySchemes 缺少 {id: BearerAuth, type: http, scheme: bearer}"
        )

    for scheme in schemes:
        if isinstance(scheme, dict) and "defaultCredential" in scheme:
            errors.append(
                f"C4: server.securitySchemes.{scheme.get('id', '<unknown>')} "
                "禁止配置 defaultCredential"
            )

    for tool in merged_tools:
        name = tool.get("name", "<unnamed>")
        sec = tool.get("security")
        req_tpl = tool.get("requestTemplate") or {}
        req_sec = req_tpl.get("security")

        # C3: passthrough + requestTemplate.security.id == BearerAuth
        if not (
            isinstance(sec, dict)
            and sec.get("id") == "BearerAuth"
            and sec.get("passthrough") is True
        ):
            errors.append(
                f"C3: 工具 {name} 的 security 必须为 "
                f"{{id: BearerAuth, passthrough: true}}，实际为 {sec!r}"
            )
        if not (isinstance(req_sec, dict) and req_sec.get("id") == "BearerAuth"):
            errors.append(
                f"C3: 工具 {name} 的 requestTemplate.security.id 必须为 'BearerAuth'，"
                f"实际为 {req_sec!r}"
            )

        # C4: no hardcoded credentials anywhere in security config
        if isinstance(sec, dict) and "credential" in sec:
            errors.append(f"C4: 工具 {name} 禁止配置 security.credential")
        if isinstance(req_sec, dict) and "credential" in req_sec:
            errors.append(f"C4: 工具 {name} 禁止配置 requestTemplate.security.credential")

        # C5: every arg has non-empty description & type, and no default
        args = tool.get("args") or []
        if not isinstance(args, list):
            errors.append(f"C5: 工具 {name} 的 args 必须是列表")
            continue
        for arg in args:
            if not isinstance(arg, dict):
                errors.append(f"C5: 工具 {name} 包含非对象参数定义: {arg!r}")
                continue
            arg_name = arg.get("name", "<unnamed>")
            desc = arg.get("description")
            arg_type = arg.get("type")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"C5: 工具 {name} 的参数 {arg_name} 缺少非空 description")
            if not isinstance(arg_type, str) or not arg_type.strip():
                errors.append(f"C5: 工具 {name} 的参数 {arg_name} 缺少非空 type")
            if "default" in arg:
                errors.append(f"C5: 工具 {name} 的参数 {arg_name} 禁止配置 default")


def render_gateway(config: RenderConfig) -> dict[str, Any]:
    """Validate C1–C11, render artifacts, and write outputs atomically on success."""
    errors: list[str] = []

    openapi = json.loads(config.openapi_path.read_text(encoding="utf-8"))
    raw_mcp = yaml.safe_load(config.raw_mcp_path.read_text(encoding="utf-8")) or {}
    approval_tools = yaml.safe_load(config.approval_tools_path.read_text(encoding="utf-8")) or {}
    mcp_plugin_tpl = yaml.safe_load(config.mcp_plugin_template_path.read_text(encoding="utf-8"))
    jwt_plugin_tpl = yaml.safe_load(config.jwt_plugin_template_path.read_text(encoding="utf-8"))
    ingress = yaml.safe_load(config.ingress_path.read_text(encoding="utf-8")) or {}
    mcpbridge = yaml.safe_load(config.mcpbridge_path.read_text(encoding="utf-8")) or {}

    # C11 & approvalFieldGuide check
    _check_c11_approval_schema(approval_tools, errors)
    approval_schema = approval_tools.get("approvalSchema") or {}
    approval_field_guide = approval_tools.get("approvalFieldGuide")
    if not isinstance(approval_field_guide, str) or not approval_field_guide.strip():
        errors.append("C11: approval-tools.yaml 缺少非空的 approvalFieldGuide")

    # C7: JWKS validation
    jwks_text = _check_c7_jwks(config.jwks_path, errors)

    # Extract OpenAPI operations and tool lists
    openapi_ops = _extract_openapi_operations(openapi, errors)

    raw_upstream_tools = raw_mcp.get("tools") or []
    if not isinstance(raw_upstream_tools, list):
        errors.append("C1: aftersale.mcp.yaml 中的 tools 必须是列表")
        raw_upstream_tools = []
    upstream_tools: list[dict[str, Any]] = copy.deepcopy(raw_upstream_tools)

    raw_gateway_tools = approval_tools.get("tools") or []
    if not isinstance(raw_gateway_tools, list):
        errors.append("C1: approval-tools.yaml 中的 tools 必须是列表")
        raw_gateway_tools = []
    gateway_tools: list[dict[str, Any]] = copy.deepcopy(raw_gateway_tools)

    # C1: tool name sets & disjointness
    upstream_names_list = [t.get("name") for t in upstream_tools if isinstance(t, dict)]
    gateway_names_list = [t.get("name") for t in gateway_tools if isinstance(t, dict)]

    if len(upstream_names_list) != len(set(upstream_names_list)):
        errors.append("C1: 上游工具列表存在重复的工具名")
    if len(gateway_names_list) != len(set(gateway_names_list)):
        errors.append("C1: 网关自有工具列表存在重复的工具名")

    upstream_names = {n for n in upstream_names_list if isinstance(n, str) and n}
    gateway_names = {n for n in gateway_names_list if isinstance(n, str) and n}
    openapi_names = set(openapi_ops.keys())

    if upstream_names != openapi_names:
        missing = sorted(openapi_names - upstream_names)
        extra = sorted(upstream_names - openapi_names)
        errors.append(
            f"C1: 上游工具集合与 OpenAPI operationId 不一致 (缺少: {missing}, 多余: {extra})"
        )

    overlap = sorted(upstream_names & gateway_names)
    if overlap:
        errors.append(f"C1: 上游工具与网关自有工具名称冲突: {overlap}")

    # C2: upstream requestTemplate matches OpenAPI method & url
    for tool in upstream_tools:
        name = tool.get("name")
        if name not in openapi_ops:
            continue
        expected_method, expected_url = openapi_ops[name]
        req_tpl = tool.get("requestTemplate") or {}
        actual_method = str(req_tpl.get("method") or "").upper()
        actual_url = req_tpl.get("url")
        if actual_method != expected_method or actual_url != expected_url:
            errors.append(
                f"C2: 上游工具 {name} 的 requestTemplate ({actual_method} {actual_url!r}) "
                f"与 OpenAPI ({expected_method} {expected_url!r}) 不一致"
            )

    # C6 & C8: routing & retry annotations
    _check_c6_c8_routing(ingress, mcpbridge, upstream_tools, errors)

    # C9 & C10: overrides and gateway-owned tools constraints
    overrides = approval_tools.get("overrides")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        errors.append("C9: approval-tools.yaml 的 overrides 必须是映射")
        overrides = {}

    write_tool_names: set[str] = set()
    for tool in upstream_tools + gateway_tools:
        name = tool.get("name")
        method = str((tool.get("requestTemplate") or {}).get("method") or "").upper()
        if isinstance(name, str) and method and method != "GET":
            write_tool_names.add(name)

    for write_name in sorted(write_tool_names):
        override_entry = overrides.get(write_name)
        if not isinstance(override_entry, dict):
            errors.append(f"C9: 写工具 {write_name} 在 approval-tools.yaml 的 overrides 中没有条目")
            continue
        override_desc = override_entry.get("description")
        if not isinstance(override_desc, str) or not override_desc.strip():
            errors.append(f"C9: overrides.{write_name} 缺少非空 description")

    for override_key in sorted(overrides.keys()):
        if override_key not in write_tool_names:
            errors.append(
                f"C10: overrides 中的键 {override_key!r} 不是现有的写工具（现有写工具: "
                f"{sorted(write_tool_names)}）"
            )

    for tool in gateway_tools:
        name = tool.get("name", "<unnamed>")
        req_tpl = tool.get("requestTemplate") or {}
        method = str(req_tpl.get("method") or "").upper()
        url = req_tpl.get("url") or ""
        parsed = urlparse(url) if isinstance(url, str) else None
        if method != "GET":
            errors.append(f"C10: 自有工具 {name} 的 HTTP 方法必须为 GET，实际为 {method!r}")
        if parsed is None or not parsed.scheme or not parsed.netloc:
            errors.append(f"C10: 自有工具 {name} 的 requestTemplate.url 非法: {url!r}")
        elif not parsed.path.startswith("/guard/v1/"):
            errors.append(
                f"C10: 自有工具 {name} 的路径必须以 '/guard/v1/' 开头，实际为 {parsed.path!r}"
            )

    # Apply Step 1: write-tool approval overrides (3.7.3)
    for tool in upstream_tools:
        name = tool.get("name")
        if name in overrides and isinstance(overrides[name], dict):
            desc = overrides[name].get("description")
            if isinstance(desc, str):
                tool["description"] = desc
            resp_tpl = tool.get("responseTemplate")
            if not isinstance(resp_tpl, dict):
                resp_tpl = {}
                tool["responseTemplate"] = resp_tpl
            resp_tpl["prependBody"] = approval_field_guide
            tool["outputSchema"] = copy.deepcopy(approval_schema)

    # Apply Step 2: complete gateway-owned tools and merge (3.7.3)
    completed_gateway_tools: list[dict[str, Any]] = []
    for tool in gateway_tools:
        tool.setdefault("security", {"id": "BearerAuth", "passthrough": True})
        req_tpl = tool.setdefault("requestTemplate", {})
        if isinstance(req_tpl, dict):
            req_tpl.setdefault("security", {"id": "BearerAuth"})
        resp_tpl = tool.setdefault("responseTemplate", {})
        if isinstance(resp_tpl, dict):
            resp_tpl.setdefault("prependBody", approval_field_guide)
        if "outputSchema" not in tool:
            if tool.get("name") == "list_approvals":
                tool["outputSchema"] = build_approval_list_schema(approval_schema)
            else:
                tool["outputSchema"] = copy.deepcopy(approval_schema)
        completed_gateway_tools.append(tool)

    merged_tools = sorted(
        upstream_tools + completed_gateway_tools,
        key=lambda t: str(t.get("name", "")),
    )

    # Confirm final merged tool name set matches C1
    final_names = {t.get("name") for t in merged_tools if isinstance(t, dict)}
    if final_names != (openapi_names | gateway_names) or len(merged_tools) != len(final_names):
        errors.append("C1: 合并后的工具集合与预期不一致")

    server_cfg = copy.deepcopy(raw_mcp.get("server") or {})
    _check_c3_c4_c5(server_cfg, merged_tools, errors)

    if errors:
        raise ContractError(errors)

    # Build mcpgate-mcp-server.yaml
    mcp_plugin = copy.deepcopy(mcp_plugin_tpl)
    mcp_plugin["spec"]["url"] = mcp_plugin["spec"]["url"].replace(
        "${MCP_SERVER_PLUGIN_VERSION}", config.mcp_server_plugin_version
    )
    mcp_plugin["spec"]["matchRules"][0]["config"] = {
        "server": server_cfg,
        "tools": merged_tools,
    }

    # Build mcpgate-jwt-auth.yaml
    jwt_plugin = copy.deepcopy(jwt_plugin_tpl)
    jwt_plugin["spec"]["url"] = jwt_plugin["spec"]["url"].replace(
        "${JWT_AUTH_PLUGIN_VERSION}", config.jwt_auth_plugin_version
    )
    consumer = jwt_plugin["spec"]["defaultConfig"]["consumers"][0]
    consumer["issuer"] = consumer["issuer"].replace("${MCPGATE_JWT_ISSUER}", config.jwt_issuer)
    consumer["jwks"] = jwks_text
    if isinstance(consumer.get("claims"), dict) and isinstance(consumer["claims"].get("aud"), str):
        consumer["claims"]["aud"] = consumer["claims"]["aud"].replace(
            "${MCPGATE_JWT_AUDIENCE}", config.jwt_audience
        )

    # Build tools.manifest.json
    manifest: list[dict[str, Any]] = []
    for tool in merged_tools:
        name = tool["name"]
        method = tool["requestTemplate"]["method"].upper()
        path = urlparse(tool["requestTemplate"]["url"]).path
        manifest.append(
            {
                "name": name,
                "method": method,
                "path": path,
                "write": method != "GET",
                "owner": "gateway" if name in gateway_names else "upstream",
            }
        )

    # All checks passed: write artifacts
    wasmplugins_dir = config.output_dir / "wasmplugins"
    wasmplugins_dir.mkdir(parents=True, exist_ok=True)

    mcp_out_path = wasmplugins_dir / "mcpgate-mcp-server.yaml"
    jwt_out_path = wasmplugins_dir / "mcpgate-jwt-auth.yaml"
    manifest_out_path = config.output_dir / "tools.manifest.json"

    mcp_out_path.write_text(dump_yaml(mcp_plugin), encoding="utf-8")
    jwt_out_path.write_text(dump_yaml(jwt_plugin), encoding="utf-8")
    manifest_out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "mcp_plugin": mcp_plugin,
        "jwt_plugin": jwt_plugin,
        "manifest": manifest,
        "outputs": [mcp_out_path, jwt_out_path, manifest_out_path],
    }


def build_default_config(args: argparse.Namespace) -> RenderConfig:
    gateway_dir = Path(__file__).resolve().parent.parent
    repo_root = gateway_dir.parent

    params = parse_env_file(gateway_dir / "params.env")
    root_env = parse_env_file(repo_root / ".env")

    def resolve_shared(key: str, default: str) -> str:
        val = os.environ.get(key)
        if val:
            return val
        val = root_env.get(key)
        if val:
            return val
        return default

    jwks_rel = args.jwks or params.get("JWKS_FILE", DEFAULT_JWKS_FILE)
    jwks_path = Path(jwks_rel)
    if not jwks_path.is_absolute():
        jwks_path = repo_root / jwks_path

    return RenderConfig(
        openapi_path=Path(args.openapi) if args.openapi else repo_root / "mock-api/openapi.json",
        raw_mcp_path=(
            Path(args.raw_mcp) if args.raw_mcp else gateway_dir / "generated/aftersale.mcp.yaml"
        ),
        approval_tools_path=(
            Path(args.approval_tools)
            if args.approval_tools
            else gateway_dir / "templates/approval-tools.yaml"
        ),
        mcp_plugin_template_path=(
            Path(args.mcp_plugin_template)
            if args.mcp_plugin_template
            else gateway_dir / "templates/wasmplugin-mcp-server.yaml"
        ),
        jwt_plugin_template_path=(
            Path(args.jwt_plugin_template)
            if args.jwt_plugin_template
            else gateway_dir / "templates/wasmplugin-jwt-auth.yaml"
        ),
        ingress_path=(
            Path(args.ingress)
            if args.ingress
            else gateway_dir / "resources/ingresses/aftersale-mcp.yaml"
        ),
        mcpbridge_path=(
            Path(args.mcpbridge)
            if args.mcpbridge
            else gateway_dir / "resources/mcpbridges/default.yaml"
        ),
        jwks_path=jwks_path,
        output_dir=Path(args.output_dir) if args.output_dir else gateway_dir / "generated",
        mcp_server_plugin_version=(
            args.mcp_server_plugin_version
            or os.environ.get("MCP_SERVER_PLUGIN_VERSION")
            or params.get("MCP_SERVER_PLUGIN_VERSION", DEFAULT_MCP_SERVER_PLUGIN_VERSION)
        ),
        jwt_auth_plugin_version=(
            args.jwt_auth_plugin_version
            or os.environ.get("JWT_AUTH_PLUGIN_VERSION")
            or params.get("JWT_AUTH_PLUGIN_VERSION", DEFAULT_JWT_AUTH_PLUGIN_VERSION)
        ),
        jwt_issuer=args.jwt_issuer or resolve_shared("MCPGATE_JWT_ISSUER", DEFAULT_JWT_ISSUER),
        jwt_audience=(
            args.jwt_audience or resolve_shared("MCPGATE_JWT_AUDIENCE", DEFAULT_JWT_AUDIENCE)
        ),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="合并审批工具、组装 WasmPlugin、执行 C1–C11 契约检查并输出 tools.manifest.json"
    )
    parser.add_argument("--openapi", help="OpenAPI JSON 路径")
    parser.add_argument("--raw-mcp", help="openapi-to-mcp 原始输出路径")
    parser.add_argument("--approval-tools", help="approval-tools.yaml 路径")
    parser.add_argument("--mcp-plugin-template", help="wasmplugin-mcp-server.yaml 路径")
    parser.add_argument("--jwt-plugin-template", help="wasmplugin-jwt-auth.yaml 路径")
    parser.add_argument("--ingress", help="aftersale-mcp Ingress 路径")
    parser.add_argument("--mcpbridge", help="default McpBridge 路径")
    parser.add_argument("--jwks", help="JWKS 文件路径")
    parser.add_argument("--output-dir", help="产物输出目录（默认 gateway/generated）")
    parser.add_argument("--mcp-server-plugin-version", help="mcp-server 插件版本")
    parser.add_argument("--jwt-auth-plugin-version", help="jwt-auth 插件版本")
    parser.add_argument("--jwt-issuer", help="JWT issuer")
    parser.add_argument("--jwt-audience", help="JWT audience")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = build_default_config(args)
    try:
        result = render_gateway(config)
    except ContractError as exc:
        print("契约检查失败，未写出任何产物：", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI guard
        print(f"渲染失败: {exc}", file=sys.stderr)
        return 1

    print(f"契约检查 C1–C11 全部通过，已生成 {len(result['manifest'])} 个工具配置：")
    for out in result["outputs"]:
        print(f"  - {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
