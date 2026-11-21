from __future__ import annotations

import json
import re
from typing import Any

from app.log import sanitize_error_detail

CALL_SUMMARY_STRING_FIELDS = ("tool", "kind", "error_code", "request_id", "approval_id")
CALL_SUMMARY_FIELDS = (
    "tool",
    "kind",
    "http_status",
    "error_code",
    "request_id",
    "approval_id",
)

DIFY_ERROR_EVENT_PAYLOAD: dict[str, str] = {
    "code": "dify_error",
    "message": "助手暂时不可用，请稍后再试。",
}

_DOC_EXT_RE = re.compile(r"\.(?:md|markdown|txt|pdf|docx|html|htm)$", re.IGNORECASE)
_CLAUSE_HEADER_RE = re.compile(
    r"^\s*(?:#+\s*)?"
    r"(?:《(?P<doc1>[^《》，。：；！？\n]+)》\s*|(?P<doc2>[^《》，。：；！？\s]+)\s+)"
    r"第\s*(?P<clause>\d+(?:\.\d+)+)\s*条"
    r"(?:\s+(?P<title>.+?))?\s*$"
)


def _strip_doc_extension(name: str) -> str:
    return _DOC_EXT_RE.sub("", name.strip())


def parse_citations(retriever_resources: Any) -> list[dict[str, Any]]:
    if not isinstance(retriever_resources, list):
        return []

    citations: list[dict[str, Any]] = []
    for item in retriever_resources:
        if not isinstance(item, dict):
            continue

        raw_doc = item.get("document_name")
        doc_name = _strip_doc_extension(raw_doc) if isinstance(raw_doc, str) else ""

        raw_content = item.get("content")
        content = raw_content if isinstance(raw_content, str) else ""

        clause: str | None = None
        title: str | None = None
        parsed_doc: str | None = None

        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        if first_line:
            match = _CLAUSE_HEADER_RE.match(first_line)
            if match:
                parsed_doc = (match.group("doc1") or match.group("doc2") or "").strip() or None
                clause = match.group("clause").strip()
                raw_title = match.group("title")
                if raw_title and raw_title.strip():
                    title = raw_title.strip()

        final_doc = doc_name or parsed_doc or ""

        citation: dict[str, Any] = {}
        if final_doc:
            citation["document"] = final_doc
        if clause:
            citation["clause"] = clause
        if title:
            citation["title"] = title
        if isinstance(raw_content, str):
            citation["content"] = raw_content

        score = item.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            citation["score"] = float(score)

        if citation:
            citations.append(citation)

    return citations


def sanitize_call_summary(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    cleaned: dict[str, Any] = {}
    for field in CALL_SUMMARY_STRING_FIELDS:
        value = raw.get(field)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                cleaned[field] = stripped

    raw_status = raw.get("http_status")
    if isinstance(raw_status, int) and not isinstance(raw_status, bool):
        cleaned["http_status"] = raw_status
    elif isinstance(raw_status, str) and raw_status.strip().isdigit():
        cleaned["http_status"] = int(raw_status.strip())

    if "tool" not in cleaned and "kind" not in cleaned:
        return None

    ordered: dict[str, Any] = {}
    for field in CALL_SUMMARY_FIELDS:
        if field in cleaned:
            ordered[field] = cleaned[field]
    return ordered


def format_sse(event_name: str, data: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class DifyStreamProcessor:
    def __init__(self) -> None:
        self.conversation_id: str = ""
        self.message_id: str = ""
        self.trace: list[dict[str, Any]] = []
        self._answer_parts: list[str] = []
        self.done_payload: dict[str, Any] | None = None
        self.failed: bool = False
        self.error_reason: str | None = None
        self.error_detail: str | None = None

    @property
    def answer(self) -> str:
        return "".join(self._answer_parts)

    def _track_ids(self, event_obj: dict[str, Any]) -> None:
        conv_id = event_obj.get("conversation_id")
        if isinstance(conv_id, str) and conv_id.strip():
            self.conversation_id = conv_id.strip()
        msg_id = event_obj.get("message_id") or event_obj.get("id")
        if isinstance(msg_id, str) and msg_id.strip():
            self.message_id = msg_id.strip()

    def process_event(self, event_obj: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(event_obj, dict):
            return []

        event_type = event_obj.get("event")
        if not isinstance(event_type, str):
            return []

        self._track_ids(event_obj)

        if event_type == "message":
            if self.failed:
                return []
            chunk = event_obj.get("answer")
            if isinstance(chunk, str) and chunk != "":
                self._answer_parts.append(chunk)
                return [("delta", {"text": chunk})]
            return []

        if event_type == "node_finished":
            if self.failed:
                return []
            data = event_obj.get("data")
            if not isinstance(data, dict):
                return []
            outputs = data.get("outputs")
            if not isinstance(outputs, dict):
                return []
            summary = sanitize_call_summary(outputs.get("call_summary"))
            if summary is None:
                return []
            self.trace.append(summary)
            return [("trace", summary)]

        if event_type == "message_end":
            if self.failed:
                return []
            metadata = event_obj.get("metadata")
            raw_resources = (
                metadata.get("retriever_resources") if isinstance(metadata, dict) else None
            )
            citations = parse_citations(raw_resources)
            payload: dict[str, Any] = {
                "conversation_id": self.conversation_id,
                "message_id": self.message_id,
                "answer": self.answer,
                "citations": citations,
                "trace": list(self.trace),
            }
            self.done_payload = payload
            return [("done", payload)]

        if event_type == "error":
            detail = sanitize_error_detail(event_obj.get("message"))
            if self.failed:
                if self.error_detail is None and detail is not None:
                    self.error_detail = detail
                return []
            code = event_obj.get("code")
            status = event_obj.get("status")
            if isinstance(code, str) and code.strip():
                self.error_reason = f"dify_error_{code.strip()}"
            elif isinstance(status, int) and not isinstance(status, bool):
                self.error_reason = f"dify_error_{status}"
            else:
                self.error_reason = "dify_stream_error"
            self.error_detail = detail
            self.failed = True
            return [("error", dict(DIFY_ERROR_EVENT_PAYLOAD))]

        if event_type == "workflow_finished":
            data = event_obj.get("data")
            status = data.get("status") if isinstance(data, dict) else None
            if status != "succeeded":
                raw_error = data.get("error") if isinstance(data, dict) else None
                detail = sanitize_error_detail(raw_error)
                if self.failed:
                    if self.error_detail is None and detail is not None:
                        self.error_detail = detail
                    return []
                status_label = status if isinstance(status, str) and status.strip() else "unknown"
                self.error_reason = f"workflow_{status_label}"
                self.error_detail = detail
                self.failed = True
                return [("error", dict(DIFY_ERROR_EVENT_PAYLOAD))]
            return []

        return []
