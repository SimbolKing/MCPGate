from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from starlette.requests import Request

RESERVED_PREFIXES = ("/guard/", "/console/")
RESERVED_EXACT = frozenset({"/guard", "/console", "/healthz", "/"})
_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ToolEntry:
    name: str
    method: str
    path: str
    write: bool
    owner: str


@dataclass(frozen=True)
class MatchedTool:
    entry: ToolEntry
    path_params: dict[str, str]

    def render_upstream_path(self) -> str:
        return _PARAM_RE.sub(
            lambda m: quote(self.path_params[m.group(1)], safe=""),
            self.entry.path,
        )


def extract_request_path(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    if isinstance(raw_path, bytes) and raw_path:
        return raw_path.decode("latin-1")
    return request.url.path


def _compile_path_template(template: str) -> re.Pattern[str]:
    parts: list[str] = ["^"]
    pos = 0
    for match in _PARAM_RE.finditer(template):
        parts.append(re.escape(template[pos : match.start()]))
        param_name = match.group(1)
        parts.append(f"(?P<{param_name}>[^/]+)")
        pos = match.end()
    parts.append(re.escape(template[pos:]))
    parts.append("$")
    return re.compile("".join(parts))


def _check_upstream_path_conflict(path: str) -> None:
    if path in RESERVED_EXACT or path.startswith(RESERVED_PREFIXES):
        raise ValueError(f"工具清单中的上游路径 {path!r} 与 guard 自有路径冲突")


class ToolManifest:
    def __init__(self, entries: list[ToolEntry]) -> None:
        self._entries = tuple(entries)
        upstream_list: list[tuple[ToolEntry, re.Pattern[str]]] = []
        gateway_list: list[ToolEntry] = []

        for entry in entries:
            if entry.owner == "upstream":
                _check_upstream_path_conflict(entry.path)
                upstream_list.append((entry, _compile_path_template(entry.path)))
            elif entry.owner == "gateway":
                gateway_list.append(entry)
            else:
                raise ValueError(f"未知的工具 owner: {entry.owner!r} ({entry.name})")

        # Prefer static paths over parameterized paths when matching
        upstream_list.sort(key=lambda pair: (pair[0].path.count("{"), pair[0].path))
        self._upstream_matchers = tuple(upstream_list)
        self._gateway_entries = tuple(gateway_list)

    @classmethod
    def from_raw(cls, raw: list[dict[str, Any]]) -> ToolManifest:
        entries = [
            ToolEntry(
                name=str(item["name"]),
                method=str(item["method"]).upper(),
                path=str(item["path"]),
                write=bool(item["write"]),
                owner=str(item["owner"]),
            )
            for item in raw
        ]
        return cls(entries)

    @classmethod
    def from_file(cls, path: Path) -> ToolManifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"工具清单必须为 JSON 数组: {path}")
        return cls.from_raw(data)

    @property
    def entries(self) -> tuple[ToolEntry, ...]:
        return self._entries

    @property
    def upstream_entries(self) -> tuple[ToolEntry, ...]:
        return tuple(entry for entry, _ in self._upstream_matchers)

    @property
    def gateway_entries(self) -> tuple[ToolEntry, ...]:
        return self._gateway_entries

    def match_upstream(self, method: str, path: str) -> MatchedTool | None:
        normalized_method = method.upper()
        for entry, pattern in self._upstream_matchers:
            if entry.method != normalized_method:
                continue
            match = pattern.match(path)
            if match is not None:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return MatchedTool(entry=entry, path_params=params)
        return None
