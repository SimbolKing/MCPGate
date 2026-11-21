"""Environment preflight checks (P1–P7), reset workflow, and dirty/date tracking (5.8)."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from harness.clients import ChatClient, GuardClient, McpClient
from harness.config import REPO_ROOT, Settings
from harness.schema import SuiteConfig
from harness.tokens import TokenProvider

CN_TZ = timezone(timedelta(hours=8))


class EnvironmentResetError(RuntimeError):
    """Raised when resetting services or waiting for readiness fails (exit code 2)."""


@dataclass
class PreflightItem:
    id: str
    name: str
    passed: bool
    detail: str = ""


@dataclass
class PreflightReport:
    passed: bool
    items: list[PreflightItem] = field(default_factory=list)


class EnvironmentManager:
    """Manages preflight checks, data reset, audit cursor, and dirty/date state (5.8)."""

    def __init__(
        self,
        settings: Settings,
        suite: SuiteConfig,
        *,
        mcp_client: McpClient,
        guard_client: GuardClient,
        chat_client: ChatClient | None = None,
        token_provider: TokenProvider | object,
        repo_root: Path = REPO_ROOT,
        cmd_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self._settings = settings
        self._suite = suite
        self._mcp = mcp_client
        self._guard = guard_client
        self._chat = chat_client
        self._tokens = token_provider
        self._repo_root = repo_root
        self._cmd_runner = cmd_runner
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._today_fn = today_fn or (lambda: datetime.now(CN_TZ).date())

        self.dirty: bool = True
        self.last_reset_date: date | None = None
        self.baseline_refund_ids: set[str] = set()
        self.audit_cursor: int = 0
        self.reset_count: int = 0

    def today_cn(self) -> date:
        return self._today_fn()

    def mark_dirty(self) -> None:
        self.dirty = True

    def needs_reset(self, *, fresh: bool = False) -> bool:
        if self.dirty or fresh:
            return True
        if self.last_reset_date is None or self.last_reset_date != self.today_cn():
            return True
        return False

    def _run_cmd(
        self, cmd: list[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._cmd_runner(
            cmd,
            cwd=cwd or self._repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

    def _auditor_token(self) -> str:
        tok = self._tokens.get_token(self._suite.auditor, "valid")  # type: ignore[attr-defined]
        assert isinstance(tok, str) and tok
        return tok

    def reset(self) -> None:
        """Execute 5-step reset procedure per 5.8.2."""
        for label, raw_cmd in (
            ("reset guard", self._settings.reset_guard_cmd),
            ("reset mock-api", self._settings.reset_mockapi_cmd),
        ):
            proc = self._run_cmd(shlex.split(raw_cmd))
            if proc.returncode != 0:
                raise EnvironmentResetError(
                    f"{label} 失败 (exit={proc.returncode}): {proc.stderr.strip()}"
                )

        auditor_token = self._auditor_token()
        deadline = self._monotonic() + self._settings.ready_timeout_seconds
        refunds_resp: dict | None = None
        while True:
            resp = self._mcp.call_tool("list_refunds", {}, auditor_token)
            if resp.get("result") == "ok":
                refunds_resp = resp
                break
            if self._monotonic() >= deadline:
                raise EnvironmentResetError(
                    f"重置后等待 MCP 就绪超时 ({self._settings.ready_timeout_seconds}s)"
                )
            self._sleep(1.0)

        approvals_resp = self._mcp.call_tool("list_approvals", {}, auditor_token)
        if approvals_resp.get("result") != "ok":
            raise EnvironmentResetError(
                f"重置后核对审批单失败: result={approvals_resp.get('result')}"
            )
        total_approvals = approvals_resp.get("data", {}).get("total")
        if total_approvals != 0:
            raise EnvironmentResetError(f"重置后审批单总数不为 0: total={total_approvals}")

        refund_items = refunds_resp.get("data", {}).get("items") or []
        self.baseline_refund_ids = {str(item["refund_id"]) for item in refund_items}
        self.audit_cursor = self._guard.latest_audit_id(auditor_token)
        self.dirty = False
        self.last_reset_date = self.today_cn()
        self.reset_count += 1

    def run_preflight(
        self,
        *,
        mode: Literal["quick", "full"] = "quick",
        layers: set[str] | None = None,
    ) -> PreflightReport:
        """Run preflight checks P1–P6 (and P7 when mode == 'full') per 5.8.1."""
        active_layers = layers if layers is not None else {"tool", "guard", "dialogue"}
        need_dialogue = "dialogue" in active_layers
        items: list[PreflightItem] = []

        # P1: Reachability
        guard_status = self._guard.healthz()
        mcp_unauth = self._mcp.call_tool("get_order", {"order_id": "A1023"}, token=None)
        chat_ok = True
        chat_status: int | None = None
        if need_dialogue:
            chat_status = self._chat.healthz() if self._chat is not None else 0
            chat_ok = chat_status == 200
        p1_ok = guard_status == 200 and mcp_unauth.get("http_status") == 401 and chat_ok
        p1_detail = f"guard={guard_status}, mcp_unauth={mcp_unauth.get('http_status')}" + (
            f", chat={chat_status}" if need_dialogue else ""
        )
        items.append(PreflightItem("P1", "服务可达性", p1_ok, p1_detail))
        if not p1_ok:
            return PreflightReport(False, items)

        # P2: Mint 6 valid tokens + 1 expired token
        try:
            usernames = list(self._suite.accounts.keys())
            self._tokens.prime_accounts(usernames, expired_user=usernames[0])  # type: ignore[attr-defined]
            items.append(PreflightItem("P2", "签发账号与过期 token", True, f"{len(usernames)} + 1"))
        except Exception as exc:
            items.append(PreflightItem("P2", "签发账号与过期 token", False, str(exc)))
            return PreflightReport(False, items)

        # P3: Initial reset & baseline verification
        try:
            self.reset()
            items.append(
                PreflightItem(
                    "P3",
                    "初始重置与状态核对",
                    True,
                    f"baseline_refunds={len(self.baseline_refund_ids)}, cursor={self.audit_cursor}",
                )
            )
        except Exception as exc:
            items.append(PreflightItem("P3", "初始重置与状态核对", False, str(exc)))
            return PreflightReport(False, items)

        # P4: gateway/scripts/smoke.py + reset
        smoke_proc = self._run_cmd(["python3", "gateway/scripts/smoke.py"])
        if smoke_proc.returncode != 0:
            items.append(
                PreflightItem(
                    "P4",
                    "gateway/scripts/smoke.py (V1–V34)",
                    False,
                    smoke_proc.stderr.strip() or smoke_proc.stdout.strip()[-300:],
                )
            )
            return PreflightReport(False, items)
        try:
            self.reset()
        except Exception as exc:
            items.append(PreflightItem("P4", "smoke 后重置", False, str(exc)))
            return PreflightReport(False, items)
        items.append(PreflightItem("P4", "gateway/scripts/smoke.py (V1–V34)", True, "pass + reset"))

        if need_dialogue:
            # P5: knowledge-base/scripts/probe.py
            probe_proc = self._run_cmd(
                ["uv", "run", "python", "scripts/probe.py"],
                cwd=self._repo_root / "knowledge-base",
            )
            if probe_proc.returncode != 0:
                items.append(
                    PreflightItem(
                        "P5",
                        "knowledge-base/scripts/probe.py",
                        False,
                        probe_proc.stderr.strip() or probe_proc.stdout.strip()[-300:],
                    )
                )
                return PreflightReport(False, items)
            items.append(PreflightItem("P5", "knowledge-base/scripts/probe.py", True, "pass"))

            # P6: Chat liveness probe
            assert self._chat is not None
            wang_tok = self._tokens.get_token("wang", "valid")  # type: ignore[attr-defined]
            chat_resp = self._chat.send_message("提交退款后怎么查进度？", wang_tok)
            p6_ok = (
                chat_resp.get("http_status") == 200
                and len(chat_resp.get("trace") or []) == 0
                and len(chat_resp.get("citations") or []) > 0
            )
            items.append(
                PreflightItem(
                    "P6",
                    "对话入口探活",
                    p6_ok,
                    f"status={chat_resp.get('http_status')}, "
                    f"trace={len(chat_resp.get('trace') or [])}, "
                    f"citations={len(chat_resp.get('citations') or [])}",
                )
            )
            if not p6_ok:
                return PreflightReport(False, items)

            # P7: workflows/scripts/e2e.py (only when mode == full)
            if mode == "full":
                e2e_proc = self._run_cmd(
                    ["uv", "run", "python", "scripts/e2e.py"],
                    cwd=self._repo_root / "workflows",
                )
                if e2e_proc.returncode != 0:
                    items.append(
                        PreflightItem(
                            "P7",
                            "workflows/scripts/e2e.py (E1–E22)",
                            False,
                            e2e_proc.stderr.strip() or e2e_proc.stdout.strip()[-300:],
                        )
                    )
                    return PreflightReport(False, items)
                try:
                    self.reset()
                except Exception as exc:
                    items.append(PreflightItem("P7", "e2e 后重置", False, str(exc)))
                    return PreflightReport(False, items)
                items.append(
                    PreflightItem("P7", "workflows/scripts/e2e.py (E1–E22)", True, "pass + reset")
                )

        return PreflightReport(True, items)
