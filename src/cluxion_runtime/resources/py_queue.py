"""Pure-Python queue fallback, schema-compatible with the Rust engine.

Mirrors rust/cluxion_queue semantics exactly (same SQLite schema, same
dispatch JSON layout, same response shapes) so the bridge can swap
backends without callers noticing. Used only when neither the native
module nor the CLI binary is available.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    _fcntl = None

# Mirrors dispatch_store.RUNNING_LEASE_SECONDS and dispatch.rs RUNNING_LEASE_SECS.
_RUNNING_LEASE_SECONDS = 600.0

# Mirrors queue.rs ENQUEUE_RETRY_ATTEMPTS / OPEN_RETRY_ATTEMPTS / UNIQUE_SEQUENCE_INDEX.
_ENQUEUE_RETRY_ATTEMPTS = 5
_OPEN_RETRY_ATTEMPTS = 20
_UNIQUE_SEQUENCE_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_queue_sequence ON work_queue(sequence)"
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def run(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    store_dir = Path(payload.get("store_dir") or _default_store())
    handlers = {
        "enqueue": _enqueue,
        "dequeue": _dequeue,
        "peek": _peek,
        "persist": _persist,
        "next": _next_step,
        "record": _record_step,
        "brief": _brief,
        "status": _status,
    }
    handler = handlers.get(command)
    if handler is None:
        raise RuntimeError(f"unknown command: {command}")
    return handler(store_dir, payload)


def _default_store() -> str:
    home = os.environ.get("HOME", ".")
    return str(Path(home) / ".local" / "share" / "cluxion-agentplugin-preprocessing" / "queue")


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
    """Open with O_NOFOLLOW, verify regular file via fstat, fchmod the descriptor."""
    flags = _open_flags(create=create, write=create)
    try:
        fd = os.open(path, flags, mode if create else 0o600)
    except FileNotFoundError:
        if create:
            raise
        return  # optional leaf vanished (e.g. WAL checkpoint race)
    except OSError as exc:
        # ENOENT for optional leaves is not a permission failure.
        if not create and exc.errno == errno.ENOENT:
            return
        raise RuntimeError(f"failed to open {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"expected regular file, found non-regular: {path}")
        if create or (st.st_mode & 0o777) != mode:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _ensure_dir_mode(path: Path, mode: int = _DIR_MODE) -> None:
    """Ensure path is a real directory (not a symlink) and mode-tight on POSIX."""
    if path.is_symlink():
        raise RuntimeError(f"expected directory, found symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not _is_posix():
        return
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"expected directory, found non-directory: {path}")
    if st.st_mode & 0o777 != mode:
        os.chmod(path, mode, follow_symlinks=False)


def _ensure_file_mode(path: Path, mode: int = _FILE_MODE, *, create: bool = False) -> None:
    """Ensure a single leaf regular file; reject symlink/wrong type. Missing optional files OK."""
    if path.is_symlink():
        raise RuntimeError(f"expected regular file, found symlink: {path}")
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
        raise RuntimeError(f"expected regular file, found non-regular: {path}")
    if st.st_mode & 0o777 == mode:
        return
    # Mode needs tightening: O_RDONLY|O_NOFOLLOW + fstat + fchmod.
    _fchmod_regular(path, mode, create=False)


def _db_sidecars(db_path: Path) -> tuple[Path, Path]:
    return Path(f"{db_path}-wal"), Path(f"{db_path}-shm")


def _migrate_db_modes(db_path: Path, *, create_db: bool) -> None:
    _ensure_file_mode(db_path, _FILE_MODE, create=create_db)
    for side in _db_sidecars(db_path):
        _ensure_file_mode(side, _FILE_MODE, create=False)


def _open_db(store_dir: Path) -> sqlite3.Connection:
    _ensure_dir_mode(store_dir, _DIR_MODE)
    db_path = store_dir / "work_queue.sqlite"
    _migrate_db_modes(db_path, create_db=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    # The rollback->WAL switch upgrades a read txn to EXCLUSIVE inside SQLite
    # (sqlite3BtreeSetVersion) and that path returns SQLITE_BUSY without
    # consulting the busy handler, so the connect timeout cannot protect a
    # fresh-store multi-process burst: retry the (idempotent) schema script.
    for attempt in range(_OPEN_RETRY_ATTEMPTS + 1):
        if attempt:
            time.sleep(0.01 * attempt)
        try:
            _migrate_db_modes(db_path, create_db=False)
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                -- NORMAL+WAL can lose the latest commit on OS crash, but avoids corruption and keeps queue throughput balanced.
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS work_queue (
                    work_id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    surface TEXT NOT NULL DEFAULT 'api',
                    priority INTEGER NOT NULL DEFAULT 2,
                    status TEXT NOT NULL DEFAULT 'pending',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    sequence INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_queue_status_priority
                    ON work_queue(status, priority, sequence);
                """
            )
            break
        except sqlite3.OperationalError as err:
            if attempt >= _OPEN_RETRY_ATTEMPTS or ("locked" not in str(err) and "busy" not in str(err)):
                raise
    _migrate_db_modes(db_path, create_db=False)
    _ensure_unique_sequence_index(conn)
    return conn


