"""Sync knowledge-base documents to Dify via Service API (PROPOSAL 4.7.7)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

if __package__:
    from scripts import lint
else:
    import lint  # type: ignore[import-not-found]

DEFAULT_DIFY_BASE_URL = "http://localhost:8088"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0
PAGE_LIMIT = 100

IN_PROGRESS_DISPLAY_STATUSES = frozenset(
    {"indexing", "queuing", "waiting", "parsing", "cleaning", "splitting"}
)
IN_PROGRESS_INDEXING_STATUSES = frozenset(
    {"waiting", "parsing", "cleaning", "splitting", "indexing", "queuing"}
)
RECREATE_DISPLAY_STATUSES = frozenset({"error", "disabled", "archived", "paused"})
RECREATE_INDEXING_STATUSES = frozenset({"error", "paused"})


class SyncError(RuntimeError):
    """Raised when any step of knowledge-base synchronization fails."""


class DifyAPIError(SyncError):
    """Raised when Dify Service API returns an HTTP 4xx/5xx response."""

    def __init__(self, *, method: str, url: str, status_code: int, detail: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Dify API 返回错误 {status_code}（{method} {url}）：{detail}")


@dataclass(frozen=True)
class RemoteDocument:
    id: str
    name: str
    display_status: str | None = None
    indexing_status: str | None = None
    enabled: bool | None = None
    archived: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class KBConfig:
    base_url: str
    api_key: str
    embedding_provider: str = ""
    embedding_model: str = ""
    rerank_provider: str = ""
    rerank_model: str = ""


@dataclass(frozen=True)
class SyncedDocument:
    dataset_key: str
    dataset_name: str
    dataset_id: str
    filename: str
    document_id: str
    batch: str
    expected_segments: int
    actual_segments: int = 0


def parse_dotenv(dotenv_path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file without external dependencies."""
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_config(
    kb_dir: Path,
    env: Mapping[str, str] | None = None,
    *,
    require_api_key: bool = True,
) -> KBConfig:
    """Load KB_* configuration from kb_dir/.env overlaid with process env (or custom mapping)."""
    merged = parse_dotenv(kb_dir / ".env")
    source_env = os.environ if env is None else env
    for key, value in source_env.items():
        if key.startswith("KB_"):
            merged[key] = value

    base_url = merged.get("KB_DIFY_BASE_URL", "").strip() or DEFAULT_DIFY_BASE_URL
    base_url = base_url.rstrip("/")
    api_key = merged.get("KB_DIFY_API_KEY", "").strip()
    embedding_provider = merged.get("KB_EMBEDDING_PROVIDER", "").strip()
    embedding_model = merged.get("KB_EMBEDDING_MODEL", "").strip()
    rerank_provider = merged.get("KB_RERANK_PROVIDER", "").strip()
    rerank_model = merged.get("KB_RERANK_MODEL", "").strip()

    if require_api_key and not api_key:
        raise SyncError("缺少环境变量 KB_DIFY_API_KEY，请在 knowledge-base/.env 或环境中配置。")
    if bool(embedding_provider) != bool(embedding_model):
        raise SyncError("KB_EMBEDDING_PROVIDER 与 KB_EMBEDDING_MODEL 必须同时配置或同时留空。")
    if rerank_model and not rerank_provider:
        raise SyncError("配置 KB_RERANK_MODEL 时必须同时配置 KB_RERANK_PROVIDER。")

    return KBConfig(
        base_url=base_url,
        api_key=api_key,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        rerank_provider=rerank_provider,
        rerank_model=rerank_model,
    )


def load_kb_yaml(kb_dir: Path) -> dict[str, Any]:
    kb_yaml_path = kb_dir / "kb.yaml"
    if not kb_yaml_path.exists():
        raise SyncError(f"找不到配置文件：{kb_yaml_path}")
    data = yaml.safe_load(kb_yaml_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("datasets"), list):
        raise SyncError("kb.yaml 格式无效：缺少 datasets 列表。")
    return data


