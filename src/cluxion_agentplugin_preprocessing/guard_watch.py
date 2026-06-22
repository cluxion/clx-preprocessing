"""Hermes hook wiring for automatic guard daemon watch.

The hook surface is intentionally best-effort: failures are reported once to
stderr and never raised into the host agent.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from typing import Any

from cluxion_runtime.resources import guard_bridge

AUTOSTART_ENV = "CLUXION_GUARD_AUTOSTART"
AUTO_APPLY_ENV = "CLUXION_GUARD_AUTO_APPLY"
WATCH_INTERVAL_ENV = "CLUXION_GUARD_WATCH_INTERVAL"
DEFAULT_WATCH_INTERVAL_SECONDS = 30.0
WARNING_INTERVAL_SECONDS = 300.0

_lock = threading.Lock()
_last_watch_at: float | None = None
_last_warning_at: float | None = None


def on_session_end(**_: Any) -> None:
    """Stop the guard daemon on clean session end so orphans do not linger."""
    with contextlib.suppress(Exception):
        guard_bridge.stop_daemon()


def on_session_start(**_: Any) -> None:
    """Start the guard daemon unless ``CLUXION_GUARD_AUTOSTART=0`` or ``false``.

    Startup is idempotent: an already-running daemon is success. Hook failures
    are logged as one concise stderr warning and never propagated to the host.
    """
    _maybe_apply_hermes_deliver_patch()
    if not _autostart_enabled():
        return
    try:
        result = guard_bridge.start_daemon()
        guard_bridge.touch_heartbeat()
    except Exception as exc:
        _warn(f"cluxion guard autostart failed: {exc}")
        return
    if not result.get("ok", False):
        reason = result.get("reason") or result.get("error") or "unknown"
        _warn(f"cluxion guard autostart failed: {reason}")


def _maybe_apply_hermes_deliver_patch() -> None:
    """Best-effort Hermes deliver=agent patch (``CLUXION_HERMES_PATCH_AUTOFIX``, default on)."""
    try:
        from cluxion_agentplugin_preprocessing import hermes_deliver_patch

        if not hermes_deliver_patch.autostart_enabled():
            return
        status = hermes_deliver_patch.patch_status()
        if status.status == "applied":
            return
        result = hermes_deliver_patch.ensure_applied()
        if result.status != "applied" and result.status != "no_hermes":
            _warn(f"cluxion hermes deliver patch: {result.detail}")
    except Exception as exc:
        _warn(f"cluxion hermes deliver patch failed: {exc}")


def post_tool_call(**_: Any) -> None:
    """Run a throttled guard watch after tool calls.

    By default this is report-only and calls ``auto_enforce(..., dry_run=True)``.
    Set ``CLUXION_GUARD_AUTO_APPLY=1`` or ``true`` to pass ``dry_run=False`` and
    let the existing owned-only fail-closed enforcement path terminate
    candidates.
    """
    global _last_warning_at, _last_watch_at

    with contextlib.suppress(Exception):
        guard_bridge.touch_heartbeat()

    now = time.monotonic()
    try:
        with _lock:
            if _last_watch_at is not None and now - _last_watch_at < _watch_interval_seconds():
                return
            _last_watch_at = now
            result = guard_bridge.auto_enforce([os.getpid()], dry_run=not _auto_apply_enabled())
            should_warn = (
                bool(result.get("triggered", False))
                and bool(result.get("dry_run", True))
                and (_last_warning_at is None or now - _last_warning_at >= WARNING_INTERVAL_SECONDS)
            )
            if should_warn:
                _last_warning_at = now
    except Exception as exc:
        _warn(f"cluxion guard watch failed: {exc}")
        return
    if should_warn:
        _warn_triggered(result)


def _autostart_enabled() -> bool:
    return os.environ.get(AUTOSTART_ENV, "1").strip().lower() not in {"0", "false"}


def _auto_apply_enabled() -> bool:
    return os.environ.get(AUTO_APPLY_ENV, "").strip().lower() in {"1", "true"}


def _watch_interval_seconds() -> float:
    raw = os.environ.get(WATCH_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_WATCH_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_WATCH_INTERVAL_SECONDS


def _warn_triggered(result: dict[str, Any]) -> None:
    pids = [str(entry.get("pid")) for entry in result.get("candidates", []) if isinstance(entry, dict)]
    reasons = [str(reason) for reason in result.get("trigger_reasons", [])]
    _warn(
        "cluxion guard triggered"
        f" candidates={','.join(pids) if pids else 'none'}"
        f" reasons={'; '.join(reasons) if reasons else 'unknown'}"
    )


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


__all__ = ["on_session_end", "on_session_start", "post_tool_call"]
