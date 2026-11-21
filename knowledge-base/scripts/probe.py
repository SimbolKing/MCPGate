"""Run retrieval probes against synced Dify knowledge bases (PROPOSAL 4.7.8)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

if __package__:
    from scripts import sync
else:
    import sync  # type: ignore[import-not-found]

CLAUSE_HEAD_RE = re.compile(r"^(?P<doc>[^\s《》#]+)\s+第\s*(?P<cid>\d+\.\d+)\s*条(?:\s|$)")
EXPECT_LONG_RE = re.compile(r"^(?P<doc>[^\s《》#]+)\s+第\s*(?P<cid>\d+\.\d+)\s*条$")
EXPECT_SHORT_RE = re.compile(r"^(?P<doc>[^\s《》#]+)\s+(?P<cid>\d+\.\d+)$")


class ProbeError(RuntimeError):
    """Raised when probe configuration or API calls fail."""


@dataclass(frozen=True)
class ProbeResult:
    probe_id: str
    dataset: str
    query: str
    expected: list[str]
    retrieved: list[str]
    missing: list[str]

    @property
    def passed(self) -> bool:
        return len(self.missing) == 0


def normalize_clause_ref(raw: str) -> str:
    """Normalize clause expectation into canonical '<文档名> 第 X.Y 条' form."""
    text = raw.strip()
    m = EXPECT_LONG_RE.match(text)
    if m:
        return f"{m.group('doc')} 第 {m.group('cid')} 条"
    m = EXPECT_SHORT_RE.match(text)
    if m:
        return f"{m.group('doc')} 第 {m.group('cid')} 条"
    return text


def extract_clause_from_segment(content: str) -> str | None:
    """Extract '<文档名> 第 X.Y 条' from the first non-empty line of a Dify segment."""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            line = line[3:].strip()
        m = CLAUSE_HEAD_RE.match(line)
        if m:
            return f"{m.group('doc')} 第 {m.group('cid')} 条"
        return None
    return None


def load_kb_lock(kb_dir: Path) -> dict[str, str]:
    lock_path = kb_dir / "kb.lock.json"
    if not lock_path.exists():
        raise ProbeError(f"找不到 {lock_path}，请先执行 scripts/sync.py 导入知识库。")
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProbeError(f"kb.lock.json 不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ProbeError("kb.lock.json 格式无效：顶层应为对象。")
    return {str(k): str(v) for k, v in data.items()}


def load_probes_yaml(kb_dir: Path) -> list[dict[str, Any]]:
    probes_path = kb_dir / "probes.yaml"
    if not probes_path.exists():
        raise ProbeError(f"找不到探针文件：{probes_path}")
    raw = yaml.safe_load(probes_path.read_text(encoding="utf-8"))
    items = raw.get("probes") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ProbeError("probes.yaml 格式无效：缺少非空的 probes 列表。")
    return items


def run_probes(
    kb_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> list[ProbeResult]:
    """Execute all retrieval probes in probes.yaml and return structured results."""
    kb_dir = kb_dir.resolve()
    try:
        config = sync.load_env_config(kb_dir, env=env, require_api_key=True)
        kb_spec = sync.load_kb_yaml(kb_dir)
    except sync.SyncError as exc:
        raise ProbeError(str(exc)) from exc

    lock_map = load_kb_lock(kb_dir)
    probes = load_probes_yaml(kb_dir)
    retrieval_model = sync.build_retrieval_model(
        kb_spec.get("retrieval_model", {}),
        config.rerank_provider,
        config.rerank_model,
    )

    owns_client = client is None
    active_client = (
        httpx.Client(base_url=config.base_url, timeout=60.0) if client is None else client
    )
    results: list[ProbeResult] = []
    try:
        for idx, item in enumerate(probes, start=1):
            probe_id = str(item.get("id") or f"P{idx:02d}")
            dataset_key = str(item.get("dataset", "")).strip()
            query = str(item.get("query", "")).strip()
            raw_expect = item.get("expect") or []

            if dataset_key not in lock_map:
                raise ProbeError(
                    f"探针 {probe_id} 引用了 kb.lock.json 中不存在的 dataset「{dataset_key}」。"
                )
            if not query:
                raise ProbeError(f"探针 {probe_id} 缺少 query。")
            if not isinstance(raw_expect, list) or not raw_expect:
                raise ProbeError(f"探针 {probe_id} 缺少 expect 列表。")

            expected = [normalize_clause_ref(str(e)) for e in raw_expect]
            dataset_id = lock_map[dataset_key]
            url = (
                f"/v1/datasets/{dataset_id}/retrieve"
                if str(active_client.base_url)
                else f"{config.base_url}/v1/datasets/{dataset_id}/retrieve"
            )
            try:
                resp = active_client.post(
                    url,
                    headers={"Authorization": f"Bearer {config.api_key}"},
                    json={"query": query, "retrieval_model": retrieval_model},
                )
            except httpx.HTTPError as exc:
                raise ProbeError(f"探针 {probe_id} 调用 Dify 检索接口失败：{exc}") from exc

            if resp.status_code >= 400:
                detail = resp.text.strip()[:300]
                raise ProbeError(f"探针 {probe_id} 检索接口返回错误 {resp.status_code}：{detail}")

            try:
                payload = resp.json()
            except ValueError as exc:
                raise ProbeError(f"探针 {probe_id} 检索接口返回非 JSON 响应。") from exc

            records = payload.get("records", [])
            retrieved: list[str] = []
            for rec in records:
                segment = rec.get("segment") if isinstance(rec, dict) else None
                content = segment.get("content", "") if isinstance(segment, dict) else ""
                clause = extract_clause_from_segment(str(content))
                if clause is not None:
                    retrieved.append(clause)

            retrieved_set = set(retrieved)
            missing = [e for e in expected if e not in retrieved_set]
            results.append(
                ProbeResult(
                    probe_id=probe_id,
                    dataset=dataset_key,
                    query=query,
                    expected=expected,
                    retrieved=retrieved,
                    missing=missing,
                )
            )
    finally:
        if owns_client:
            active_client.close()

    return results


def format_report(results: list[ProbeResult]) -> str:
    lines: list[str] = []
    passed_count = sum(1 for r in results if r.passed)
    for r in results:
        tag = "PASS" if r.passed else "FAIL"
        lines.append(f"[{tag}] {r.probe_id} ({r.dataset}) {r.query}")
        if r.passed:
            lines.append(f"       命中期望：{', '.join(r.expected)}")
        else:
            lines.append(f"       期望条款：{', '.join(r.expected)}")
            lines.append(f"       缺失条款：{', '.join(r.missing)}")
            top_preview = ", ".join(r.retrieved) if r.retrieved else "（无条款分段命中）"
            lines.append(f"       实际返回：{top_preview}")
    lines.append(f"检索探针汇总：通过 {passed_count}/{len(results)} 条。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对已同步的 Dify 知识库执行检索探针验证")
    parser.add_argument(
        "--kb-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="knowledge-base 目录路径（默认为脚本上级目录）",
    )
    args = parser.parse_args(argv)

    try:
        results = run_probes(args.kb_dir)
    except ProbeError as exc:
        print(f"探针执行失败：{exc}", file=sys.stderr)
        return 1

    print(format_report(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
