"""Small file-based store for Hermes host-model segment dispatch."""

from __future__ import annotations

import errno
import json
import os
import stat
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

_DIR_MODE = 0o700
_FILE_MODE = 0o600

if TYPE_CHECKING:
    from cluxion_runtime.core.types import HarnessPlan, QueueSegment

DISPATCH_DIR_ENV = "CLUXION_PREPROCESS_DISPATCH_DIR"
_LEGACY_DISPATCH_DIR_ENV = "HERMES_CLUXION_DISPATCH_DIR"
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
    """Return the shared producer/consumer dispatch store path.

    Precedence:
    CLUXION_PREPROCESS_DISPATCH_DIR > HERMES_CLUXION_DISPATCH_DIR >
    ``${CLUXION_QUEUE_STORE_DIR}/dispatch`` > built-in default.
    """
    for env_name in (DISPATCH_DIR_ENV, _LEGACY_DISPATCH_DIR_ENV):
        value = os.environ.get(env_name, "").strip()
        if value:
            return Path(value).expanduser()
    from cluxion_runtime.resources.queue_bridge import default_store_dir

    return default_store_dir() / "dispatch"


def resolved_producer_dispatch_dir() -> Path:
    """Return the actual producer write path (not a consumer alias).

    Explicit/legacy ``CLUXION_PREPROCESS_DISPATCH_DIR`` /
    ``HERMES_CLUXION_DISPATCH_DIR`` overrides use the shared dispatch resolver;
    otherwise producers write to ``queue_bridge.default_store_dir()/dispatch``.
    """
    if _custom_dispatch_dir_configured():
        return default_dispatch_dir()
    from cluxion_runtime.resources.queue_bridge import default_store_dir

    return default_store_dir() / "dispatch"


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
        from cluxion_runtime.resources.queue_bridge import default_store_dir
        from cluxion_runtime.resources.queue_bridge import persist_dispatch_bundle as rust_persist

        producer_path = default_store_dir() / "dispatch" / f"{_validated_work_id(plan.item.work_id)}.json"
        try:
            result = rust_persist(plan.item.work_id, bundle, store_dir=default_store_dir())
        except RuntimeError:
            # Backend outage falls through to the same locked, fail-closed core
            # path. Corrupt existing bytes are re-read there and never overwritten.
            pass
        else:
            # Contract results (idempotent / conflict / corrupt) must not fall
            # through to a destructive Python overwrite — only true backend
            # outages without an existing file fallback.
            if result.get("error") in {"dispatch_owner_conflict", "invalid_dispatch_owner"}:
                raise DispatchStoreError(str(result["error"]))
            if result.get("error") == "dispatch_bundle_conflict":
                raise DispatchStoreError("dispatch_bundle_conflict")
            if result.get("ok"):
                path = str(result.get("path", ""))
                if path:
                    return Path(path)
            if producer_path.exists():
                raise DispatchStoreError(str(result.get("error") or "dispatch_persist_failed"))
    _ensure_dir_mode(target_dir, _DIR_MODE)
    path = _bundle_path(plan.item.work_id, target_dir)
    with _exclusive_bundle_lock(path):
        decision = _persist_under_lock(path, bundle)
        if decision == "owner_conflict":
            raise DispatchStoreError("dispatch_owner_conflict")
        if decision == "conflict":
            raise DispatchStoreError("dispatch_bundle_conflict")
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
        "schema_version": 2,
        "created_at": time.time(),
        "work_id": plan.item.work_id,
        "owner": _owner_from_item(plan.item),
        "surface": plan.item.surface.value,
        "original_prompt_preview": plan.preprocessing.normalized_prompt,
        "answer_policy": {
            "response_contract": plan.preprocessing.answer_policy.response_contract,
            "required_checks": list(plan.preprocessing.answer_policy.required_checks),
            "rules": list(plan.preprocessing.answer_policy.rules),
        },
        "steps": [_step_from_segment(segment) for segment in plan.preprocessing.segments],
    }


