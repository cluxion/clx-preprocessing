"""Durable queue bridge with a three-tier backend chain.

Backend resolution order (override with CLUXION_QUEUE_BACKEND):
1. ``native``     — in-process Rust extension (cluxion_queue_native), microsecond ops
2. ``subprocess`` — Rust CLI binary over JSON stdin/stdout
3. ``python``     — pure-Python SQLite fallback (py_queue), schema-identical
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import py_queue

if TYPE_CHECKING:
    from collections.abc import Mapping

QUEUE_BIN_ENV = "CLUXION_QUEUE_BIN"
QUEUE_STORE_ENV = "CLUXION_QUEUE_STORE_DIR"
QUEUE_BACKEND_ENV = "CLUXION_QUEUE_BACKEND"
_DEFAULT_STORE = Path.home() / ".local" / "share" / "cluxion-agentplugin-preprocessing" / "queue"
_SUBPROCESS_TIMEOUT_SECONDS = 15.0

_logger = logging.getLogger(__name__)

try:
    import cluxion_queue_native as _native
except ImportError:
    _native = None


def resolve_backend() -> str:
    """Pick the best available backend, honoring the env override."""
    forced = os.environ.get(QUEUE_BACKEND_ENV, "").strip().lower()
    if forced in ("native", "subprocess", "python"):
        return forced
    if _native is not None:
        return "native"
    if shutil.which(_queue_binary()) is not None:
        return "subprocess"
    return "python"


def queue_available() -> bool:
    """Return True when any queue backend is usable (always true: python fallback)."""
    return True


def default_store_dir() -> Path:
    return Path(os.environ.get(QUEUE_STORE_ENV, str(_DEFAULT_STORE))).expanduser()


def enqueue_work(payload: Mapping[str, object], *, store_dir: Path | None = None) -> dict[str, object]:
    """Enqueue work via Rust when available, else raise for Python fallback."""
    return _invoke("enqueue", payload, store_dir=store_dir)


def dequeue_work(*, store_dir: Path | None = None) -> dict[str, object]:
    return _invoke("dequeue", {}, store_dir=store_dir)


def peek_order(*, store_dir: Path | None = None, limit: int = 16) -> dict[str, object]:
    return _invoke("peek", {"limit": limit}, store_dir=store_dir)


def persist_dispatch_bundle(
    work_id: str, bundle: Mapping[str, object], *, store_dir: Path | None = None
) -> dict[str, object]:
    return _invoke(
        "persist",
        {"work_id": work_id, "bundle": dict(bundle)},
        store_dir=store_dir,
    )


def next_dispatch_step(work_id: str, *, store_dir: Path | None = None) -> dict[str, object]:
    return _invoke("next", {"work_id": work_id}, store_dir=store_dir)


def record_dispatch_step(
    work_id: str,
    step_id: str,
    *,
    result: str = "",
    error: str = "",
    failed: bool = False,
    store_dir: Path | None = None,
) -> dict[str, object]:
    return _invoke(
        "record",
        {
            "work_id": work_id,
            "step_id": step_id,
            "result": result,
            "error": error,
            "failed": failed,
        },
        store_dir=store_dir,
    )


def build_briefing(work_id: str, *, store_dir: Path | None = None) -> dict[str, object]:
    return _invoke("brief", {"work_id": work_id}, store_dir=store_dir)


def queue_status(*, store_dir: Path | None = None) -> dict[str, object]:
    return _invoke("status", {}, store_dir=store_dir)


def compress_context(payload: Mapping[str, object]) -> dict[str, object]:
    """Deterministic context compression — pure function, no store_dir involved."""
    from cluxion_runtime.core import context_compress

    body = dict(payload)
    backend = resolve_backend()
    if backend == "python":
        return context_compress.compress(body)
    if backend == "native":
        stage1 = _invoke_native("context-compress", body)
    else:
        stage1 = _invoke_subprocess("context-compress", body)
    return _finalize_context_compress(body, stage1)


def _context_trigger_ratio(payload: Mapping[str, object]) -> float:
    from cluxion_runtime.core.context_compress import DEFAULT_TRIGGER_RATIO

    value = payload.get("trigger_ratio")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 < float(value) < 1.0:
        return float(value)
    return DEFAULT_TRIGGER_RATIO


def _context_bool_flag(payload: Mapping[str, object], key: str, default: bool) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return default


def _finalize_context_compress(body: Mapping[str, object], stage1: dict[str, object]) -> dict[str, object]:
    """After Rust Stage-1, continue the Python pipeline when still above trigger."""
    from cluxion_runtime.core import context_compress

    trigger_ratio = _context_trigger_ratio(body)
    usage_after = float(stage1.get("usage_after", 0))
    if usage_after <= trigger_ratio:
        stage1["reached_target"] = True
        return stage1

    enable_llm = _context_bool_flag(body, "enable_llm_summary", True)
    enable_forget = _context_bool_flag(body, "enable_forget", True)
    if not enable_llm and not enable_forget:
        stage1["reached_target"] = False
        if stage1.get("ai_summary_request"):
            stage1["requires_summary"] = True
        return stage1

    continued_body = dict(body)
    messages = stage1.get("messages")
    if isinstance(messages, list):
        continued_body["messages"] = messages
    continued = context_compress.compress(continued_body)
    continued_usage = float(continued.get("usage_after", 1.0))
    continued["reached_target"] = continued_usage <= trigger_ratio
    if not continued["reached_target"] and continued.get("ai_summary_request"):
        continued["requires_summary"] = True

    stage1_stages = stage1.get("stages_applied")
    continued_stages = continued.get("stages_applied")
    if isinstance(stage1_stages, list) and isinstance(continued_stages, list):
        merged = list(stage1_stages)
        for stage in continued_stages:
            if stage not in merged:
                merged.append(stage)
        continued["stages_applied"] = merged
    return continued


def _invoke(command: str, payload: Mapping[str, object], *, store_dir: Path | None) -> dict[str, object]:
    body = dict(payload)
    body["store_dir"] = str(default_store_dir() if store_dir is None else store_dir)
    backend = resolve_backend()
    if backend == "native":
        return _invoke_native(command, body)
    if backend == "subprocess":
        try:
            return _invoke_subprocess(command, body)
        except subprocess.TimeoutExpired as exc:
            if os.environ.get(QUEUE_BACKEND_ENV, "").strip().lower() == "subprocess":
                raise RuntimeError(_timeout_error(command, exc.timeout)) from exc
            _logger.warning("%s; using Python queue fallback", _timeout_error(command, exc.timeout))
            return py_queue.run(command, body)
    return py_queue.run(command, body)


def _invoke_native(command: str, body: dict[str, object]) -> dict[str, object]:
    if _native is None:
        raise RuntimeError("native backend forced but cluxion_queue_native is not importable")
    raw = _native.run(command, json.dumps(body, ensure_ascii=False))
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"cluxion-queue {command} returned non-object JSON")
    return parsed


def _invoke_subprocess(command: str, body: dict[str, object]) -> dict[str, object]:
    binary = _queue_binary()
    if shutil.which(binary) is None:
        raise RuntimeError("cluxion-queue binary not found")
    completed = subprocess.run(
        [binary, command],
        input=json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        stdout = completed.stdout.strip()
        try:
            error = json.loads(stdout).get("error", "")
        except (json.JSONDecodeError, AttributeError):
            error = ""
        raise RuntimeError(error or completed.stderr.strip() or f"cluxion-queue {command} failed")
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"cluxion-queue {command} returned non-object JSON")
    return parsed


def _timeout_error(command: str, timeout: float | None) -> str:
    return f"cluxion-queue {command} timed out after {timeout or _SUBPROCESS_TIMEOUT_SECONDS:g}s"


def _queue_binary() -> str:
    configured = os.environ.get(QUEUE_BIN_ENV, "").strip()
    if configured:
        return configured
    local = Path(__file__).resolve().parents[3] / "rust" / "cluxion_queue" / "target" / "release" / "cluxion-queue"
    if local.exists():
        return str(local)
    return "cluxion-queue"


__all__ = [
    "QUEUE_BACKEND_ENV",
    "QUEUE_BIN_ENV",
    "QUEUE_STORE_ENV",
    "build_briefing",
    "compress_context",
    "default_store_dir",
    "dequeue_work",
    "enqueue_work",
    "next_dispatch_step",
    "peek_order",
    "persist_dispatch_bundle",
    "queue_available",
    "queue_status",
    "record_dispatch_step",
    "resolve_backend",
]