def _ensure_unique_sequence_index(conn: sqlite3.Connection) -> None:
    """Sequence uniqueness backstop, mirroring queue.rs.

    Stores written by pre-0.3.41 builds can hold duplicate sequences:
    resequence them order-preserving and retry; if the index still cannot
    be built, skip it — enqueue stays correct via BEGIN IMMEDIATE.
    """
    try:
        with conn:
            conn.execute(_UNIQUE_SEQUENCE_INDEX)
        return
    except sqlite3.IntegrityError:
        pass
    except sqlite3.OperationalError:
        return  # transient (e.g. lock contention): the next open retries
    try:
        with conn:
            conn.execute(
                """UPDATE work_queue SET sequence = (
                       SELECT COUNT(*) FROM work_queue AS w2
                       WHERE w2.sequence < work_queue.sequence
                          OR (w2.sequence = work_queue.sequence AND w2.rowid <= work_queue.rowid))"""
            )
            conn.execute(_UNIQUE_SEQUENCE_INDEX)
    except sqlite3.Error:
        pass


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **data}


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"missing required field: {key}")
    return value


def _enqueue(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    prompt = _require_str(payload, "prompt")
    surface = payload.get("surface") or "api"
    priority = payload.get("priority", 2)
    metadata_json = json.dumps(payload.get("metadata", {}), ensure_ascii=False)
    now = time.time()
    # Retry absorbs transient "database is locked" (a burst must not lose
    # enqueues) and unique-sequence collisions (recompute MAX and try again),
    # mirroring queue.rs enqueue.
    last_error: Exception | None = None
    for attempt in range(_ENQUEUE_RETRY_ATTEMPTS):
        if attempt:
            time.sleep(0.01 * attempt)
        try:
            with _open_db(store_dir) as conn:
                _begin_immediate(conn)
                sequence = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM work_queue").fetchone()[0]
                conn.execute(
                    """INSERT INTO work_queue (work_id, prompt, surface, priority, status, metadata_json, sequence, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                       ON CONFLICT(work_id) DO UPDATE SET
                           prompt=excluded.prompt, surface=excluded.surface,
                           priority=excluded.priority, metadata_json=excluded.metadata_json,
                           status='pending', updated_at=excluded.updated_at""",
                    (work_id, prompt, surface, priority, metadata_json, sequence, now, now),
                )
                # ON CONFLICT preserves sequence/created_at; return that stored
                # admission identity rather than the provisional MAX+1 candidate.
                sequence = conn.execute(
                    "SELECT sequence FROM work_queue WHERE work_id = ?",
                    (work_id,),
                ).fetchone()[0]
            return _ok({"accepted": True, "work_id": work_id, "sequence": sequence, "reason": "queued"})
        except sqlite3.IntegrityError as err:
            last_error = err
        except sqlite3.OperationalError as err:
            if "locked" not in str(err) and "busy" not in str(err):
                raise
            last_error = err
    raise RuntimeError(f"enqueue failed after {_ENQUEUE_RETRY_ATTEMPTS} attempts: {last_error}")


def _dequeue(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    with _open_db(store_dir) as conn:
        _begin_immediate(conn)
        row = conn.execute(
            """SELECT work_id, prompt, surface, priority, metadata_json FROM work_queue
               WHERE status = 'pending' ORDER BY priority ASC, sequence ASC LIMIT 1"""
        ).fetchone()
        if row is None:
            return _ok({"ready": False, "item": None})
        conn.execute(
            "UPDATE work_queue SET status='running', updated_at=? WHERE work_id=?",
            (now, row[0]),
        )
    try:
        metadata = json.loads(row[4])
    except json.JSONDecodeError:
        metadata = {}
    return _ok(
        {
            "ready": True,
            "item": {
                "work_id": row[0],
                "prompt": row[1],
                "surface": row[2],
                "priority": row[3],
                "metadata": metadata,
            },
        }
    )


def _peek(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    limit = int(payload.get("limit", 16))
    with _open_db(store_dir) as conn:
        rows = conn.execute(
            """SELECT work_id, priority, status, sequence FROM work_queue
               ORDER BY priority ASC, sequence ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    order = [{"work_id": r[0], "priority": r[1], "status": r[2], "sequence": r[3]} for r in rows]
    return _ok({"order": order, "size": len(order)})


def _status(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    with _open_db(store_dir) as conn:
        pending = conn.execute("SELECT COUNT(*) FROM work_queue WHERE status='pending'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM work_queue WHERE status='running'").fetchone()[0]
    return _ok({"pending": pending, "running": running, "backend": "python_sqlite"})


def _dispatch_dir(store_dir: Path) -> Path:
    # Application queue leaf first, then dispatch child — never chmod parents above store_dir.
    _ensure_dir_mode(store_dir, _DIR_MODE)
    path = store_dir / "dispatch"
    _ensure_dir_mode(path, _DIR_MODE)
    return path


def _validated_work_id(work_id: str) -> str:
    safe = "".join(ch for ch in work_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise RuntimeError("work_id is empty")
    if safe != work_id:
        raise RuntimeError(f"invalid work_id: {work_id!r}")
    return safe


def _bundle_path(store_dir: Path, work_id: str) -> Path:
    safe = _validated_work_id(work_id)
    return _dispatch_dir(store_dir) / f"{safe}.json"


def _read_bundle(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"expected regular file, found symlink: {path}")
    if not path.exists():
        raise RuntimeError(f"dispatch bundle not found: {path}")
    _ensure_file_mode(path, _FILE_MODE, create=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(f"dispatch bundle is invalid JSON: {path}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"dispatch bundle expected object, found {_type_name(payload)}: {path}")
    return payload


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    _ensure_dir_mode(path.parent, _DIR_MODE)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
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


def _steps(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    steps = bundle.get("steps")
    if not isinstance(steps, list):
        raise RuntimeError(f"dispatch bundle expected steps array, found {_type_name(steps)}")
    if not all(isinstance(step, dict) for step in steps):
        bad = next(step for step in steps if not isinstance(step, dict))
        raise RuntimeError(f"dispatch bundle expected step objects, found {_type_name(bad)}")
    return steps


def _type_name(value: object) -> str:
    if value is None:
        return "missing"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _remaining(steps: list[dict[str, Any]]) -> int:
    return sum(1 for s in steps if s.get("status") in ("queued", "retry_wait", "running"))


def _step_identity_sequence(bundle: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered (step_id, checksum) identity; validates steps array/objects."""
    steps = _steps(bundle)
    return [(str(step.get("step_id", "")), str(step.get("checksum", ""))) for step in steps]


def _persist(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    if "bundle" not in payload:
        raise RuntimeError("missing bundle")
    bundle = payload["bundle"]
    if not isinstance(bundle, dict):
        raise RuntimeError("bundle must be an object")
    path = _bundle_path(store_dir, work_id)
    with _exclusive_bundle_lock(path):
        # is_symlink uses lstat: reject dangling links too; never replace/follow.
        if path.is_symlink():
            raise RuntimeError(f"expected regular file, found symlink: {path}")
        if path.exists():
            # Read/JSON/shape failures fail closed — never catch-and-overwrite.
            existing = _read_bundle(path)
            if _step_identity_sequence(existing) == _step_identity_sequence(bundle):
                return _ok({"stored": False, "idempotent": True, "path": str(path)})
            return {
                "ok": False,
                "stored": False,
                "error": "dispatch_bundle_conflict",
                "path": str(path),
            }
        _write_atomic(path, bundle)
    return _ok({"stored": True, "path": str(path)})


def _public_step(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": step.get("step_id", ""),
        "segment_id": step.get("segment_id", ""),
        "checksum": step.get("checksum", ""),
        "token_estimate": step.get("token_estimate", 0),
        "content": step.get("content", ""),
        "instruction": (
            "Process this segment with the current host model. Preserve checksum "
            "and do not claim checks were run unless they were run."
        ),
    }


def _stale_running(step: dict[str, Any], now: float) -> bool:
    if step.get("status") != "running":
        return False
    updated = step.get("updated_at")
    updated_at = float(updated) if isinstance(updated, (int, float)) else 0.0
    return now - updated_at > _RUNNING_LEASE_SECONDS


def _next_step(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    path = _bundle_path(store_dir, work_id)
    now = time.time()
    with _exclusive_bundle_lock(path):
        bundle = _read_bundle(path)
        steps = _steps(bundle)
        for step in steps:
            if step.get("status") in ("queued", "retry_wait") or _stale_running(step, now):
                step["status"] = "running"
                step["updated_at"] = time.time()
                _write_atomic(path, bundle)
                return _ok(
                    {
                        "work_id": work_id,
                        "ready": True,
                        "step": _public_step(step),
                        "remaining": _remaining(steps),
                        "synthesis_ready": False,
                    }
                )
        return _ok(
            {
                "work_id": work_id,
                "ready": False,
                "step": {},
                "remaining": _remaining(steps),
                "synthesis_ready": all(s.get("status") == "succeeded" for s in steps),
            }
        )


def _record_step(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    step_id = _require_str(payload, "step_id")
    failed = bool(payload.get("failed", False))
    retryable = bool(payload.get("retryable", False))
    status = ("retry_wait" if retryable else "failed") if failed else "succeeded"
    result = payload.get("result", "")
    error = payload.get("error", "")
    path = _bundle_path(store_dir, work_id)
    with _exclusive_bundle_lock(path):
        bundle = _read_bundle(path)
        steps = _steps(bundle)
        for step in steps:
            if step.get("step_id") == step_id:
                if step.get("status") in {"succeeded", "failed"}:
                    stored_status = str(step.get("status", ""))
                    stored_result = str(step.get("result", ""))
                    stored_error = str(step.get("error", ""))
                    if stored_status == status and stored_result == result and stored_error == error:
                        return _ok(
                            {
                                "work_id": work_id,
                                "step_id": step_id,
                                "recorded": True,
                                "idempotent": True,
                                "status": stored_status,
                                "remaining": _remaining(steps),
                                "synthesis_ready": all(s.get("status") == "succeeded" for s in steps),
                            }
                        )
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
                _write_atomic(path, bundle)
                return _ok(
                    {
                        "work_id": work_id,
                        "step_id": step_id,
                        "recorded": True,
                        "status": step["status"],
                        "remaining": _remaining(steps),
                        "synthesis_ready": all(s.get("status") == "succeeded" for s in steps),
                    }
                )
    raise RuntimeError(f"dispatch step not found: {work_id}/{step_id}")


@contextmanager
def _exclusive_bundle_lock(path: Path) -> Iterator[None]:
    _ensure_dir_mode(path.parent, _DIR_MODE)
    if _fcntl is None:
        # Non-POSIX platforms keep atomic rename but skip advisory locking.
        yield
        return
    lock_path = path.parent / ".dispatch.lock"
    if lock_path.is_symlink():
        raise RuntimeError(f"expected regular file, found symlink: {lock_path}")
    if lock_path.exists() and not stat.S_ISREG(lock_path.lstat().st_mode):
        raise RuntimeError(f"expected regular file, found non-regular: {lock_path}")
    try:
        fd = os.open(lock_path, _open_flags(create=True), _FILE_MODE)
    except OSError as exc:
        raise RuntimeError(f"failed to open lock file {lock_path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"expected regular file, found non-regular: {lock_path}")
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


def _brief(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    bundle = _read_bundle(_bundle_path(store_dir, work_id))
    steps = _steps(bundle)
    missing = [s.get("step_id", "") for s in steps if s.get("status") != "succeeded"]
    if missing:
        return _ok(
            {
                "work_id": work_id,
                "ready": False,
                "missing_steps": missing,
                "briefing_prompt": "",
            }
        )
    lines = [
        "[cluxion_final_briefing]",
        f"work_id={bundle.get('work_id', '')}",
        "Synthesize the ordered segment results into a concise user-facing briefing.",
        "Separate verified facts, tool results, inferences, missing checks, and remaining risks.",
        "[segment_results]",
    ]
    for step in steps:
        lines.append(
            f"step_id={step.get('step_id', '')}\nsegment_id={step.get('segment_id', '')}\n"
            f"checksum={step.get('checksum', '')}\n{step.get('result', '')}"
        )
    return _ok(
        {
            "work_id": work_id,
            "ready": True,
            "missing_steps": [],
            "briefing_prompt": "\n\n".join(lines),
            "result_count": len(steps),
        }
    )
