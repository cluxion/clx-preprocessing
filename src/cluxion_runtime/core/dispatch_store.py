"""Small file-based store for Hermes host-model segment dispatch."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    _fcntl = None

if TYPE_CHECKING:
    from cluxion_runtime.core.types import HarnessPlan, QueueSegment

DISPATCH_DIR_ENV = "CLUXION_PREPROCESS_DISPATCH_DIR"
_LEGACY_DISPATCH_DIR_ENV = "HERMES_CLUXION_DISPATCH_DIR"
_DEFAULT_BASE_DIR = Path.home() / ".local" / "share" / "cluxion-agentplugin-preprocessing" / "queue" / "dispatch"
# A step abandoned in 'running' (crashed or killed worker) becomes claimable
# again after this lease; matches the LoopAutoOptions.timeout_seconds default,
# so no live worker can legitimately hold a step longer. Mirrored in
# py_queue._RUNNING_LEASE_SECONDS and dispatch.rs RUNNING_LEASE_SECS.
RUNNING_LEASE_SECONDS = 600.0


@dataclass(frozen=True)
class DispatchStepRecord:
    """Stored segment execution step."""

    step_id: str
    segment_id: str
    checksum: str
    token_estimate: int
    content: str
    status: str
    result: str = ""
    error: str = ""


class DispatchStoreError(RuntimeError):
    """Raised when a dispatch bundle is missing or corrupted."""


def default_dispatch_dir() -> Path:
    """Return the per-user dispatch store path."""
    for env_name in (DISPATCH_DIR_ENV, _LEGACY_DISPATCH_DIR_ENV):
        value = os.environ.get(env_name, "").strip()
        if value:
            return Path(value).expanduser()
    return _DEFAULT_BASE_DIR


def persist_dispatch_bundle(plan: HarnessPlan, *, dispatch_dir: Path | None = None) -> Path | None:
    """Persist segment content as a separate bundle only for plans requiring the queue."""
    if not plan.execution.queue_required:
        return None
    bundle = _bundle_from_plan(plan)
    target_dir = default_dispatch_dir() if dispatch_dir is None else dispatch_dir
    if (
        plan.queue_backend in ("native", "subprocess")
        and dispatch_dir is None
        and not _custom_dispatch_dir_configured()
    ):
        try:
            from cluxion_runtime.resources.queue_bridge import default_store_dir
            from cluxion_runtime.resources.queue_bridge import persist_dispatch_bundle as rust_persist

            result = rust_persist(plan.item.work_id, bundle, store_dir=default_store_dir())
            if result.get("ok") and result.get("stored"):
                return Path(str(result.get("path", "")))
        except RuntimeError:
            pass
    target_dir.mkdir(parents=True, exist_ok=True)
    path = _bundle_path(plan.item.work_id, target_dir)
    with _exclusive_bundle_lock(path):
        _atomic_write_json(path, bundle)
    return path


def load_dispatch_bundle(work_id: str, *, dispatch_dir: Path | None = None) -> dict[str, object]:
    """Read the dispatch bundle for a work_id."""
    path = _bundle_path(work_id, default_dispatch_dir() if dispatch_dir is None else dispatch_dir)
    return _load_dispatch_bundle_from_path(path, work_id)


def next_dispatch_step(work_id: str, *, dispatch_dir: Path | None = None) -> dict[str, object]:
    """Mark the next queued segment as running and return the payload for Hermes."""
    target_dir = default_dispatch_dir() if dispatch_dir is None else dispatch_dir
    path = _bundle_path(work_id, target_dir)
    now = time.time()
    with _exclusive_bundle_lock(path):
        bundle = _load_dispatch_bundle_from_path(path, work_id)
        steps = _steps(bundle)
        for step in steps:
            if step.get("status") in {"queued", "retry_wait"} or _stale_running(step, now):
                step["status"] = "running"
                step["updated_at"] = time.time()
                _atomic_write_json(path, bundle)
                return {
                    "work_id": work_id,
                    "ready": True,
                    "step": _public_step(step),
                    "remaining": _remaining_count(steps),
                    "synthesis_ready": False,
                }
        return {
            "work_id": work_id,
            "ready": False,
            "step": {},
            "remaining": _remaining_count(steps),
            "synthesis_ready": all(step.get("status") == "succeeded" for step in steps),
        }


def record_dispatch_result(
    work_id: str,
    step_id: str,
    *,
    result: str = "",
    error: str = "",
    succeeded: bool = True,
    retryable: bool = False,
    dispatch_dir: Path | None = None,
) -> dict[str, object]:
    """Store the segment result produced by the Hermes model.

    A failure recorded with ``retryable=True`` parks the step in 'retry_wait'
    so the next drain re-claims it, instead of the terminal 'failed'.
    """
    target_dir = default_dispatch_dir() if dispatch_dir is None else dispatch_dir
    path = _bundle_path(work_id, target_dir)
    with _exclusive_bundle_lock(path):
        bundle = _load_dispatch_bundle_from_path(path, work_id)
        steps = _steps(bundle)
        for step in steps:
            if step.get("step_id") == step_id:
                status = "succeeded" if succeeded else ("retry_wait" if retryable else "failed")
                if step.get("status") in {"succeeded", "failed"}:
                    stored_result = str(step.get("result", ""))
                    stored_error = str(step.get("error", ""))
                    stored_status = str(step.get("status", ""))
                    if stored_status == status and stored_result == result and stored_error == error:
                        return {
                            "ok": True,
                            "work_id": work_id,
                            "step_id": step_id,
                            "recorded": True,
                            "idempotent": True,
                            "status": stored_status,
                            "remaining": _remaining_count(steps),
                            "synthesis_ready": all(item.get("status") == "succeeded" for item in steps),
                        }
                    return {
                        "ok": False,
                        "error": "step_already_recorded",
                        "work_id": work_id,
                        "step_id": step_id,
                        "recorded": False,
                        "stored_status": stored_status,
                        "stored_result": stored_result,
                        "stored_error": stored_error,
                    }
                step["status"] = status
                step["result"] = result
                step["error"] = error
                step["updated_at"] = time.time()
                _atomic_write_json(path, bundle)
                return {
                    "ok": True,
                    "work_id": work_id,
                    "step_id": step_id,
                    "recorded": True,
                    "status": step["status"],
                    "remaining": _remaining_count(steps),
                    "synthesis_ready": all(item.get("status") == "succeeded" for item in steps),
                }
    raise DispatchStoreError(f"dispatch step not found: {work_id}/{step_id}")


def build_briefing_payload(work_id: str, *, dispatch_dir: Path | None = None) -> dict[str, object]:
    """Bundle all segment results into a final synthesis prompt."""
    bundle = load_dispatch_bundle(work_id, dispatch_dir=dispatch_dir)
    steps = _steps(bundle)
    missing: list[str] = []
    briefing_blocks: list[str] = []
    for step in steps:
        if step.get("status") != "succeeded":
            missing.append(str(step.get("step_id", "")))
        briefing_blocks.append(_briefing_step_block(step))
    if missing:
        return {"work_id": work_id, "ready": False, "missing_steps": missing, "briefing_prompt": ""}
    return {
        "work_id": work_id,
        "ready": True,
        "missing_steps": [],
        "briefing_prompt": _briefing_prompt(bundle, briefing_blocks),
        "result_count": len(steps),
    }


def _bundle_from_plan(plan: HarnessPlan) -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": time.time(),
        "work_id": plan.item.work_id,
        "surface": plan.item.surface.value,
        "original_prompt_preview": plan.preprocessing.normalized_prompt,
        "answer_policy": {
            "response_contract": plan.preprocessing.answer_policy.response_contract,
            "required_checks": list(plan.preprocessing.answer_policy.required_checks),
            "rules": list(plan.preprocessing.answer_policy.rules),
        },
        "steps": [_step_from_segment(segment) for segment in plan.preprocessing.segments],
    }


def _step_from_segment(segment: QueueSegment) -> dict[str, object]:
    return {
        "step_id": f"exec_{segment.segment_id}",
        "segment_id": segment.segment_id,
        "checksum": segment.checksum,
        "token_estimate": segment.token_estimate,
        "content": segment.content,
        "status": "queued",
        "result": "",
        "error": "",
        "updated_at": time.time(),
    }


def _public_step(step: dict[str, object]) -> dict[str, object]:
    content = str(step.get("content", ""))
    return {
        "step_id": str(step.get("step_id", "")),
        "segment_id": str(step.get("segment_id", "")),
        "checksum": str(step.get("checksum", "")),
        "token_estimate": int(step.get("token_estimate", 0)),
        "content": content,
        "instruction": (
            "Process this segment with the current Hermes model. Preserve the checksum, "
            "return only segment-grounded findings, and do not claim checks were run unless they were run."
        ),
    }


def _briefing_prompt(bundle: dict[str, object], briefing_blocks: list[str]) -> str:
    lines = [
        "[cluxion_final_briefing]",
        f"work_id={bundle.get('work_id', '')}",
        "Synthesize the ordered segment results into a concise user-facing briefing.",
        "Separate verified facts, tool results, inferences, missing checks, and remaining risks.",
        "[segment_results]",
    ]
    lines.extend(briefing_blocks)
    return "\n\n".join(lines)


def _briefing_step_block(step: dict[str, object]) -> str:
    return "\n".join(
        [
            f"step_id={step.get('step_id', '')}",
            f"segment_id={step.get('segment_id', '')}",
            f"checksum={step.get('checksum', '')}",
            str(step.get("result", "")),
        ]
    )


def _steps(bundle: dict[str, object]) -> list[dict[str, object]]:
    steps = bundle.get("steps")
    if not isinstance(steps, list):
        raise DispatchStoreError(f"dispatch bundle expected steps array, found {_type_name(steps)}")
    if not all(isinstance(step, dict) for step in steps):
        bad = next(step for step in steps if not isinstance(step, dict))
        raise DispatchStoreError(f"dispatch bundle expected step objects, found {_type_name(bad)}")
    return steps


def _remaining_count(steps: list[dict[str, object]]) -> int:
    return sum(1 for step in steps if step.get("status") in {"queued", "retry_wait", "running"})


def _stale_running(step: dict[str, object], now: float) -> bool:
    if step.get("status") != "running":
        return False
    updated = step.get("updated_at")
    updated_at = float(updated) if isinstance(updated, (int, float)) else 0.0
    return now - updated_at > RUNNING_LEASE_SECONDS


def _custom_dispatch_dir_configured() -> bool:
    return bool(os.environ.get(DISPATCH_DIR_ENV, "").strip() or os.environ.get(_LEGACY_DISPATCH_DIR_ENV, "").strip())


def _bundle_path(work_id: str, dispatch_dir: Path) -> Path:
    safe = "".join(ch for ch in work_id if ch.isalnum() or ch in {"-", "_"})
    if not safe:
        raise DispatchStoreError("work_id is empty")
    return dispatch_dir / f"{safe}.json"


def _load_dispatch_bundle_from_path(path: Path, work_id: str) -> dict[str, object]:
    if not path.exists():
        raise DispatchStoreError(f"dispatch bundle not found: {work_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DispatchStoreError(f"dispatch bundle is invalid JSON: {work_id}") from exc
    if not isinstance(payload, dict):
        raise DispatchStoreError(f"dispatch bundle expected object, found {_type_name(payload)}: {work_id}")
    return payload


def _type_name(value: object) -> str:
    if value is None:
        return "missing"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


@contextmanager
def _exclusive_bundle_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _fcntl is None:
        # Non-POSIX platforms keep atomic rename but skip advisory locking.
        yield
        return
    lock_path = path.parent / ".dispatch.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "r+b") as lock_file:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "DISPATCH_DIR_ENV",
    "RUNNING_LEASE_SECONDS",
    "DispatchStepRecord",
    "DispatchStoreError",
    "build_briefing_payload",
    "default_dispatch_dir",
    "load_dispatch_bundle",
    "next_dispatch_step",
    "persist_dispatch_bundle",
    "record_dispatch_result",
]