def build_retrieval_model(
    base_retrieval_model: Mapping[str, Any],
    rerank_provider: str,
    rerank_model: str,
) -> dict[str, Any]:
    """Compute effective Dify retrieval_model, applying Rerank overrides per 4.7.6."""
    model: dict[str, Any] = {
        "search_method": base_retrieval_model.get("search_method", "semantic_search"),
        "reranking_enable": bool(base_retrieval_model.get("reranking_enable", False)),
        "top_k": int(base_retrieval_model.get("top_k", 8)),
        "score_threshold_enabled": bool(base_retrieval_model.get("score_threshold_enabled", False)),
    }
    if (
        "score_threshold" in base_retrieval_model
        and base_retrieval_model["score_threshold"] is not None
    ):
        model["score_threshold"] = base_retrieval_model["score_threshold"]

    if rerank_model:
        model["reranking_enable"] = True
        model["reranking_mode"] = "reranking_model"
        model["reranking_model"] = {
            "reranking_provider_name": rerank_provider,
            "reranking_model_name": rerank_model,
        }
        model["top_k"] = 6
    else:
        model["reranking_enable"] = False

    return model


def _url(client: httpx.Client, base_url: str, path: str) -> str:
    if str(client.base_url):
        return path
    return f"{base_url}{path}"


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    api_key: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
) -> Any:
    try:
        resp = client.request(
            method,
            url,
            headers=_auth_headers(api_key),
            params=params,
            json=json_body,
            data=data,
            files=files,
        )
    except httpx.HTTPError as exc:
        raise SyncError(f"请求 Dify 失败（{method} {url}）：{exc}") from exc

    if resp.status_code >= 400:
        detail = resp.text.strip()[:300]
        raise DifyAPIError(method=method, url=url, status_code=resp.status_code, detail=detail)

    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise SyncError(f"Dify API 返回非 JSON 响应（{method} {url}）") from exc


