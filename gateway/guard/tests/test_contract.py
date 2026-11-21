from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.manifest import ToolManifest
from app.policy import WRITE_POLICIES
from app.schemas import AgentListApprovalsQuery, Approval, RefundArgs
from tests.conftest import GATEWAY_DIR, MANIFEST_PATH, REPO_ROOT

APPROVAL_TOOLS_PATH = GATEWAY_DIR / "templates/approval-tools.yaml"
OPENAPI_PATH = REPO_ROOT / "mock-api/openapi.json"


def _load_approval_tools() -> dict[str, Any]:
    return yaml.safe_load(APPROVAL_TOOLS_PATH.read_text(encoding="utf-8"))


def _load_openapi() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _collect_api_routes(routes: list[Any]) -> dict[tuple[str, str], APIRoute]:
    api_routes: dict[tuple[str, str], APIRoute] = {}
    for route in routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                api_routes[(method.upper(), route.path)] = route
        elif hasattr(route, "original_router"):
            api_routes.update(_collect_api_routes(list(route.original_router.routes)))
        elif hasattr(route, "routes"):
            api_routes.update(_collect_api_routes(list(route.routes)))
    return api_routes


def test_gateway_tools_match_guard_routes_and_params(app: FastAPI) -> None:
    approval_tools = _load_approval_tools()
    manifest = ToolManifest.from_file(MANIFEST_PATH)
    api_routes = _collect_api_routes(list(app.routes))

    # Every gateway-owned tool in approval-tools.yaml matches a concrete guard route
    for tool in approval_tools["tools"]:
        method = tool["requestTemplate"]["method"].upper()
        parsed_url = urlparse(tool["requestTemplate"]["url"])
        path = parsed_url.path
        assert (method, path) in api_routes, (
            f"Gateway tool {tool['name']} ({method} {path}) has no matching guard route"
        )
        route = api_routes[(method, path)]

        # Check path args match route path params; query args match AgentListApprovalsQuery
        for arg in tool.get("args", []):
            pos = arg["position"]
            name = arg["name"]
            if pos == "path":
                assert f"{{{name}}}" in route.path
            elif pos == "query":
                assert name in AgentListApprovalsQuery.model_fields, (
                    f"Query arg {name} of {tool['name']} missing from AgentListApprovalsQuery"
                )
            else:
                raise AssertionError(f"Unexpected arg position: {pos}")

    # Every gateway entry in tools.manifest.json exists in guard routes
    for entry in manifest.gateway_entries:
        assert (entry.method, entry.path) in api_routes


def test_approval_schema_matches_pydantic_model() -> None:
    approval_tools = _load_approval_tools()
    yaml_schema = approval_tools["approvalSchema"]
    model_schema = Approval.model_json_schema()

    yaml_props = set(yaml_schema["properties"].keys())
    model_props = set(model_schema["properties"].keys())
    assert yaml_props == model_props, (
        f"Approval properties mismatch: yaml-only={yaml_props - model_props}, "
        f"model-only={model_props - yaml_props}"
    )

    yaml_required = set(yaml_schema["required"])
    model_required = set(model_schema["required"])
    assert yaml_required == model_required, (
        f"Approval required mismatch: yaml={yaml_required}, model={model_required}"
    )

    # Enums match
    for enum_field in ("status", "region", "reason_category"):
        yaml_enum = set(yaml_schema["properties"][enum_field]["enum"])
        model_enum = set(model_schema["properties"][enum_field]["enum"])
        assert yaml_enum == model_enum, f"Enum mismatch on {enum_field}"


def test_refund_args_matches_mock_api_openapi() -> None:
    openapi = _load_openapi()
    post_refund = openapi["paths"]["/api/v1/refunds"]["post"]
    assert post_refund["operationId"] == "create_refund"

    api_schema = post_refund["requestBody"]["content"]["application/json"]["schema"]
    model_schema = RefundArgs.model_json_schema()

    api_props = set(api_schema["properties"].keys())
    model_props = set(model_schema["properties"].keys())
    assert api_props == model_props, (
        f"RefundArgs fields mismatch with mock-api OpenAPI: "
        f"openapi-only={api_props - model_props}, model-only={model_props - api_props}"
    )

    assert set(api_schema["required"]) == set(model_schema["required"])

    # Compare reason_category enum
    assert set(api_schema["properties"]["reason_category"]["enum"]) == set(
        model_schema["properties"]["reason_category"]["enum"]
    )

    # Compare key string/numeric constraints
    assert (
        api_schema["properties"]["reason"]["maxLength"]
        == model_schema["properties"]["reason"]["maxLength"]
    )
    assert (
        api_schema["properties"]["order_id"]["maxLength"]
        == model_schema["properties"]["order_id"]["maxLength"]
    )
    assert (
        api_schema["properties"]["idempotency_key"]["pattern"]
        == (model_schema["properties"]["idempotency_key"]["anyOf"][0]["pattern"])
    )


def test_every_upstream_write_tool_in_manifest_has_policy() -> None:
    manifest = ToolManifest.from_file(MANIFEST_PATH)
    write_tools = [entry for entry in manifest.upstream_entries if entry.write]
    assert len(write_tools) >= 1
    for entry in write_tools:
        assert entry.name in WRITE_POLICIES, (
            f"Upstream write tool {entry.name!r} is missing from app.policy.WRITE_POLICIES"
        )
        policy = WRITE_POLICIES[entry.name]
        assert policy.execute_method == entry.method
        assert policy.execute_path == entry.path
