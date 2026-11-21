from __future__ import annotations

import json
import math
from dataclasses import dataclass
from itertools import islice
from typing import Any

from app.errors import ValidationAppError
from app.schemas import is_invalid_path_segment

MAX_JSON_DEPTH = 32
MAX_DEPTH_PLACEHOLDER = "[max_depth_exceeded]"
MAX_ORDER_ID_LEN = 32
MAX_AUDIT_KEY_LEN = 128
MAX_AUDIT_DICT_KEYS = 64
KEY_TRUNCATION_SUFFIX = "..."
TRUNCATED_KEYS_FIELD = "_truncated_keys"


@dataclass(frozen=True)
class RawBody:
    data: bytes
    too_large: bool = False


class NonFiniteNumberError(ValueError):
    """Raised when JSON input contains NaN, Infinity, or -Infinity."""


class MaxDepthExceededError(ValueError):
    """Raised when JSON input nesting exceeds MAX_JSON_DEPTH."""


class InvalidStringError(ValueError):
    """Raised when JSON strings contain lone surrogates not encodable as UTF-8."""


def _reject_constant(token: str) -> Any:
    raise NonFiniteNumberError(f"非有限数值常量: {token}")


def is_valid_utf8_string(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def sanitize_utf8_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in value)
    return value


def format_non_finite_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def validate_strict_json_value(value: Any, *, max_depth: int = MAX_JSON_DEPTH) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            next_depth = depth + 1
            if next_depth > max_depth:
                raise MaxDepthExceededError("请求体嵌套过深")
            for k, v in current.items():
                if not is_valid_utf8_string(k):
                    raise InvalidStringError("请求体含非法字符")
                stack.append((v, next_depth))
        elif isinstance(current, list):
            next_depth = depth + 1
            if next_depth > max_depth:
                raise MaxDepthExceededError("请求体嵌套过深")
            for item in current:
                stack.append((item, next_depth))
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise NonFiniteNumberError("非有限浮点数")
        elif isinstance(current, str):
            if not is_valid_utf8_string(current):
                raise InvalidStringError("请求体含非法字符")


def loads_strict_json(text: str, *, max_depth: int = MAX_JSON_DEPTH) -> Any:
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise MaxDepthExceededError("请求体嵌套过深") from exc
    validate_strict_json_value(parsed, max_depth=max_depth)
    return parsed


def parse_strict_json_dict(raw_body: bytes, *, allow_empty: bool = False) -> dict[str, Any]:
    if not raw_body.strip():
        if allow_empty:
            return {}
        raise ValidationAppError([{"field": "body", "message": "请求体不能为空"}])
    try:
        parsed = loads_strict_json(raw_body.decode("utf-8"))
    except MaxDepthExceededError as exc:
        raise ValidationAppError(
            [{"field": "body", "message": "请求体嵌套过深"}],
            message="请求体嵌套过深。",
        ) from exc
    except InvalidStringError as exc:
        raise ValidationAppError(
            [{"field": "body", "message": "请求体含非法字符"}],
            message="请求体含非法字符。",
        ) from exc
    except NonFiniteNumberError as exc:
        raise ValidationAppError(
            [{"field": "body", "message": "请求体含非法数值"}],
            message="请求体含非法数值。",
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationAppError(
            [{"field": "body", "message": "请求体必须是合法 JSON"}],
            message="请求体 JSON 格式错误。",
        ) from exc
    except ValueError as exc:
        raise ValidationAppError(
            [{"field": "body", "message": "请求体含非法数值"}],
            message="请求体含非法数值。",
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationAppError(
            [{"field": "body", "message": "请求体必须是 JSON 对象"}],
            message="请求体 JSON 格式错误。",
        )
    return parsed


def _sanitize_scalar(value: Any, *, max_len: int | None) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return format_non_finite_float(value)
    if isinstance(value, str):
        clean = sanitize_utf8_string(value)
        if max_len is not None and len(clean) > max_len:
            return clean[:max_len]
        return clean
    return value


def _sanitize_child(
    item: Any,
    depth: int,
    *,
    max_len: int | None,
    max_depth: int,
    stack: list[tuple[Any, Any, int]],
) -> Any:
    if isinstance(item, dict):
        if depth >= max_depth:
            return MAX_DEPTH_PLACEHOLDER
        child_dict: dict[str, Any] = {}
        stack.append((item, child_dict, depth + 1))
        return child_dict
    if isinstance(item, list):
        if depth >= max_depth:
            return MAX_DEPTH_PLACEHOLDER
        child_list: list[Any] = []
        stack.append((item, child_list, depth + 1))
        return child_list
    return _sanitize_scalar(item, max_len=max_len)


def _truncate_key(key: str, *, max_key_len: int | None) -> str:
    if max_key_len is None or len(key) <= max_key_len:
        return key
    if max_key_len <= len(KEY_TRUNCATION_SUFFIX):
        return key[:max_key_len]
    return key[: max_key_len - len(KEY_TRUNCATION_SUFFIX)] + KEY_TRUNCATION_SUFFIX


def sanitize_json_value(
    value: Any,
    *,
    redact_fields: frozenset[str] = frozenset(),
    max_len: int | None = None,
    max_depth: int = MAX_JSON_DEPTH,
    max_key_len: int | None = None,
    max_dict_keys: int | None = None,
) -> Any:
    if not isinstance(value, (dict, list)):
        return _sanitize_scalar(value, max_len=max_len)

    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any, int]] = [(value, root, 1)]
    while stack:
        src, dst, depth = stack.pop()
        if isinstance(src, dict):
            if max_dict_keys is not None and len(src) > max_dict_keys:
                items_iter = islice(src.items(), max_dict_keys)
                dropped_keys = len(src) - max_dict_keys
            else:
                items_iter = iter(src.items())
                dropped_keys = 0
            for raw_key, item in items_iter:
                clean_key = sanitize_utf8_string(str(raw_key))
                key = _truncate_key(clean_key, max_key_len=max_key_len)
                if clean_key in redact_fields:
                    dst[key] = "***"
                else:
                    dst[key] = _sanitize_child(
                        item,
                        depth,
                        max_len=max_len,
                        max_depth=max_depth,
                        stack=stack,
                    )
            if dropped_keys > 0:
                dst[TRUNCATED_KEYS_FIELD] = dropped_keys
        else:
            for item in src:
                dst.append(
                    _sanitize_child(
                        item,
                        depth,
                        max_len=max_len,
                        max_depth=max_depth,
                        stack=stack,
                    )
                )
    return root


def _safe_parse_int(token: str) -> int | str:
    try:
        return int(token)
    except ValueError:
        return token


def _is_extractable_parse_error(exc: ValidationAppError) -> bool:
    cause = exc.__cause__
    return isinstance(cause, ValueError) and not isinstance(
        cause, (UnicodeDecodeError, json.JSONDecodeError)
    )


def extract_audit_json_dict(
    raw_body: bytes,
    exc: ValidationAppError,
    *,
    max_depth: int = MAX_JSON_DEPTH,
) -> dict[str, Any]:
    if not _is_extractable_parse_error(exc):
        return {}
    try:
        parsed = json.loads(raw_body.decode("utf-8"), parse_int=_safe_parse_int)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return sanitize_json_value(parsed, max_depth=max_depth)


def extract_valid_order_id(arguments: dict[str, Any]) -> str | None:
    raw = arguments.get("order_id")
    if not isinstance(raw, str):
        return None
    if not raw.strip() or len(raw) > MAX_ORDER_ID_LEN or is_invalid_path_segment(raw):
        return None
    if "\ufffd" in raw or not is_valid_utf8_string(raw):
        return None
    return raw
