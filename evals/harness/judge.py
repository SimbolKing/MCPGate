"""Blind LLM verdict judge with evidence validation, disk cache, and calibration (5.5.4)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from harness.asserts import JudgeVerdictResult, redact_tokens
from harness.config import EVALS_DIR, Settings
from harness.schema import JUDGE_LABELS, CalibrationSample

CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_cache_key(model: str, prompt_sha256: str, query: str, answer: str) -> str:
    canonical = json.dumps([model, prompt_sha256, query, answer], ensure_ascii=False)
    return sha256_text(canonical)


def parse_judge_json(raw_content: str, answer: str) -> tuple[bool, str, str, str]:
    """Validate judge JSON output: label in enum AND evidence is exact substring of answer."""
    text = raw_content.strip()
    fence_match = CODE_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1)
    try:
        parsed = json.loads(text)
    except ValueError:
        return False, "unknown", "", "评审输出不是合法 JSON"

    if not isinstance(parsed, dict):
        return False, "unknown", "", "评审输出不是 JSON 对象"

    label = parsed.get("label")
    evidence = parsed.get("evidence")
    if not isinstance(label, str) or label not in JUDGE_LABELS:
        return False, "unknown", "", f"评审标签不在枚举内: {label!r}"
    if not isinstance(evidence, str) or not evidence.strip():
        return False, "unknown", "", "评审缺少 evidence"
    if evidence not in answer:
        return False, "unknown", evidence, "评审 evidence 不是回答的逐字子串"

    return True, label, evidence, ""


@dataclass
class CalibrationItemResult:
    id: str
    expected_label: str
    predicted_label: str
    valid: bool
    passed: bool
    evidence: str = ""
    error: str = ""


@dataclass
class CalibrationReport:
    passed: bool
    total: int
    correct: int
    items: list[CalibrationItemResult] = field(default_factory=list)


class VerdictJudge:
    """OpenAI-compatible `/chat/completions` blind classifier with caching (5.5.4)."""

    def __init__(
        self,
        settings: Settings,
        *,
        evals_dir: Path = EVALS_DIR,
        http_client: httpx.Client | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._evals_dir = evals_dir
        self._prompt_path = evals_dir / "judge" / "prompt.md"
        self._prompt_text = self._prompt_path.read_text(encoding="utf-8")
        self._prompt_sha256 = sha256_text(self._prompt_text)
        self._cache_dir = cache_dir or (evals_dir / ".cache" / "judge")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=30.0)
        self.enabled: bool = settings.judge_configured
        self.calibration_passed: bool | None = None
        self.disabled_reason: str = "" if self.enabled else "未配置 EVALS_JUDGE_*"
        self.call_count: int = 0
        self.calibration_call_count: int = 0
        self.cache_hits: int = 0
        self.invalid_count: int = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def prompt_sha256(self) -> str:
        return self._prompt_sha256

    @property
    def model(self) -> str:
        return self._settings.judge_model.strip()

    def _cache_file(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        path = self._cache_file(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_cache(self, key: str, payload: dict[str, Any]) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file(key).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _call_remote(self, query: str, answer: str) -> tuple[str | None, str]:
        url = f"{self._settings.judge_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._settings.judge_api_key.strip()}",
            "Content-Type": "application/json",
        }
        user_msg = f"【用户问题】\n{query}\n\n【助手回答】\n{answer}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 256,
            "messages": [
                {"role": "system", "content": self._prompt_text},
                {"role": "user", "content": user_msg},
            ],
        }
        try:
            resp = self._client.post(url, json=payload, headers=headers, timeout=30.0)
        except httpx.HTTPError as exc:
            return None, f"评审请求失败: {exc}"
        if resp.status_code != 200:
            return None, f"评审 HTTP 状态异常: {resp.status_code}"
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return None, f"评审响应结构异常: {exc}"
        if not isinstance(content, str):
            return None, "评审响应 content 非字符串"
        return content, ""

    def classify(
        self,
        query: str,
        answer: str,
        *,
        use_cache: bool = True,
        force: bool = False,
        is_calibration: bool = False,
    ) -> JudgeVerdictResult:
        if not force and not self.enabled:
            return JudgeVerdictResult(
                status="disabled", label="unknown", error=self.disabled_reason
            )

        # L10: always sanitize JWT patterns before cache lookup, outbound request, or cache write
        safe_query = redact_tokens(query)
        safe_answer = redact_tokens(answer)

        key = build_cache_key(self.model, self._prompt_sha256, safe_query, safe_answer)
        if use_cache:
            cached_data = self._read_cache(key)
            if cached_data is not None:
                if not is_calibration:
                    self.cache_hits += 1
                res = JudgeVerdictResult(
                    status=str(cached_data.get("status", "invalid")),
                    label=str(cached_data.get("label", "unknown")),
                    evidence=str(cached_data.get("evidence", "")),
                    cached=True,
                    raw_output=str(cached_data.get("raw_output", "")),
                    error=str(cached_data.get("error", "")),
                )
                if res.status == "invalid" and not is_calibration:
                    self.invalid_count += 1
                return res

        if is_calibration:
            self.calibration_call_count += 1
        else:
            self.call_count += 1
        raw_content, err = self._call_remote(safe_query, safe_answer)
        if raw_content is None:
            return JudgeVerdictResult(status="unavailable", label="unknown", error=err)

        safe_raw = redact_tokens(raw_content)
        valid, label, evidence, parse_err = parse_judge_json(safe_raw, safe_answer)
        if not valid:
            if not is_calibration:
                self.invalid_count += 1
            res = JudgeVerdictResult(
                status="invalid",
                label="unknown",
                evidence=evidence,
                cached=False,
                raw_output=safe_raw,
                error=parse_err,
            )
        else:
            res = JudgeVerdictResult(
                status="ok",
                label=label,
                evidence=evidence,
                cached=False,
                raw_output=safe_raw,
                error="",
            )

        if use_cache:
            self._write_cache(key, redact_tokens(res.to_dict()))
        return res

    def calibrate(
        self,
        samples: list[CalibrationSample],
        *,
        use_cache: bool = False,
    ) -> CalibrationReport:
        """Run calibration set; disable judge if any sample fails (5.5.4)."""
        items: list[CalibrationItemResult] = []
        correct = 0
        for sample in samples:
            res = self.classify(
                sample.query,
                sample.answer,
                use_cache=use_cache,
                force=True,
                is_calibration=True,
            )
            valid = res.status == "ok"
            passed = valid and res.label == sample.expected_label
            if passed:
                correct += 1
            items.append(
                CalibrationItemResult(
                    id=sample.id,
                    expected_label=sample.expected_label,
                    predicted_label=res.label,
                    valid=valid,
                    passed=passed,
                    evidence=res.evidence,
                    error=res.error,
                )
            )

        all_passed = len(samples) > 0 and correct == len(samples)
        self.calibration_passed = all_passed
        if not all_passed:
            self.enabled = False
            self.disabled_reason = f"校准集未全部通过 ({correct}/{len(samples)})"
        else:
            self.enabled = True
            self.disabled_reason = ""
        return CalibrationReport(
            passed=all_passed,
            total=len(samples),
            correct=correct,
            items=items,
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "configured": self._settings.judge_configured,
            "enabled": self.enabled,
            "model": self.model or None,
            "prompt_sha256": self._prompt_sha256,
            "calibration_passed": self.calibration_passed,
            "disabled_reason": self.disabled_reason or None,
        }
