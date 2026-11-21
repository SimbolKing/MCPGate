from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parent.parent
GENERATE_SH = GATEWAY_DIR / "scripts/generate.sh"
APPLY_SH = GATEWAY_DIR / "scripts/apply.sh"


def _make_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _setup_sandbox_repo(tmp_path: Path, *, params_content: str | None) -> tuple[Path, Path]:
    """Create a minimal sandboxed repo layout for testing generate.sh / apply.sh."""
    repo_root = tmp_path / "repo"
    gw_scripts = repo_root / "gateway/scripts"
    gw_scripts.mkdir(parents=True)
    shutil.copy2(GENERATE_SH, gw_scripts / "generate.sh")
    shutil.copy2(APPLY_SH, gw_scripts / "apply.sh")
    if params_content is not None:
        (repo_root / "gateway/params.env").write_text(params_content, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return repo_root, bin_dir


def _run_bash(
    script: Path,
    args: list[str] | None = None,
    *,
    bin_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script), *(args or [])],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_shell_scripts_pass_bash_syntax_check() -> None:
    for script in (GENERATE_SH, APPLY_SH):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, f"{script.name} 语法检查失败: {res.stderr}"


def test_generate_sh_reports_missing_param_key_explicitly(tmp_path: Path) -> None:
    repo_root, _ = _setup_sandbox_repo(
        tmp_path,
        params_content=(
            "MCP_SERVER_NAME=aftersale\n"
            "MCP_SERVER_PLUGIN_VERSION=2.0.0\n"
            "JWT_AUTH_PLUGIN_VERSION=2.0.0\n"
            "OPENAPI_TO_MCP_REF=abc123\n"
            "GO_IMAGE=golang:1.24\n"
            # 故意缺失 JWKS_FILE
        ),
    )
    res = _run_bash(repo_root / "gateway/scripts/generate.sh")
    assert res.returncode == 1
    assert "缺少参数 JWKS_FILE" in res.stderr


@pytest.mark.parametrize(
    ("jwks_value", "is_absolute"),
    [
        ("jwt/keys/jwks.json", False),
        ("/etc/mcpgate/jwks.json", True),
    ],
)
def test_generate_sh_resolves_relative_and_absolute_jwks_file(
    tmp_path: Path,
    jwks_value: str,
    is_absolute: bool,
) -> None:
    repo_root, bin_dir = _setup_sandbox_repo(
        tmp_path,
        params_content=(
            "MCP_SERVER_NAME=aftersale\n"
            "MCP_SERVER_PLUGIN_VERSION=2.0.0\n"
            "JWT_AUTH_PLUGIN_VERSION=2.0.0\n"
            "OPENAPI_TO_MCP_REF=abc123\n"
            "GO_IMAGE=golang:1.24\n"
            f"JWKS_FILE={jwks_value}\n"
        ),
    )
    capture_file = tmp_path / "uv_args.txt"
    _make_executable(bin_dir / "docker", "#!/usr/bin/env bash\nexit 0\n")
    _make_executable(
        bin_dir / "uv",
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{capture_file}"\n'
        'out_dir=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--output-dir" ]]; then out_dir="$2"; shift 2; else shift; fi\n'
        "done\n"
        'mkdir -p "$out_dir/wasmplugins"\n'
        'touch "$out_dir/aftersale.mcp.yaml" "$out_dir/tools.manifest.json" \\\n'
        '  "$out_dir/wasmplugins/mcpgate-mcp-server.yaml" \\\n'
        '  "$out_dir/wasmplugins/mcpgate-jwt-auth.yaml"\n',
    )

    res = _run_bash(repo_root / "gateway/scripts/generate.sh", bin_dir=bin_dir)
    assert res.returncode == 0, res.stderr

    args = capture_file.read_text(encoding="utf-8").splitlines()
    jwks_idx = args.index("--jwks")
    actual_jwks = args[jwks_idx + 1]
    expected_jwks = jwks_value if is_absolute else str(repo_root.resolve() / jwks_value)
    assert actual_jwks == expected_jwks


def test_apply_sh_validates_params_file_and_missing_keys(tmp_path: Path) -> None:
    # 1. 缺少 params.env
    repo_no_file, _ = _setup_sandbox_repo(tmp_path / "no_file", params_content=None)
    res_missing_file = _run_bash(repo_no_file / "gateway/scripts/apply.sh")
    assert res_missing_file.returncode == 1
    assert "缺少参数文件" in res_missing_file.stderr

    # 2. 缺少 JWT_AUTH_PLUGIN_VERSION
    repo_no_key, _ = _setup_sandbox_repo(
        tmp_path / "no_key",
        params_content="MCP_SERVER_PLUGIN_VERSION=2.0.0\n",
    )
    res_missing_key = _run_bash(repo_no_key / "gateway/scripts/apply.sh")
    assert res_missing_key.returncode == 1
    assert "缺少参数 JWT_AUTH_PLUGIN_VERSION" in res_missing_key.stderr


