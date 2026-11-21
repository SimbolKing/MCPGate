from __future__ import annotations

import ast
import io
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import e2e
import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent
OVERRIDE_PATH = WORKFLOWS_DIR / "dify" / "compose.override.yaml"
FETCH_SCRIPT = WORKFLOWS_DIR / "dify" / "fetch.sh"

# Dify 1.20.0 官方 docker-compose.yaml 中已显式声明 networks 的被覆盖服务白名单
OFFICIAL_SERVICES_WITH_NETWORKS: frozenset[str] = frozenset({"ssrf_proxy"})


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader supporting Docker Compose `!override` tag."""


def _construct_override(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_override)


# ---------------------------------------------------------------------------
# H1: Dify compose.override.yaml networks static check
# ---------------------------------------------------------------------------


def test_compose_override_networks_include_default_for_services_without_official_networks() -> None:
    doc = yaml.load(OVERRIDE_PATH.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert isinstance(doc, dict)
    services = doc.get("services")
    assert isinstance(services, dict)

    for svc_name, svc_cfg in services.items():
        if not isinstance(svc_cfg, dict) or "networks" not in svc_cfg:
            continue
        if svc_name in OFFICIAL_SERVICES_WITH_NETWORKS:
            continue
        networks = svc_cfg["networks"]
        if isinstance(networks, dict):
            has_default = "default" in networks
        elif isinstance(networks, list):
            has_default = "default" in networks
        else:
            has_default = False
        assert has_default, (
            f"compose.override.yaml 服务 {svc_name!r} 声明了 networks 但缺少 default，"
            "会被移出 Dify 默认网络导致内部解析失败"
        )

    nginx_nets = services["nginx"]["networks"]
    assert isinstance(nginx_nets, dict)
    assert "default" in nginx_nets
    assert "mcpgate" in nginx_nets
    assert "dify.internal" in (nginx_nets["mcpgate"].get("aliases") or [])

    ssrf_nets = services["ssrf_proxy"]["networks"]
    assert isinstance(ssrf_nets, dict)
    assert "mcpgate" in ssrf_nets


# ---------------------------------------------------------------------------
# M8: e2e.py E16 negative-first verdict classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("结论：可退。\n依据：已签收 7 天，满足第 2.1 条。", "refundable"),
        ("结论：可以退货，建议办理退货退款。\n依据：会员第 6.1 条。", "refundable"),
        ("支持退货，按《售后制度》第 2.1 条办理。", "refundable"),
        ("结论：同意退货退款。", "refundable"),
        ("结论：不可退。\n依据：生鲜食品不支持无理由退货（第 4.1 条）。", "not_refundable"),
        ("结论：不能退货。", "not_refundable"),
        ("结论：不可以退货，已超过无理由期限。", "not_refundable"),
        ("结论：不同意退款申请。", "not_refundable"),
        ("结论：不得退货。", "not_refundable"),
        ("结论：不应退款。", "not_refundable"),
        ("结论：不满足退货条件。", "not_refundable"),
        ("结论：不符合退货规定。", "not_refundable"),
        ("结论：不符合退款条件。", "not_refundable"),
        ("结论：不予受理。", "not_refundable"),
        ("结论：无法退货。", "not_refundable"),
        ("结论：没有可退款项。", "not_refundable"),
        ("结论：需补充信息，先确认客户诉求。", "need_info"),
        ("结论：建议请示主管处理。", "escalate"),
        ("结论：可退，但不可退运费。", "unknown"),
    ],
)
def test_e2e_classify_verdict(answer: str, expected: str) -> None:
    assert e2e.classify_verdict(answer) == expected


def test_l8_e2e_and_evals_verdict_keywords_are_identical() -> None:
    """L8: workflows/scripts/e2e.py 与 evals/harness/asserts.py 的结论关键词表必须逐项一致。"""
    asserts_path = WORKFLOWS_DIR.parent / "evals" / "harness" / "asserts.py"
    tree = ast.parse(asserts_path.read_text(encoding="utf-8"), filename=str(asserts_path))
    target_names = (
        "ESCALATE_KEYWORDS",
        "NEED_INFO_KEYWORDS",
        "NEGATIVE_KEYWORDS",
        "POSITIVE_KEYWORDS",
    )
    evals_tables: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in target_names and node.value is not None:
                evals_tables[node.target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in target_names:
                    evals_tables[tgt.id] = ast.literal_eval(node.value)

    assert set(evals_tables) == set(target_names)
    for name in target_names:
        assert getattr(e2e, name) == evals_tables[name], f"{name} 在 e2e.py 与 asserts.py 不一致"


# ---------------------------------------------------------------------------
# L5: workflows/dify/fetch.sh real behavioral tests (offline via DIFY_SOURCE)
# ---------------------------------------------------------------------------


def _build_dify_tarball(
    archive_path: Path,
    *,
    compose_content: str = "services: {}\n",
    include_ssrf_proxy: bool = True,
) -> Path:
    """Create a minimal local dify-1.20.0.tar.gz archive for offline fetch.sh tests."""
    entries: dict[str, bytes] = {
        "dify-1.20.0/docker/docker-compose.yaml": compose_content.encode("utf-8"),
        "dify-1.20.0/docker/.env.example": b"KEY=value\n",
        "dify-1.20.0/docker/nginx/nginx.conf": b"events {}\n",
    }
    if include_ssrf_proxy:
        entries["dify-1.20.0/docker/ssrf_proxy/squid.conf"] = b"http_port 3128\n"

    with tarfile.open(archive_path, "w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive_path


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _seed_existing_docker(work_dir: Path) -> Path:
    docker_dir = work_dir / "docker"
    (docker_dir / "volumes" / "db").mkdir(parents=True)
    (docker_dir / "nginx").mkdir(parents=True)
    (docker_dir / "ssrf_proxy").mkdir(parents=True)
    (docker_dir / "docker-compose.yaml").write_text("services:\n  old: {}\n", encoding="utf-8")
    (docker_dir / ".env.example").write_text("OLD_EXAMPLE=1\n", encoding="utf-8")
    (docker_dir / ".env").write_text("KEEP=1\nSECRET=abc\n", encoding="utf-8")
    (docker_dir / "volumes" / "db" / "data.txt").write_text("volume-state\n", encoding="utf-8")
    return docker_dir


def test_fetch_sh_syntax_and_basic_guards(tmp_path: Path) -> None:
    r_syntax = subprocess.run(["bash", "-n", str(FETCH_SCRIPT)], capture_output=True, text=True)
    assert r_syntax.returncode == 0, r_syntax.stderr

    work_dir = tmp_path / "dify"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)

    # --help documents --force and --clean
    r_help = subprocess.run(["bash", str(script_copy), "--help"], capture_output=True, text=True)
    assert r_help.returncode == 0
    assert "--force" in r_help.stdout
    assert "--clean" in r_help.stdout

    # --clean without --force fails immediately
    r_clean_only = subprocess.run(
        ["bash", str(script_copy), "--clean"], capture_output=True, text=True
    )
    assert r_clean_only.returncode != 0

    # Existing docker/ without --force fails without modifying docker/
    docker_dir = _seed_existing_docker(work_dir)
    r_exists = subprocess.run(["bash", str(script_copy)], capture_output=True, text=True)
    assert r_exists.returncode != 0
    assert (docker_dir / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"


def test_fetch_sh_force_extraction_failure_leaves_docker_untouched(tmp_path: Path) -> None:
    work_dir = tmp_path / "dify"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)
    docker_dir = _seed_existing_docker(work_dir)
    _write_executable(work_dir / "dify.sh", "#!/usr/bin/env bash\nexit 0\n")

    # 1. Corrupted tarball fails during extraction -> docker/ untouched, no backup
    bad_tar = tmp_path / "corrupt.tar.gz"
    bad_tar.write_bytes(b"not-a-valid-gzip-archive")
    r_corrupt = subprocess.run(
        ["bash", str(script_copy), "--force"],
        capture_output=True,
        text=True,
        env={**os.environ, "DIFY_SOURCE": str(bad_tar)},
    )
    assert r_corrupt.returncode != 0
    assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  old: {}\n"
    )
    assert (docker_dir / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"
    assert (docker_dir / "volumes" / "db" / "data.txt").read_text(encoding="utf-8") == (
        "volume-state\n"
    )
    assert list(work_dir.glob("docker.bak.*")) == []

    # 2. Incomplete tarball (missing ssrf_proxy/) fails validation -> docker/ untouched, no backup
    incomplete_tar = _build_dify_tarball(
        tmp_path / "incomplete.tar.gz",
        compose_content="services:\n  new: {}\n",
        include_ssrf_proxy=False,
    )
    r_incomplete = subprocess.run(
        ["bash", str(script_copy), "--force"],
        capture_output=True,
        text=True,
        env={**os.environ, "DIFY_SOURCE": str(incomplete_tar)},
    )
    assert r_incomplete.returncode != 0
    assert "ssrf_proxy" in r_incomplete.stderr
    assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  old: {}\n"
    )
    assert list(work_dir.glob("docker.bak.*")) == []


def test_fetch_sh_force_migrates_volumes_and_env_and_backs_up(tmp_path: Path) -> None:
    work_dir = tmp_path / "dify"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)
    docker_dir = _seed_existing_docker(work_dir)
    _write_executable(work_dir / "dify.sh", "#!/usr/bin/env bash\nexit 0\n")

    good_tar = _build_dify_tarball(
        tmp_path / "dify-1.20.0.tar.gz",
        compose_content="services:\n  new_version: {}\n",
    )
    r_ok = subprocess.run(
        ["bash", str(script_copy), "--force"],
        capture_output=True,
        text=True,
        env={**os.environ, "DIFY_SOURCE": str(good_tar)},
    )
    assert r_ok.returncode == 0, r_ok.stderr

    # New docker/ is installed, volumes/ and .env migrated back
    assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  new_version: {}\n"
    )
    assert (docker_dir / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"
    assert (docker_dir / "volumes" / "db" / "data.txt").read_text(encoding="utf-8") == (
        "volume-state\n"
    )

    # Backup directory exists with old compose file and backup copy of .env
    backups = list(work_dir.glob("docker.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  old: {}\n"
    )
    assert (backups[0] / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"


def test_fetch_sh_force_clean_does_not_migrate_volumes_or_env(tmp_path: Path) -> None:
    work_dir = tmp_path / "dify"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)
    docker_dir = _seed_existing_docker(work_dir)
    _write_executable(work_dir / "dify.sh", "#!/usr/bin/env bash\nexit 0\n")

    good_tar = _build_dify_tarball(
        tmp_path / "dify-1.20.0.tar.gz",
        compose_content="services:\n  clean_version: {}\n",
    )
    r_clean = subprocess.run(
        ["bash", str(script_copy), "--force", "--clean"],
        capture_output=True,
        text=True,
        env={**os.environ, "DIFY_SOURCE": str(good_tar)},
    )
    assert r_clean.returncode == 0, r_clean.stderr

    assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  clean_version: {}\n"
    )
    assert not (docker_dir / ".env").exists()
    assert not (docker_dir / "volumes").exists()

    backups = list(work_dir.glob("docker.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"
    assert (backups[0] / "volumes" / "db" / "data.txt").read_text(encoding="utf-8") == (
        "volume-state\n"
    )


def test_fetch_sh_force_rejects_running_containers_and_rolls_back_on_swap_error(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "dify"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)
    docker_dir = _seed_existing_docker(work_dir)

    good_tar = _build_dify_tarball(
        tmp_path / "dify-1.20.0.tar.gz",
        compose_content="services:\n  new_version: {}\n",
    )

    # 1. Running / restarting / paused containers reported by dify.sh ps -> refuse --force
    for active_output, expected_state in [
        ("running\n", "running"),
        ("exited\nrestarting\n", "restarting"),
        ("paused\n", "paused"),
    ]:
        _write_executable(
            work_dir / "dify.sh",
            (
                "#!/usr/bin/env bash\n"
                'if [[ "$1" == "ps" ]]; then\n'
                f"  printf '%s' {active_output!r}\n"
                "fi\n"
                "exit 0\n"
            ),
        )
        r_active = subprocess.run(
            ["bash", str(script_copy), "--force"],
            capture_output=True,
            text=True,
            env={**os.environ, "DIFY_SOURCE": str(good_tar)},
        )
        assert r_active.returncode != 0
        assert "./dify.sh stop" in r_active.stderr
        assert expected_state in r_active.stderr
        assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
            "services:\n  old: {}\n"
        )
        assert list(work_dir.glob("docker.bak.*")) == []

    # 2. Swap failure after backing up docker/ -> rolls back backup to docker/
    # (Also verifies exited/created/dead states are treated as stopped and allowed)
    _write_executable(
        work_dir / "dify.sh",
        (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "ps" ]]; then\n'
            '  printf "exited\\ncreated\\ndead\\n"\n'
            "fi\n"
            "exit 0\n"
        ),
    )
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    real_mv = shutil.which("mv") or "/bin/mv"
    _write_executable(
        fake_bin / "mv",
        (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == *".fetch.tmp."* && "$2" == *"/docker" ]]; then\n'
            '  echo "simulated mv failure" >&2\n'
            "  exit 1\n"
            "fi\n"
            f'exec "{real_mv}" "$@"\n'
        ),
    )
    r_swap_fail = subprocess.run(
        ["bash", str(script_copy), "--force"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "DIFY_SOURCE": str(good_tar),
        },
    )
    assert r_swap_fail.returncode != 0
    assert "已回滚" in r_swap_fail.stderr
    assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  old: {}\n"
    )
    assert (docker_dir / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"
    assert (docker_dir / "volumes" / "db" / "data.txt").read_text(encoding="utf-8") == (
        "volume-state\n"
    )
    assert list(work_dir.glob("docker.bak.*")) == []


def test_fetch_sh_force_migration_failure_prints_recovery_guide(tmp_path: Path) -> None:
    work_dir = tmp_path / "dify_mig_fail"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)
    docker_dir = _seed_existing_docker(work_dir)
    _write_executable(work_dir / "dify.sh", "#!/usr/bin/env bash\nexit 0\n")

    good_tar = _build_dify_tarball(
        tmp_path / "dify-1.20.0.tar.gz",
        compose_content="services:\n  new_version: {}\n",
    )

    # Simulate failure when migrating .env back from docker.bak.* (after volumes/ succeeds)
    fake_bin = tmp_path / "fake_cp_bin"
    fake_bin.mkdir()
    real_cp = shutil.which("cp") or "/bin/cp"
    _write_executable(
        fake_bin / "cp",
        (
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  if [[ "$a" == *"docker.bak."*".env" ]]; then\n'
            '    echo "simulated cp failure" >&2\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
            f'exec "{real_cp}" "$@"\n'
        ),
    )

    r_fail = subprocess.run(
        ["bash", str(script_copy), "--force"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "DIFY_SOURCE": str(good_tar),
        },
    )
    assert r_fail.returncode != 0
    backups = list(work_dir.glob("docker.bak.*"))
    assert len(backups) == 1
    assert str(backups[0]) in r_fail.stderr
    assert "volumes/ 已迁回，不要删除" in r_fail.stderr
    assert "rm -rf" not in r_fail.stderr
    assert f'cp -p "{backups[0]}/.env" "{docker_dir}/.env"' in r_fail.stderr
    assert "迁回完成前不要启动 Dify" in r_fail.stderr
    # volumes/ is already safely in docker/ and no longer in backup
    assert (docker_dir / "volumes" / "db" / "data.txt").read_text(encoding="utf-8") == (
        "volume-state\n"
    )
    assert not (backups[0] / "volumes").exists()
    # Old .env is preserved in backup directory for manual recovery
    assert (backups[0] / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"


def test_r4_l1_fetch_sh_volumes_migration_failure_lists_both_items(tmp_path: Path) -> None:
    work_dir = tmp_path / "dify_vol_fail"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)
    docker_dir = _seed_existing_docker(work_dir)
    _write_executable(work_dir / "dify.sh", "#!/usr/bin/env bash\nexit 0\n")

    good_tar = _build_dify_tarball(
        tmp_path / "dify-1.20.0.tar.gz",
        compose_content="services:\n  new_version: {}\n",
    )

    # Simulate failure when moving volumes/ back from docker.bak.*
    fake_bin = tmp_path / "fake_mv_vol_bin"
    fake_bin.mkdir()
    real_mv = shutil.which("mv") or "/bin/mv"
    _write_executable(
        fake_bin / "mv",
        (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == *"docker.bak."*"/volumes" ]]; then\n'
            '  echo "simulated volumes mv failure" >&2\n'
            "  exit 1\n"
            "fi\n"
            f'exec "{real_mv}" "$@"\n'
        ),
    )

    r_fail = subprocess.run(
        ["bash", str(script_copy), "--force"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "DIFY_SOURCE": str(good_tar),
        },
    )
    assert r_fail.returncode != 0
    backups = list(work_dir.glob("docker.bak.*"))
    assert len(backups) == 1
    assert "volumes/ 已迁回，不要删除" not in r_fail.stderr
    assert f'mv "{backups[0]}/volumes" "{docker_dir}/volumes"' in r_fail.stderr
    assert f'cp -p "{backups[0]}/.env" "{docker_dir}/.env"' in r_fail.stderr
    assert (backups[0] / "volumes" / "db" / "data.txt").read_text(encoding="utf-8") == (
        "volume-state\n"
    )
    assert (backups[0] / ".env").read_text(encoding="utf-8") == "KEEP=1\nSECRET=abc\n"


def test_fetch_sh_git_archive_local_source_offline(tmp_path: Path) -> None:
    # Build a minimal local git repository tagged 1.20.0 (no network access)
    repo_dir = tmp_path / "local_dify_repo"
    (repo_dir / "docker" / "nginx").mkdir(parents=True)
    (repo_dir / "docker" / "ssrf_proxy").mkdir(parents=True)
    (repo_dir / "docker" / "docker-compose.yaml").write_text(
        "services:\n  from_git_archive: {}\n", encoding="utf-8"
    )
    (repo_dir / "docker" / ".env.example").write_text("GIT_ARCHIVE=1\n", encoding="utf-8")
    (repo_dir / "docker" / "nginx" / "nginx.conf").write_text("events {}\n", encoding="utf-8")
    (repo_dir / "docker" / "ssrf_proxy" / "squid.conf").write_text(
        "http_port 3128\n", encoding="utf-8"
    )

    git_env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        ["git", "-C", str(repo_dir), "init"],
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "docker"],
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "init dify 1.20.0",
        ],
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "tag", "1.20.0"],
        check=True,
        capture_output=True,
        env=git_env,
    )

    work_dir = tmp_path / "dify_git_work"
    work_dir.mkdir()
    script_copy = work_dir / "fetch.sh"
    shutil.copy2(FETCH_SCRIPT, script_copy)

    r_fetch = subprocess.run(
        ["bash", str(script_copy)],
        capture_output=True,
        text=True,
        env={**os.environ, "DIFY_SOURCE": str(repo_dir)},
    )
    assert r_fetch.returncode == 0, r_fetch.stderr
    assert "本地仓库" in r_fetch.stdout
    docker_dir = work_dir / "docker"
    assert (docker_dir / "docker-compose.yaml").read_text(encoding="utf-8") == (
        "services:\n  from_git_archive: {}\n"
    )
    assert (docker_dir / ".env.example").read_text(encoding="utf-8") == "GIT_ARCHIVE=1\n"
    assert (docker_dir / "nginx" / "nginx.conf").exists()
    assert (docker_dir / "ssrf_proxy" / "squid.conf").exists()
