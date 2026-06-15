"""Pure-Python queue fallback, schema-compatible with the Rust engine.

Mirrors rust/cluxion_queue semantics exactly (same SQLite schema, same
dispatch JSON layout, same response shapes) so the bridge can swap
backends without callers noticing. Used only when neither the native
module nor the CLI binary is available.
"""

from __future__ import annotations

import json
import os
import sqlite3
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


def _open_db(store_dir: Path) -> sqlite3.Connection:
    store_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store_dir / "work_queue.sqlite", timeout=30.0)
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
    return conn


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
    return _ok({"accepted": True, "work_id": work_id, "sequence": sequence, "reason": "queued"})


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
    return store_dir / "dispatch"


def _bundle_path(store_dir: Path, work_id: str) -> Path:
    safe = "".join(ch for ch in work_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise RuntimeError("work_id is empty")
    return _dispatch_dir(store_dir) / f"{safe}.json"


def _read_bundle(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"dispatch bundle not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _steps(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    steps = bundle.get("steps")
    if not isinstance(steps, list):
        raise RuntimeError("dispatch bundle has no steps array")
    return steps


def _remaining(steps: list[dict[str, Any]]) -> int:
    return sum(1 for s in steps if s.get("status") in ("queued", "retry_wait", "running"))


def _persist(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    if "bundle" not in payload:
        raise RuntimeError("missing bundle")
    path = _bundle_path(store_dir, work_id)
    with _exclusive_bundle_lock(path):
        _write_atomic(path, payload["bundle"])
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


def _next_step(store_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    work_id = _require_str(payload, "work_id")
    path = _bundle_path(store_dir, work_id)
    with _exclusive_bundle_lock(path):
        bundle = _read_bundle(path)
        steps = _steps(bundle)
        for step in steps:
            if step.get("status") in ("queued", "retry_wait"):
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
    path = _bundle_path(store_dir, work_id)
    with _exclusive_bundle_lock(path):
        bundle = _read_bundle(path)
        steps = _steps(bundle)
        for step in steps:
            if step.get("step_id") == step_id:
                step["status"] = "failed" if failed else "succeeded"
                step["result"] = payload.get("result", "")
                step["error"] = payload.get("error", "")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    if _fcntl is None:
        # Non-POSIX platforms keep atomic rename but skip advisory locking.
        yield
        return
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as lock_file:
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
