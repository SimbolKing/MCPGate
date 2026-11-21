from __future__ import annotations

from fastapi import Depends, Request

from app.auth import get_settings
from app.config import Settings
from app.json_utils import RawBody


def _parse_non_negative_content_length(raw_value: str) -> int | None:
    stripped = raw_value.strip()
    if stripped.isascii() and stripped.isdigit():
        return int(stripped)
    return None


async def read_raw_body(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> RawBody:
    max_bytes = settings.max_body_bytes
    for raw_cl in request.headers.getlist("content-length"):
        parsed_cl = _parse_non_negative_content_length(raw_cl)
        if parsed_cl is not None and parsed_cl > max_bytes:
            return RawBody(data=b"", too_large=True)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return RawBody(data=b"", too_large=True)
        chunks.append(chunk)
    return RawBody(data=b"".join(chunks), too_large=False)
