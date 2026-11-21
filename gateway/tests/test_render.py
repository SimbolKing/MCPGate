from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import render

GATEWAY_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = GATEWAY_DIR.parent


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render.dump_yaml(data), encoding="utf-8")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sample_openapi() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test API", "version": "1.0.0"},
        "servers": [{"url": "http://mock-api:8000"}],
        "paths": {
            "/api/v1/orders/{order_id}": {
                "get": {"operationId": "get_order"},
            },
            "/api/v1/refunds": {
                "post": {"operationId": "create_refund"},
            },
        },
    }


def _sample_raw_mcp() -> dict[str, Any]:
    return {
        "server": {
            "name": "aftersale",
            "securitySchemes": [
                {"id": "BearerAuth", "type": "http", "scheme": "bearer"},
            ],
        },
        "tools": [
            {
                "name": "create_refund",
                "description": "原始描述",
                "args": [
                    {
                        "name": "order_id",
                        "description": "订单号",
                        "type": "string",
                        "required": True,
                        "position": "body",
                    }
                ],
                "requestTemplate": {
                    "url": "http://mock-api:8000/api/v1/refunds",
                    "method": "POST",
                    "headers": [{"key": "Content-Type", "value": "application/json"}],
                    "security": {"id": "BearerAuth"},
                },
                "responseTemplate": {"prependBody": "# Old\n"},
                "security": {"id": "BearerAuth", "passthrough": True},
                "outputSchema": {"type": "object", "properties": {"refund_id": {"type": "string"}}},
            },
            {
                "name": "get_order",
                "description": "查询订单",
                "args": [
                    {
                        "name": "order_id",
                        "description": "订单号",
                        "type": "string",
                        "required": True,
                        "position": "path",
                    }
                ],
                "requestTemplate": {
                    "url": "http://mock-api:8000/api/v1/orders/{order_id}",
                    "method": "GET",
                    "security": {"id": "BearerAuth"},
                },
                "responseTemplate": {"prependBody": "# Order\n"},
                "security": {"id": "BearerAuth", "passthrough": True},
                "outputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            },
        ],
    }


@pytest.fixture
def fixture_env(tmp_path: Path) -> tuple[render.RenderConfig, dict[str, Any]]:
    """Build an isolated valid fixture set in tmp_path for testing render_gateway."""
    openapi = _sample_openapi()
    raw_mcp = _sample_raw_mcp()
    approval_tools = yaml.safe_load(
        (GATEWAY_DIR / "templates/approval-tools.yaml").read_text(encoding="utf-8")
    )
    mcp_tpl = yaml.safe_load(
        (GATEWAY_DIR / "templates/wasmplugin-mcp-server.yaml").read_text(encoding="utf-8")
    )
    jwt_tpl = yaml.safe_load(
        (GATEWAY_DIR / "templates/wasmplugin-jwt-auth.yaml").read_text(encoding="utf-8")
    )
    ingress = yaml.safe_load(
        (GATEWAY_DIR / "resources/ingresses/aftersale-mcp.yaml").read_text(encoding="utf-8")
    )
    mcpbridge = yaml.safe_load(
        (GATEWAY_DIR / "resources/mcpbridges/default.yaml").read_text(encoding="utf-8")
    )
    jwks = json.loads((REPO_ROOT / "jwt/keys/jwks.json").read_text(encoding="utf-8"))

    paths = {
        "openapi": tmp_path / "openapi.json",
        "raw_mcp": tmp_path / "aftersale.mcp.yaml",
        "approval_tools": tmp_path / "approval-tools.yaml",
        "mcp_tpl": tmp_path / "wasmplugin-mcp-server.yaml",
        "jwt_tpl": tmp_path / "wasmplugin-jwt-auth.yaml",
        "ingress": tmp_path / "aftersale-mcp.yaml",
        "mcpbridge": tmp_path / "default.yaml",
        "jwks": tmp_path / "jwks.json",
        "output_dir": tmp_path / "out",
    }

    _write_json(paths["openapi"], openapi)
    _write_yaml(paths["raw_mcp"], raw_mcp)
    _write_yaml(paths["approval_tools"], approval_tools)
    _write_yaml(paths["mcp_tpl"], mcp_tpl)
    _write_yaml(paths["jwt_tpl"], jwt_tpl)
    _write_yaml(paths["ingress"], ingress)
    _write_yaml(paths["mcpbridge"], mcpbridge)
    _write_json(paths["jwks"], jwks)

    cfg = render.RenderConfig(
        openapi_path=paths["openapi"],
        raw_mcp_path=paths["raw_mcp"],
        approval_tools_path=paths["approval_tools"],
        mcp_plugin_template_path=paths["mcp_tpl"],
        jwt_plugin_template_path=paths["jwt_tpl"],
        ingress_path=paths["ingress"],
        mcpbridge_path=paths["mcpbridge"],
        jwks_path=paths["jwks"],
        output_dir=paths["output_dir"],
    )
    data = {
        "paths": paths,
        "openapi": openapi,
        "raw_mcp": raw_mcp,
        "approval_tools": approval_tools,
        "ingress": ingress,
        "mcpbridge": mcpbridge,
        "jwks": jwks,
    }
    return cfg, data


def _assert_fails_with(cfg: render.RenderConfig, code: str) -> None:
    with pytest.raises(render.ContractError) as exc_info:
        render.render_gateway(cfg)
    assert any(err.startswith(f"{code}:") for err in exc_info.value.errors), (
        f"Expected {code} error, got: {exc_info.value.errors}"
    )
    # Atomicity: no artifacts written when any contract check fails
    assert not cfg.output_dir.exists()


def test_render_happy_path_and_determinism(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    result = render.render_gateway(cfg)

    names = [item["name"] for item in result["manifest"]]
    assert names == ["create_refund", "get_approval", "get_order", "list_approvals"]

    mcp_yaml_path = cfg.output_dir / "wasmplugins/mcpgate-mcp-server.yaml"
    jwt_yaml_path = cfg.output_dir / "wasmplugins/mcpgate-jwt-auth.yaml"
    manifest_path = cfg.output_dir / "tools.manifest.json"
    assert mcp_yaml_path.exists()
    assert jwt_yaml_path.exists()
    assert manifest_path.exists()

    mcp_text_1 = mcp_yaml_path.read_text(encoding="utf-8")
    jwt_text_1 = jwt_yaml_path.read_text(encoding="utf-8")
    manifest_text_1 = manifest_path.read_text(encoding="utf-8")

    # No YAML anchors/aliases emitted
    assert "&id" not in mcp_text_1
    assert "*id" not in mcp_text_1

    # Verify write-tool override applied and original requestTemplate/args untouched
    mcp_doc = yaml.safe_load(mcp_text_1)
    tools_by_name = {t["name"]: t for t in mcp_doc["spec"]["matchRules"][0]["config"]["tools"]}
    cr = tools_by_name["create_refund"]
    assert cr["description"] == data["approval_tools"]["overrides"]["create_refund"]["description"]
    assert cr["responseTemplate"]["prependBody"] == data["approval_tools"]["approvalFieldGuide"]
    assert cr["outputSchema"] == data["approval_tools"]["approvalSchema"]
    assert cr["requestTemplate"]["method"] == "POST"
    assert cr["requestTemplate"]["url"] == "http://mock-api:8000/api/v1/refunds"
    assert cr["security"] == {"id": "BearerAuth", "passthrough": True}

    # Verify gateway-owned tools completed properly
    ga = tools_by_name["get_approval"]
    assert ga["security"] == {"id": "BearerAuth", "passthrough": True}
    assert ga["requestTemplate"]["security"] == {"id": "BearerAuth"}
    assert ga["outputSchema"] == data["approval_tools"]["approvalSchema"]

    la = tools_by_name["list_approvals"]
    assert la["outputSchema"]["required"] == ["items", "total", "page", "page_size"]
    assert la["outputSchema"]["properties"]["items"]["type"] == "array"
    assert (
        la["outputSchema"]["properties"]["items"]["items"]
        == (data["approval_tools"]["approvalSchema"])
    )

    # Verify jwt-auth preserves trailing space in value_prefix and valid JSON jwks
    jwt_doc = yaml.safe_load(jwt_text_1)
    consumer = jwt_doc["spec"]["defaultConfig"]["consumers"][0]
    assert consumer["issuer"] == "http://dev-idp:8010"
    assert consumer["claims"] == {"aud": "mcpgate"}
    assert consumer["from_headers"] == [{"name": "Authorization", "value_prefix": "Bearer "}]
    assert consumer["clock_skew_seconds"] == 60
    assert consumer["keep_token"] is True
    assert json.loads(consumer["jwks"]) == data["jwks"]

    # Deterministic re-render produces byte-for-byte identical files
    render.render_gateway(cfg)
    assert mcp_yaml_path.read_text(encoding="utf-8") == mcp_text_1
    assert jwt_yaml_path.read_text(encoding="utf-8") == jwt_text_1
    assert manifest_path.read_text(encoding="utf-8") == manifest_text_1


def test_c1_missing_tool_in_raw_mcp(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    raw = copy.deepcopy(data["raw_mcp"])
    raw["tools"] = [t for t in raw["tools"] if t["name"] != "get_order"]
    _write_yaml(data["paths"]["raw_mcp"], raw)
    _assert_fails_with(cfg, "C1")


def test_c1_gateway_and_upstream_name_collision(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    approval = copy.deepcopy(data["approval_tools"])
    approval["tools"][0]["name"] = "get_order"
    _write_yaml(data["paths"]["approval_tools"], approval)
    _assert_fails_with(cfg, "C1")


def test_c2_method_or_url_mismatch(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    raw = copy.deepcopy(data["raw_mcp"])
    raw["tools"][1]["requestTemplate"]["url"] = "http://mock-api:8000/api/v1/orders/wrong"
    _write_yaml(data["paths"]["raw_mcp"], raw)
    _assert_fails_with(cfg, "C2")


def test_c3_missing_passthrough_or_scheme(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    raw = copy.deepcopy(data["raw_mcp"])
    raw["tools"][1]["security"]["passthrough"] = False
    _write_yaml(data["paths"]["raw_mcp"], raw)
    _assert_fails_with(cfg, "C3")

    raw2 = copy.deepcopy(data["raw_mcp"])
    raw2["server"]["securitySchemes"] = []
    _write_yaml(data["paths"]["raw_mcp"], raw2)
    _assert_fails_with(cfg, "C3")


def test_c4_forbids_default_credential_and_tool_credential(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    raw = copy.deepcopy(data["raw_mcp"])
    raw["server"]["securitySchemes"][0]["defaultCredential"] = "secret"
    _write_yaml(data["paths"]["raw_mcp"], raw)
    _assert_fails_with(cfg, "C4")

    raw2 = copy.deepcopy(data["raw_mcp"])
    raw2["tools"][0]["security"]["credential"] = "hardcoded"
    _write_yaml(data["paths"]["raw_mcp"], raw2)
    _assert_fails_with(cfg, "C4")


def test_c5_arg_description_type_and_no_default(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    raw = copy.deepcopy(data["raw_mcp"])
    raw["tools"][1]["args"][0]["default"] = "A1023"
    _write_yaml(data["paths"]["raw_mcp"], raw)
    _assert_fails_with(cfg, "C5")

    raw2 = copy.deepcopy(data["raw_mcp"])
    raw2["tools"][1]["args"][0]["description"] = "   "
    _write_yaml(data["paths"]["raw_mcp"], raw2)
    _assert_fails_with(cfg, "C5")


def test_c6_ingress_destination_and_single_upstream(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    ingress = copy.deepcopy(data["ingress"])
    ingress["metadata"]["annotations"]["higress.io/destination"] = "mock-api.dns:8000"
    _write_yaml(data["paths"]["ingress"], ingress)
    _assert_fails_with(cfg, "C6")

    # Restore ingress, break McpBridge domain (must contain a dot)
    _write_yaml(data["paths"]["ingress"], data["ingress"])
    bridge = copy.deepcopy(data["mcpbridge"])
    bridge["spec"]["registries"][1]["domain"] = "guard"
    _write_yaml(data["paths"]["mcpbridge"], bridge)
    _assert_fails_with(cfg, "C6")

    # Restore bridge, split upstream tools across two hosts
    _write_yaml(data["paths"]["mcpbridge"], data["mcpbridge"])
    raw = copy.deepcopy(data["raw_mcp"])
    raw["tools"][1]["requestTemplate"]["url"] = "http://other-host:8000/api/v1/orders/{order_id}"
    _write_yaml(data["paths"]["raw_mcp"], raw)
    _assert_fails_with(cfg, "C6")


def test_c7_jwks_validation(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    # Empty keys
    _write_json(data["paths"]["jwks"], {"keys": []})
    _assert_fails_with(cfg, "C7")

    # Wrong algorithm
    bad_alg = copy.deepcopy(data["jwks"])
    bad_alg["keys"][0]["alg"] = "HS256"
    _write_json(data["paths"]["jwks"], bad_alg)
    _assert_fails_with(cfg, "C7")

    # Leaked private key component
    leaked_priv = copy.deepcopy(data["jwks"])
    leaked_priv["keys"][0]["d"] = "PRIVATE"
    _write_json(data["paths"]["jwks"], leaked_priv)
    _assert_fails_with(cfg, "C7")


def test_c8_ingress_retry_annotation(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    ingress = copy.deepcopy(data["ingress"])
    ingress["metadata"]["annotations"]["higress.io/proxy-next-upstream-tries"] = "1"
    _write_yaml(data["paths"]["ingress"], ingress)
    _assert_fails_with(cfg, "C8")

    ingress2 = copy.deepcopy(data["ingress"])
    del ingress2["metadata"]["annotations"]["higress.io/proxy-next-upstream-tries"]
    _write_yaml(data["paths"]["ingress"], ingress2)
    _assert_fails_with(cfg, "C8")


def test_c9_write_tool_requires_override(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    approval = copy.deepcopy(data["approval_tools"])
    approval["overrides"] = {}
    _write_yaml(data["paths"]["approval_tools"], approval)
    _assert_fails_with(cfg, "C9")


def test_c10_override_must_be_write_tool_and_gateway_tools_read_only(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    # Override referencing a GET read tool
    approval = copy.deepcopy(data["approval_tools"])
    approval["overrides"]["get_order"] = {"description": "should fail"}
    _write_yaml(data["paths"]["approval_tools"], approval)
    _assert_fails_with(cfg, "C10")

    # Gateway tool with non-GET method
    approval2 = copy.deepcopy(data["approval_tools"])
    approval2["tools"][0]["requestTemplate"]["method"] = "POST"
    _write_yaml(data["paths"]["approval_tools"], approval2)
    _assert_fails_with(cfg, "C10")

    # Gateway tool outside /guard/v1/
    approval3 = copy.deepcopy(data["approval_tools"])
    approval3["tools"][0]["requestTemplate"]["url"] = (
        "http://guard:8020/api/v1/approvals/{approval_id}"
    )
    _write_yaml(data["paths"]["approval_tools"], approval3)
    _assert_fails_with(cfg, "C10")


def test_c11_approval_schema_self_consistency(
    fixture_env: tuple[render.RenderConfig, dict[str, Any]],
) -> None:
    cfg, data = fixture_env
    approval = copy.deepcopy(data["approval_tools"])
    approval["approvalSchema"]["required"].append("nonexistent_field")
    _write_yaml(data["paths"]["approval_tools"], approval)
    _assert_fails_with(cfg, "C11")

    approval2 = copy.deepcopy(data["approval_tools"])
    approval2["approvalSchema"]["type"] = "array"
    _write_yaml(data["paths"]["approval_tools"], approval2)
    _assert_fails_with(cfg, "C11")


def test_parse_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "MCPGATE_JWT_ISSUER=http://custom-idp:9000\n"
        'export MCPGATE_JWT_AUDIENCE="custom-aud"\n'
        "SINGLE='val'\n",
        encoding="utf-8",
    )
    parsed = render.parse_env_file(env_file)
    assert parsed == {
        "MCPGATE_JWT_ISSUER": "http://custom-idp:9000",
        "MCPGATE_JWT_AUDIENCE": "custom-aud",
        "SINGLE": "val",
    }


def test_generated_artifacts_match_full_contract() -> None:
    """Verify the checked-in gateway/generated/ artifacts match PROPOSAL.md Chapter 2 & 3.7."""
    manifest_path = GATEWAY_DIR / "generated/tools.manifest.json"
    mcp_path = GATEWAY_DIR / "generated/wasmplugins/mcpgate-mcp-server.yaml"
    jwt_path = GATEWAY_DIR / "generated/wasmplugins/mcpgate-jwt-auth.yaml"
    raw_mcp_path = GATEWAY_DIR / "generated/aftersale.mcp.yaml"

    assert raw_mcp_path.exists(), "请先运行 gateway/scripts/generate.sh 生成产物"
    assert manifest_path.exists()
    assert mcp_path.exists()
    assert jwt_path.exists()

    expected_manifest = [
        {
            "name": "create_refund",
            "method": "POST",
            "path": "/api/v1/refunds",
            "write": True,
            "owner": "upstream",
        },
        {
            "name": "get_approval",
            "method": "GET",
            "path": "/guard/v1/approvals/{approval_id}",
            "write": False,
            "owner": "gateway",
        },
        {
            "name": "get_order",
            "method": "GET",
            "path": "/api/v1/orders/{order_id}",
            "write": False,
            "owner": "upstream",
        },
        {
            "name": "get_refund",
            "method": "GET",
            "path": "/api/v1/refunds/{refund_id}",
            "write": False,
            "owner": "upstream",
        },
        {
            "name": "list_approvals",
            "method": "GET",
            "path": "/guard/v1/approvals",
            "write": False,
            "owner": "gateway",
        },
        {
            "name": "list_orders",
            "method": "GET",
            "path": "/api/v1/orders",
            "write": False,
            "owner": "upstream",
        },
        {
            "name": "list_refunds",
            "method": "GET",
            "path": "/api/v1/refunds",
            "write": False,
            "owner": "upstream",
        },
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == expected_manifest

    mcp_doc = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))
    assert mcp_doc["metadata"]["name"] == "mcpgate-mcp-server"
    assert mcp_doc["metadata"]["namespace"] == "higress-system"
    assert mcp_doc["spec"]["url"] == "http://localhost:8002/plugins/mcp-server/2.0.3/plugin.wasm"
    assert mcp_doc["spec"]["phase"] == "UNSPECIFIED_PHASE"
    assert mcp_doc["spec"]["priority"] == 30
    assert mcp_doc["spec"]["failStrategy"] == "FAIL_CLOSE"
    assert mcp_doc["spec"]["defaultConfigDisable"] is True
    rule = mcp_doc["spec"]["matchRules"][0]
    assert rule["ingress"] == ["aftersale-mcp"]
    assert rule["configDisable"] is False
    assert rule["config"]["server"]["name"] == "aftersale"
    tools = rule["config"]["tools"]
    assert [t["name"] for t in tools] == [m["name"] for m in expected_manifest]

    # Verify Approval schema covers all 22 fields from PROPOSAL 3.7.1
    create_refund = tools[0]
    assert "【写操作】" in create_refund["description"]
    assert "审批" in create_refund["description"]
    assert len(create_refund["outputSchema"]["properties"]) == 22
    assert set(create_refund["outputSchema"]["required"]) == {
        "approval_id",
        "status",
        "message",
        "tool",
        "order_id",
        "region",
        "amount",
        "reason_category",
        "reason",
        "applicant_sub",
        "submitted_at",
        "expires_at",
    }

    jwt_doc = yaml.safe_load(jwt_path.read_text(encoding="utf-8"))
    assert jwt_doc["metadata"]["name"] == "mcpgate-jwt-auth"
    assert jwt_doc["spec"]["url"] == "http://localhost:8002/plugins/jwt-auth/2.0.0/plugin.wasm"
    assert jwt_doc["spec"]["phase"] == "AUTHN"
    assert jwt_doc["spec"]["priority"] == 340
    assert jwt_doc["spec"]["failStrategy"] == "FAIL_CLOSE"
    assert jwt_doc["spec"]["defaultConfigDisable"] is False
    assert jwt_doc["spec"]["defaultConfig"]["global_auth"] is False
    consumer = jwt_doc["spec"]["defaultConfig"]["consumers"][0]
    assert consumer["name"] == "idp"
    assert consumer["issuer"] == "http://dev-idp:8010"
    assert consumer["claims"] == {"aud": "mcpgate"}
    assert consumer["from_headers"] == [{"name": "Authorization", "value_prefix": "Bearer "}]
    assert consumer["clock_skew_seconds"] == 60
    assert consumer["keep_token"] is True
    assert json.loads(consumer["jwks"]) == json.loads(
        (REPO_ROOT / "jwt/keys/jwks.json").read_text(encoding="utf-8")
    )
    assert jwt_doc["spec"]["matchRules"] == [
        {
            "ingress": ["aftersale-mcp"],
            "configDisable": False,
            "config": {"allow": ["idp"]},
        }
    ]

    # Verify all 4 Higress resources have metadata.name matching filename stem and higress-system ns
    resource_files = [
        GATEWAY_DIR / "generated/wasmplugins/mcpgate-jwt-auth.yaml",
        GATEWAY_DIR / "generated/wasmplugins/mcpgate-mcp-server.yaml",
        GATEWAY_DIR / "resources/mcpbridges/default.yaml",
        GATEWAY_DIR / "resources/ingresses/aftersale-mcp.yaml",
    ]
    for path in resource_files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["metadata"]["name"] == path.stem
        assert doc["metadata"]["namespace"] == "higress-system"
        assert doc["metadata"]["creationTimestamp"] == "2000-01-01T00:00:00Z"
        assert doc["metadata"]["resourceVersion"] == "1"