def test_apply_sh_cli_flags_validation_and_help(tmp_path: Path) -> None:
    repo_root, _ = _setup_sandbox_repo(tmp_path, params_content=None)
    script = repo_root / "gateway/scripts/apply.sh"

    res_help = _run_bash(script, ["--help"])
    assert res_help.returncode == 0
    assert "--timeout" in res_help.stdout
    assert "--endpoint" in res_help.stdout

    for bad_args in (["--timeout", "0"], ["--timeout", "abc"], ["--endpoint="], ["--unknown"]):
        res = _run_bash(script, bad_args)
        assert res.returncode == 2, f"期望退出码 2，实际 {res.returncode} (args={bad_args})"


def _create_apply_resource_files(repo_root: Path) -> None:
    files = {
        "gateway/generated/wasmplugins/mcpgate-jwt-auth.yaml": (
            "metadata:\n  name: mcpgate-jwt-auth\n"
        ),
        "gateway/generated/wasmplugins/mcpgate-mcp-server.yaml": (
            "metadata:\n  name: mcpgate-mcp-server\n"
        ),
        "gateway/resources/mcpbridges/default.yaml": "metadata:\n  name: default\n",
        "gateway/resources/ingresses/aftersale-mcp.yaml": "metadata:\n  name: aftersale-mcp\n",
    }
    for rel, text in files.items():
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _install_fake_docker(bin_dir: Path) -> None:
    _make_executable(
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "compose" && "$2" == "ps" ]]; then echo "cid-123"; exit 0; fi\n'
        'if [[ "$1" == "inspect" ]]; then echo "healthy"; exit 0; fi\n'
        "exit 0\n",
    )


def test_apply_sh_ignores_unprefixed_env_and_uses_cli_args_and_curl_timeouts(
    tmp_path: Path,
) -> None:
    repo_root, bin_dir = _setup_sandbox_repo(
        tmp_path,
        params_content="MCP_SERVER_PLUGIN_VERSION=2.0.0\nJWT_AUTH_PLUGIN_VERSION=2.0.0\n",
    )
    _create_apply_resource_files(repo_root)
    _install_fake_docker(bin_dir)

    curl_args_file = tmp_path / "curl_args.txt"
    _make_executable(
        bin_dir / "curl",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{curl_args_file}"\nprintf "401"\nexit 0\n',
    )

    custom_ep = "http://127.0.0.1:18080/custom/mcp"
    res = _run_bash(
        repo_root / "gateway/scripts/apply.sh",
        ["--timeout", "10", "--endpoint", custom_ep],
        bin_dir=bin_dir,
        extra_env={
            "APPLY_TIMEOUT_SECONDS": "999",
            "MCP_ENDPOINT": "http://env-should-be-ignored.invalid/mcp",
        },
    )
    assert res.returncode == 0, res.stderr
    assert custom_ep in res.stdout
    assert "最长 10s" in res.stdout

    curl_args = curl_args_file.read_text(encoding="utf-8").splitlines()
    assert "--connect-timeout" in curl_args
    assert "--max-time" in curl_args
    assert custom_ep in curl_args
    assert "http://env-should-be-ignored.invalid/mcp" not in curl_args

    # 不传 CLI 参数时同样忽略无前缀环境变量，回退脚本常量默认值
    res_default = _run_bash(
        repo_root / "gateway/scripts/apply.sh",
        bin_dir=bin_dir,
        extra_env={
            "APPLY_TIMEOUT_SECONDS": "999",
            "MCP_ENDPOINT": "http://env-should-be-ignored.invalid/mcp",
        },
    )
    assert res_default.returncode == 0, res_default.stderr
    assert "http://127.0.0.1:8080/aftersale/mcp" in res_default.stdout
    assert "最长 60s" in res_default.stdout
    default_curl_args = curl_args_file.read_text(encoding="utf-8").splitlines()
    assert "http://127.0.0.1:8080/aftersale/mcp" in default_curl_args
    assert "http://env-should-be-ignored.invalid/mcp" not in default_curl_args


def test_apply_sh_curl_failure_prints_single_000_and_respects_timeout(tmp_path: Path) -> None:
    repo_root, bin_dir = _setup_sandbox_repo(
        tmp_path,
        params_content="MCP_SERVER_PLUGIN_VERSION=2.0.0\nJWT_AUTH_PLUGIN_VERSION=2.0.0\n",
    )
    _create_apply_resource_files(repo_root)
    _install_fake_docker(bin_dir)

    # 模拟真实 curl 连接失败：-w 输出 "000" 同时退出码为 7
    _make_executable(
        bin_dir / "curl",
        '#!/usr/bin/env bash\nprintf "000"\nexit 7\n',
    )

    start = time.monotonic()
    res = _run_bash(
        repo_root / "gateway/scripts/apply.sh",
        ["--timeout", "1"],
        bin_dir=bin_dir,
    )
    elapsed = time.monotonic() - start

    assert res.returncode == 1
    assert "最近一次探测 HTTP 状态码: 000" in res.stderr
    assert "000000" not in res.stderr
    # 两轮各 1 秒超时（首次等待 + 重启后等待），总耗时应远低于旧版固定 sleep
    assert elapsed < 6.0