def _paginate(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    path: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _request_json(
            client,
            "GET",
            _url(client, base_url, path),
            api_key=api_key,
            params={"page": page, "limit": PAGE_LIMIT},
        )
        batch = payload.get("data", [])
        if not isinstance(batch, list):
            raise SyncError(f"分页接口响应缺少 data 列表：{path}")
        items.extend(batch)
        if not payload.get("has_more") or not batch:
            break
        page += 1
    return items


def _dataset_needs_update(
    remote: Mapping[str, Any],
    indexing_technique: str,
    embedding_provider: str,
    embedding_model: str,
    desired_retrieval: Mapping[str, Any],
) -> bool:
    if remote.get("indexing_technique") != indexing_technique:
        return True
    if embedding_provider and embedding_model:
        if (
            remote.get("embedding_model_provider") != embedding_provider
            or remote.get("embedding_model") != embedding_model
        ):
            return True

    remote_ret = remote.get("retrieval_model_dict") or remote.get("retrieval_model") or {}
    if remote_ret.get("search_method") != desired_retrieval.get("search_method"):
        return True
    if bool(remote_ret.get("reranking_enable", False)) != bool(
        desired_retrieval.get("reranking_enable", False)
    ):
        return True
    if remote_ret.get("top_k") != desired_retrieval.get("top_k"):
        return True
    if bool(remote_ret.get("score_threshold_enabled", False)) != bool(
        desired_retrieval.get("score_threshold_enabled", False)
    ):
        return True

    if desired_retrieval.get("reranking_enable"):
        if remote_ret.get("reranking_mode") != desired_retrieval.get("reranking_mode"):
            return True
        remote_rm = remote_ret.get("reranking_model") or {}
        desired_rm = desired_retrieval.get("reranking_model") or {}
        if remote_rm.get("reranking_provider_name") != desired_rm.get(
            "reranking_provider_name"
        ) or remote_rm.get("reranking_model_name") != desired_rm.get("reranking_model_name"):
            return True

    return False


def _ensure_dataset(
    client: httpx.Client,
    config: KBConfig,
    existing_by_name: Mapping[str, dict[str, Any]],
    *,
    dataset_name: str,
    indexing_technique: str,
    retrieval_model: dict[str, Any],
) -> str:
    remote = existing_by_name.get(dataset_name)
    if remote is None:
        create_payload: dict[str, Any] = {
            "name": dataset_name,
            "indexing_technique": indexing_technique,
            "retrieval_model": retrieval_model,
        }
        if config.embedding_provider and config.embedding_model:
            create_payload["embedding_model_provider"] = config.embedding_provider
            create_payload["embedding_model"] = config.embedding_model
        created = _request_json(
            client,
            "POST",
            _url(client, config.base_url, "/v1/datasets"),
            api_key=config.api_key,
            json_body=create_payload,
        )
        dataset_id = created.get("id")
        if not dataset_id:
            raise SyncError(f"创建知识库「{dataset_name}」后未返回 id。")
        return str(dataset_id)

    dataset_id = str(remote["id"])
    if _dataset_needs_update(
        remote,
        indexing_technique,
        config.embedding_provider,
        config.embedding_model,
        retrieval_model,
    ):
        patch_payload: dict[str, Any] = {
            "indexing_technique": indexing_technique,
            "retrieval_model": retrieval_model,
        }
        if config.embedding_provider and config.embedding_model:
            patch_payload["embedding_model_provider"] = config.embedding_provider
            patch_payload["embedding_model"] = config.embedding_model
        _request_json(
            client,
            "PATCH",
            _url(client, config.base_url, f"/v1/datasets/{dataset_id}"),
            api_key=config.api_key,
            json_body=patch_payload,
        )
    return dataset_id


def _parse_remote_doc(
    raw: Mapping[str, Any],
    *,
    fallback_id: str = "",
    fallback_name: str = "",
) -> RemoteDocument | None:
    rid = str(raw.get("id") or fallback_id).strip()
    rname = str(raw.get("name") or fallback_name).strip()
    if not rid or not rname:
        return None
    display_status = raw.get("display_status")
    indexing_status = raw.get("indexing_status")
    enabled = raw.get("enabled")
    archived = raw.get("archived")
    error = raw.get("error")
    return RemoteDocument(
        id=rid,
        name=rname,
        display_status=str(display_status).strip().lower() if display_status is not None else None,
        indexing_status=(
            str(indexing_status).strip().lower() if indexing_status is not None else None
        ),
        enabled=enabled if isinstance(enabled, bool) else None,
        archived=archived if isinstance(archived, bool) else None,
        error=str(error) if error else None,
    )


def _format_remote_status(doc: RemoteDocument) -> str:
    return (
        f"display_status={doc.display_status}, "
        f"indexing_status={doc.indexing_status}, "
        f"enabled={doc.enabled}, "
        f"archived={doc.archived}"
    )


def _classify_remote_doc(doc: RemoteDocument) -> Literal["available", "indexing", "recreate"]:
    if doc.archived is True or doc.display_status == "archived":
        return "recreate"
    if doc.enabled is False or doc.display_status == "disabled":
        return "recreate"
    if (
        doc.display_status in RECREATE_DISPLAY_STATUSES
        or doc.indexing_status in RECREATE_INDEXING_STATUSES
    ):
        return "recreate"
    if (
        doc.display_status in IN_PROGRESS_DISPLAY_STATUSES
        or doc.indexing_status in IN_PROGRESS_INDEXING_STATUSES
    ):
        return "indexing"
    if doc.display_status == "available":
        return "available"
    if doc.display_status is None and doc.indexing_status in {None, "completed"}:
        return "available"
    return "recreate"


def _select_primary_doc(
    matches: list[RemoteDocument],
) -> tuple[RemoteDocument, list[RemoteDocument]]:
    priority = {"available": 0, "indexing": 1, "recreate": 2}
    primary_idx = min(
        range(len(matches)),
        key=lambda i: priority[_classify_remote_doc(matches[i])],
    )
    primary = matches[primary_idx]
    duplicates = [doc for idx, doc in enumerate(matches) if idx != primary_idx]
    return primary, duplicates


def _is_doc_unavailable_error(exc: DifyAPIError) -> bool:
    if exc.status_code not in {400, 403, 409}:
        return False
    lowered = exc.detail.lower()
    return any(
        marker in lowered
        for marker in (
            "not available",
            "unavailable",
            "archived_document_immutable",
            "document is archived",
            "archived document",
            "不可用",
            "已归档",
            "已禁用",
        )
    )


def _delete_remote_document(
    client: httpx.Client,
    config: KBConfig,
    dataset_id: str,
    doc: RemoteDocument,
) -> None:
    delete_url = _url(client, config.base_url, f"/v1/datasets/{dataset_id}/documents/{doc.id}")
    try:
        _request_json(client, "DELETE", delete_url, api_key=config.api_key)
    except DifyAPIError as exc:
        lowered = exc.detail.lower()
        if exc.status_code == 403 and (
            "archived_document_immutable" in lowered or "archived" in lowered
        ):
            _request_json(
                client,
                "PATCH",
                _url(
                    client,
                    config.base_url,
                    f"/v1/datasets/{dataset_id}/documents/status/un_archive",
                ),
                api_key=config.api_key,
                json_body={"document_ids": [doc.id]},
            )
            _request_json(client, "DELETE", delete_url, api_key=config.api_key)
            return
        raise


def _create_document_by_file(
    client: httpx.Client,
    config: KBConfig,
    *,
    dataset_id: str,
    filename: str,
    file_bytes: bytes,
    indexing_json: str,
) -> dict[str, Any]:
    resp = _request_json(
        client,
        "POST",
        _url(client, config.base_url, f"/v1/datasets/{dataset_id}/document/create-by-file"),
        api_key=config.api_key,
        data={"data": indexing_json},
        files={"file": (filename, file_bytes, "text/markdown")},
    )
    return dict(resp)


def _update_document_by_file(
    client: httpx.Client,
    config: KBConfig,
    *,
    dataset_id: str,
    document_id: str,
    filename: str,
    file_bytes: bytes,
    indexing_json: str,
) -> dict[str, Any]:
    resp = _request_json(
        client,
        "POST",
        _url(
            client,
            config.base_url,
            f"/v1/datasets/{dataset_id}/documents/{document_id}/update-by-file",
        ),
        api_key=config.api_key,
        data={"data": indexing_json},
        files={"file": (filename, file_bytes, "text/markdown")},
    )
    return dict(resp)


def _wait_remote_doc_terminal(
    client: httpx.Client,
    config: KBConfig,
    *,
    dataset_id: str,
    dataset_name: str,
    doc: RemoteDocument,
    poll_interval: float,
    poll_timeout: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> RemoteDocument | None:
    deadline = monotonic_fn() + poll_timeout
    current = doc
    doc_url = _url(client, config.base_url, f"/v1/datasets/{dataset_id}/documents/{doc.id}")

    while True:
        try:
            payload = _request_json(client, "GET", doc_url, api_key=config.api_key)
        except DifyAPIError as exc:
            if exc.status_code == 404:
                return None
            raise

        if not isinstance(payload, Mapping):
            raise SyncError(
                f"查询文档「{doc.name}」（dataset={dataset_name}）状态返回格式异常：{payload}"
            )
        refreshed = _parse_remote_doc(payload, fallback_id=doc.id, fallback_name=doc.name)
        if refreshed is None:
            raise SyncError(
                f"查询文档「{doc.name}」（dataset={dataset_name}）状态返回缺少文档标识：{payload}"
            )
        current = refreshed
        if _classify_remote_doc(current) != "indexing":
            return current

        if monotonic_fn() >= deadline:
            raise SyncError(
                f"文档「{doc.name}」（dataset={dataset_name}）等待既有索引结束超时"
                f"（远端状态：{_format_remote_status(current)}）。"
            )
        sleep_fn(poll_interval)


def _sync_single_document(
    client: httpx.Client,
    config: KBConfig,
    *,
    dataset_id: str,
    dataset_name: str,
    filename: str,
    file_bytes: bytes,
    indexing_json: str,
    matches: list[RemoteDocument],
    kept_ids: set[str],
    deleted_ids: set[str],
    poll_interval: float,
    poll_timeout: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> dict[str, Any]:
    if not matches:
        return _create_document_by_file(
            client,
            config,
            dataset_id=dataset_id,
            filename=filename,
            file_bytes=file_bytes,
            indexing_json=indexing_json,
        )

    primary, duplicates = _select_primary_doc(matches)
    for dup in duplicates:
        _delete_remote_document(client, config, dataset_id, dup)
        deleted_ids.add(dup.id)

    state = _classify_remote_doc(primary)
    if state == "indexing":
        refreshed = _wait_remote_doc_terminal(
            client,
            config,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            doc=primary,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        if refreshed is None:
            deleted_ids.add(primary.id)
            return _create_document_by_file(
                client,
                config,
                dataset_id=dataset_id,
                filename=filename,
                file_bytes=file_bytes,
                indexing_json=indexing_json,
            )
        primary = refreshed
        state = _classify_remote_doc(primary)

    if state == "available":
        try:
            resp = _update_document_by_file(
                client,
                config,
                dataset_id=dataset_id,
                document_id=primary.id,
                filename=filename,
                file_bytes=file_bytes,
                indexing_json=indexing_json,
            )
            kept_ids.add(primary.id)
            return resp
        except DifyAPIError as exc:
            if not _is_doc_unavailable_error(exc):
                raise SyncError(
                    f"更新文档「{filename}」（dataset={dataset_name}，"
                    f"远端状态：{_format_remote_status(primary)}）失败：{exc}"
                ) from exc
            try:
                _delete_remote_document(client, config, dataset_id, primary)
                deleted_ids.add(primary.id)
                return _create_document_by_file(
                    client,
                    config,
                    dataset_id=dataset_id,
                    filename=filename,
                    file_bytes=file_bytes,
                    indexing_json=indexing_json,
                )
            except SyncError as fallback_exc:
                raise SyncError(
                    f"更新文档「{filename}」（dataset={dataset_name}，"
                    f"远端状态：{_format_remote_status(primary)}）返回不可用错误后，"
                    f"回退删除并重建仍失败：{fallback_exc}"
                ) from fallback_exc

    # state == "recreate" (error / disabled / archived / paused)
    try:
        _delete_remote_document(client, config, dataset_id, primary)
        deleted_ids.add(primary.id)
        return _create_document_by_file(
            client,
            config,
            dataset_id=dataset_id,
            filename=filename,
            file_bytes=file_bytes,
            indexing_json=indexing_json,
        )
    except SyncError as recreate_exc:
        raise SyncError(
            f"重建不可用文档「{filename}」（dataset={dataset_name}，"
            f"远端状态：{_format_remote_status(primary)}）失败：{recreate_exc}"
        ) from recreate_exc


def _poll_indexing_status(
    client: httpx.Client,
    config: KBConfig,
    doc: SyncedDocument,
    *,
    poll_interval: float,
    poll_timeout: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> int:
    deadline = monotonic_fn() + poll_timeout
    status_path = f"/v1/datasets/{doc.dataset_id}/documents/{doc.batch}/indexing-status"

    while True:
        payload = _request_json(
            client,
            "GET",
            _url(client, config.base_url, status_path),
            api_key=config.api_key,
        )
        entries = payload.get("data", [])
        if not isinstance(entries, list) or not entries:
            raise SyncError(f"文档「{doc.filename}」的索引状态响应为空（batch={doc.batch}）。")

        entry = next((e for e in entries if str(e.get("id")) == doc.document_id), entries[0])
        status = entry.get("indexing_status")
        if status == "completed":
            return int(entry.get("total_segments", 0))
        if status == "error":
            err_msg = entry.get("error") or "未知索引错误"
            raise SyncError(
                f"文档「{doc.filename}」（dataset={doc.dataset_name}）索引失败：{err_msg}"
            )

        if monotonic_fn() >= deadline:
            raise SyncError(
                f"文档「{doc.filename}」（dataset={doc.dataset_name}）索引超时（当前状态：{status}）。"
            )
        sleep_fn(poll_interval)


def sync_knowledge_base(
    kb_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    """Execute the 6-step Dify knowledge-base synchronization workflow."""
    kb_dir = kb_dir.resolve()

    # Step 1: offline lint
    errors, _ = lint.lint_all(kb_dir)
    if errors:
        formatted = "\n".join(f"  - {e.format()}" for e in errors)
        raise SyncError(f"文档规范检查未通过，已中止导入：\n{formatted}")

    config = load_env_config(kb_dir, env=env, require_api_key=True)
    kb_spec = load_kb_yaml(kb_dir)
    indexing = kb_spec.get("indexing", {})
    indexing_technique = str(indexing.get("indexing_technique", "high_quality"))
    retrieval_model = build_retrieval_model(
        kb_spec.get("retrieval_model", {}),
        config.rerank_provider,
        config.rerank_model,
    )
    indexing_json = json.dumps(indexing, ensure_ascii=False)

    owns_client = client is None
    active_client = (
        httpx.Client(base_url=config.base_url, timeout=60.0) if client is None else client
    )
    try:
        # Step 2: create or update datasets
        remote_datasets = _paginate(active_client, config.base_url, config.api_key, "/v1/datasets")
        existing_by_name = {
            str(ds["name"]): ds for ds in remote_datasets if isinstance(ds, dict) and "name" in ds
        }

        lock_mapping: dict[str, str] = {}
        synced_docs: list[SyncedDocument] = []

        for ds_spec in kb_spec["datasets"]:
            ds_key = str(ds_spec["key"])
            ds_name = str(ds_spec["name"])
            ds_docs = [str(p) for p in ds_spec.get("documents", [])]

            dataset_id = _ensure_dataset(
                active_client,
                config,
                existing_by_name,
                dataset_name=ds_name,
                indexing_technique=indexing_technique,
                retrieval_model=retrieval_model,
            )
            lock_mapping[ds_key] = dataset_id

            # Step 3: upload / update documents and remove stale remote documents
            remote_docs = _paginate(
                active_client,
                config.base_url,
                config.api_key,
                f"/v1/datasets/{dataset_id}/documents",
            )
            parsed_remote_docs: list[RemoteDocument] = []
            remote_by_name: dict[str, list[RemoteDocument]] = {}
            for raw_doc in remote_docs:
                if not isinstance(raw_doc, Mapping):
                    continue
                parsed = _parse_remote_doc(raw_doc)
                if parsed is not None:
                    parsed_remote_docs.append(parsed)
                    remote_by_name.setdefault(parsed.name, []).append(parsed)

            kept_ids: set[str] = set()
            deleted_ids: set[str] = set()
            for rel_doc_path in ds_docs:
                doc_path = (kb_dir / rel_doc_path).resolve()
                filename = doc_path.name
                file_bytes = doc_path.read_bytes()
                expected_segments = lint.count_h2_headings(doc_path) + 1

                matches = remote_by_name.get(filename, [])
                resp = _sync_single_document(
                    active_client,
                    config,
                    dataset_id=dataset_id,
                    dataset_name=ds_name,
                    filename=filename,
                    file_bytes=file_bytes,
                    indexing_json=indexing_json,
                    matches=matches,
                    kept_ids=kept_ids,
                    deleted_ids=deleted_ids,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                )

                doc_obj = resp.get("document") or {}
                doc_id = str(doc_obj.get("id", ""))
                batch = str(resp.get("batch", ""))
                if not doc_id or not batch:
                    raise SyncError(f"上传文档「{filename}」返回缺少 document.id 或 batch。")
                kept_ids.add(doc_id)
                synced_docs.append(
                    SyncedDocument(
                        dataset_key=ds_key,
                        dataset_name=ds_name,
                        dataset_id=dataset_id,
                        filename=filename,
                        document_id=doc_id,
                        batch=batch,
                        expected_segments=expected_segments,
                    )
                )

            for rdoc in parsed_remote_docs:
                if rdoc.id not in kept_ids and rdoc.id not in deleted_ids:
                    _delete_remote_document(active_client, config, dataset_id, rdoc)
                    deleted_ids.add(rdoc.id)

        # Step 4: poll indexing-status until completed
        completed_docs: list[SyncedDocument] = []
        for doc in synced_docs:
            actual_segments = _poll_indexing_status(
                active_client,
                config,
                doc,
                poll_interval=poll_interval,
                poll_timeout=poll_timeout,
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
            completed_docs.append(
                SyncedDocument(
                    dataset_key=doc.dataset_key,
                    dataset_name=doc.dataset_name,
                    dataset_id=doc.dataset_id,
                    filename=doc.filename,
                    document_id=doc.document_id,
                    batch=doc.batch,
                    expected_segments=doc.expected_segments,
                    actual_segments=actual_segments,
                )
            )

        # Step 5: verify total_segments == H2 count + 1
        mismatches = [d for d in completed_docs if d.actual_segments != d.expected_segments]
        if mismatches:
            details = "\n".join(
                f"  - {d.dataset_name}/{d.filename}: "
                f"预期 {d.expected_segments} 段，实际 {d.actual_segments} 段"
                for d in mismatches
            )
            raise SyncError(f"分段数核对未通过（与「二级标题数 + 1」不符）：\n{details}")

        # Step 6: write kb.lock.json
        lock_path = kb_dir / "kb.lock.json"
        lock_path.write_text(
            json.dumps(lock_mapping, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return lock_mapping
    finally:
        if owns_client:
            active_client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将知识库文档同步导入 Dify 并生成 kb.lock.json")
    parser.add_argument(
        "--kb-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="knowledge-base 目录路径（默认为脚本上级目录）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="索引状态轮询间隔（秒）",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
        help="单份文档索引超时时间（秒）",
    )
    args = parser.parse_args(argv)

    try:
        lock_mapping = sync_knowledge_base(
            args.kb_dir,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
        )
    except SyncError as exc:
        print(f"同步失败：{exc}", file=sys.stderr)
        return 1

    print("知识库同步完成，已写出 kb.lock.json：")
    for key, ds_id in lock_mapping.items():
        print(f"  - {key}: {ds_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