def _owner_from_item(item: object) -> dict[str, str]:
    metadata = getattr(item, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    has_owner = any(key in metadata for key in ("owner_cwd", "owner_session_id", "owner_scope"))
    if has_owner:
        owner = {
            "cwd": metadata.get("owner_cwd"),
            "session_id": metadata.get("owner_session_id"),
            "scope": metadata.get("owner_scope"),
        }
        return _validated_owner(owner)
    raw_cwd = metadata.get("cwd") or str(Path.cwd())
    if not isinstance(raw_cwd, str) or "\x00" in raw_cwd:
        raise DispatchStoreError("invalid_dispatch_owner")
    cwd = str(Path(raw_cwd).expanduser().resolve(strict=False))
    return {"cwd": cwd, "session_id": "", "scope": f"project:{cwd}"}


def _owner_from_bundle(bundle: dict[str, object]) -> dict[str, str] | None:
    """Return normalized owner, or None when the bundle is schema-v1 ownerless."""
    schema_version = _validated_schema_version(bundle)
    owner = bundle.get("owner")
    if owner is None:
        if schema_version == 1:
            return None
        raise DispatchStoreError("invalid_dispatch_owner")
    if schema_version != 2:
        raise DispatchStoreError("invalid_dispatch_owner")
    return _validated_owner(owner)


def _validated_schema_version(bundle: dict[str, object]) -> int:
    if "schema_version" not in bundle:
        return 1
    value = bundle["schema_version"]
    if type(value) is not int or value not in (1, 2):
        raise DispatchStoreError("invalid_dispatch_owner")
    return value


def _validated_owner(owner: object) -> dict[str, str]:
    if not isinstance(owner, dict) or set(owner) != {"cwd", "session_id", "scope"}:
        raise DispatchStoreError("invalid_dispatch_owner")
    cwd, session_id, scope = owner["cwd"], owner["session_id"], owner["scope"]
    if not isinstance(cwd, str) or not cwd or not isinstance(session_id, str) or not isinstance(scope, str) or not scope:
        raise DispatchStoreError("invalid_dispatch_owner")
    return {"cwd": cwd, "session_id": session_id, "scope": scope}


def _owners_equal(left: dict[str, str], right: dict[str, str]) -> bool:
    return left.get("cwd") == right.get("cwd") and left.get("session_id") == right.get("session_id") and left.get(
        "scope"
    ) == right.get("scope")


def _step_identity_sequence(bundle: dict[str, object]) -> list[tuple[str, str]]:
    """Ordered (step_id, checksum) identity used for stable work_id conflict checks.

    Validates that ``steps`` is an array of objects — malformed shape fails closed
    instead of collapsing to an empty sequence.
    """
    steps = _steps(bundle)
    return [(str(step.get("step_id", "")), str(step.get("checksum", ""))) for step in steps]


def _persist_under_lock(path: Path, bundle: dict[str, object]) -> str:
    """Write a new bundle, or decide idempotent/conflict against an existing one.

    Returns ``"written"``, ``"idempotent"``, ``"owner_conflict"``, or ``"conflict"``.
    Idempotent re-persist requires owner equality AND ordered ``(step_id, checksum)``
    identity; never copy/compare progress fields. Ownerless (schema-v1) existing
    bytes fail closed — never guess an owner. When the path already exists,
    read/JSON/shape failures propagate (fail-closed) and never fall through to write.
    Symlinks (including dangling) are rejected via ``is_symlink``/lstat — never
    replace, unlink, or follow them.
    """
    new_owner = _owner_from_bundle(bundle)
    # is_symlink uses lstat: detects dangling links that path.exists() would miss.
    if path.is_symlink():
        raise DispatchStoreError(f"expected regular file, found symlink: {path}")
    if path.exists():
        existing = _load_dispatch_bundle_from_path(path, str(bundle.get("work_id", "")))
        # Validate step shape first so corrupt existing bytes fail closed via raise,
        # never fall through to an owner/idempotent decision that mutates.
        existing_id = _step_identity_sequence(existing)
        new_id = _step_identity_sequence(bundle)
        existing_owner = _owner_from_bundle(existing)
        # Ownerless existing (or new without owner over existing): fail closed.
        if existing_owner is None or new_owner is None:
            return "owner_conflict"
        if not _owners_equal(existing_owner, new_owner):
            return "owner_conflict"
        if existing_id == new_id:
            return "idempotent"
        return "conflict"
    _atomic_write_json(path, bundle)
    return "written"


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


def _is_posix() -> bool:
    return os.name == "posix"


def _open_flags(*, create: bool, write: bool = True) -> int:
    flags = os.O_RDWR if write else os.O_RDONLY
    if create:
        flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _fchmod_regular(path: Path, mode: int, *, create: bool) -> None:
    flags = _open_flags(create=create, write=create)
    try:
        fd = os.open(path, flags, mode if create else 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    except OSError as exc:
        if not create and exc.errno == errno.ENOENT:
            return
        raise DispatchStoreError(f"failed to open {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DispatchStoreError(f"expected regular file, found non-regular: {path}")
        if create or (st.st_mode & 0o777) != mode:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _ensure_dir_mode(path: Path, mode: int = _DIR_MODE) -> None:
    if path.is_symlink():
        raise DispatchStoreError(f"expected directory, found symlink: {path}")
    # Existing non-directory must fail closed before mkdir (FileExistsError is not DispatchStoreError).
    try:
        pre = path.lstat()
    except FileNotFoundError:
        pre = None
    except NotADirectoryError:
        # Parent component is a non-directory; lstat cannot traverse it.
        raise DispatchStoreError(f"expected directory, found non-directory: {path}") from None
    if pre is not None and not stat.S_ISDIR(pre.st_mode):
        raise DispatchStoreError(f"expected directory, found non-directory: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except NotADirectoryError:
        # Parent component is a non-directory; mkdir cannot create under it.
        raise DispatchStoreError(f"expected directory, found non-directory: {path}") from None
    except FileExistsError:
        # Race: path appeared as a non-directory between pre-check and mkdir.
        try:
            raced = path.lstat()
        except FileNotFoundError:
            raise
        if not stat.S_ISDIR(raced.st_mode):
            raise DispatchStoreError(f"expected directory, found non-directory: {path}") from None
        raise
    if not _is_posix():
        return
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise DispatchStoreError(f"expected directory, found non-directory: {path}")
    if st.st_mode & 0o777 != mode:
        os.chmod(path, mode, follow_symlinks=False)


def _ensure_file_mode(path: Path, mode: int = _FILE_MODE, *, create: bool = False) -> None:
    if path.is_symlink():
        raise DispatchStoreError(f"expected regular file, found symlink: {path}")
    if not path.exists():
        if not create:
            return
        if not _is_posix():
            path.touch()
            return
        _fchmod_regular(path, mode, create=True)
        return
    if not _is_posix():
        return
    try:
        st = path.lstat()
    except FileNotFoundError:
        if create:
            raise
        return
    if not stat.S_ISREG(st.st_mode):
        raise DispatchStoreError(f"expected regular file, found non-regular: {path}")
    if st.st_mode & 0o777 == mode:
        return
    _fchmod_regular(path, mode, create=False)


def _validated_work_id(work_id: str) -> str:
    safe = "".join(ch for ch in work_id if ch.isalnum() or ch in {"-", "_"})
    if not safe:
        raise DispatchStoreError("work_id is empty")
    if safe != work_id:
        raise DispatchStoreError(f"invalid work_id: {work_id!r}")
    return safe


def _bundle_path(work_id: str, dispatch_dir: Path) -> Path:
    safe = _validated_work_id(work_id)
    _ensure_dir_mode(dispatch_dir, _DIR_MODE)
    return dispatch_dir / f"{safe}.json"


def _load_dispatch_bundle_from_path(path: Path, work_id: str) -> dict[str, object]:
    if path.is_symlink():
        raise DispatchStoreError(f"expected regular file, found symlink: {path}")
    if not path.exists():
        raise DispatchStoreError(f"dispatch bundle not found: {work_id}")
    _ensure_file_mode(path, _FILE_MODE, create=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise DispatchStoreError(f"dispatch bundle is invalid JSON: {work_id}") from None
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
    _ensure_dir_mode(path.parent, _DIR_MODE)
    if _fcntl is None:
        # Non-POSIX platforms keep atomic rename but skip advisory locking.
        yield
        return
    lock_path = path.parent / ".dispatch.lock"
    if lock_path.is_symlink():
        raise DispatchStoreError(f"expected regular file, found symlink: {lock_path}")
    if lock_path.exists() and not stat.S_ISREG(lock_path.lstat().st_mode):
        raise DispatchStoreError(f"expected regular file, found non-regular: {lock_path}")
    try:
        fd = os.open(lock_path, _open_flags(create=True), _FILE_MODE)
    except OSError as exc:
        raise DispatchStoreError(f"failed to open lock file {lock_path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise DispatchStoreError(f"expected regular file, found non-regular: {lock_path}")
        os.fchmod(fd, _FILE_MODE)
    except Exception:
        os.close(fd)
        raise
    with os.fdopen(fd, "r+b") as lock_file:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    _ensure_dir_mode(path.parent, _DIR_MODE)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        if temporary is not None:
            _ensure_file_mode(temporary, _FILE_MODE, create=False)
        os.replace(temporary, path)
        _ensure_file_mode(path, _FILE_MODE, create=False)
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
    "resolved_producer_dispatch_dir",
]
