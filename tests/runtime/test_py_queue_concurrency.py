"""Concurrency coverage for the pure-Python queue fallback."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from cluxion_runtime.resources import py_queue

_RACE_TIMEOUT_SECONDS = 0.25


def _run_concurrently(worker_count: int, worker) -> list[object]:
    start = threading.Barrier(worker_count)

    def run(index: int) -> object:
        start.wait()
        return worker(index)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(run, range(worker_count)))


def _bundle(step_count: int) -> dict[str, Any]:
    return {
        "work_id": "py-race",
        "steps": [
            {
                "step_id": f"s{index}",
                "segment_id": f"g{index}",
                "checksum": f"c{index}",
                "token_estimate": 10,
                "content": f"segment {index}",
                "status": "queued",
                "result": "",
                "error": "",
            }
            for index in range(step_count)
        ],
    }


def _store_payload(store_dir: Path, **payload: Any) -> dict[str, Any]:
    return {"store_dir": str(store_dir), **payload}


def _dispatch_bundle_path(store_dir: Path, work_id: str) -> Path:
    return store_dir / "dispatch" / f"{work_id}.json"


def _read_dispatch_bundle(store_dir: Path, work_id: str) -> dict[str, Any]:
    return json.loads(_dispatch_bundle_path(store_dir, work_id).read_text(encoding="utf-8"))


def _stall_bundle_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    original_write = py_queue._write_atomic
    write_barrier = threading.Barrier(2)

    def write(path: Path, payload: dict[str, Any]) -> None:
        with suppress(threading.BrokenBarrierError):
            write_barrier.wait(timeout=_RACE_TIMEOUT_SECONDS)
        original_write(path, payload)

    monkeypatch.setattr(py_queue, "_write_atomic", write)


def test_python_queue_concurrent_next_steps_do_not_claim_same_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python fallback queue-next serializes JSON bundle updates."""
    store_dir = tmp_path / "queue"
    py_queue.run("persist", _store_payload(store_dir, work_id="py-race", bundle=_bundle(2)))
    _stall_bundle_writes(monkeypatch)

    results = _run_concurrently(2, lambda _index: py_queue.run("next", _store_payload(store_dir, work_id="py-race")))
    step_ids = [str(result["step"]["step_id"]) for result in results if result["ready"]]

    assert len(step_ids) == 2
    assert len(set(step_ids)) == 2
    statuses = [step["status"] for step in _read_dispatch_bundle(store_dir, "py-race")["steps"]]
    assert statuses.count("running") == 2


def test_python_queue_concurrent_record_steps_preserve_both_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python fallback queue-record serializes JSON bundle updates."""
    store_dir = tmp_path / "queue"
    py_queue.run("persist", _store_payload(store_dir, work_id="py-race", bundle=_bundle(2)))
    step_ids = [
        str(py_queue.run("next", _store_payload(store_dir, work_id="py-race"))["step"]["step_id"]),
        str(py_queue.run("next", _store_payload(store_dir, work_id="py-race"))["step"]["step_id"]),
    ]
    _stall_bundle_writes(monkeypatch)

    _run_concurrently(
        2,
        lambda index: py_queue.run(
            "record",
            _store_payload(
                store_dir,
                work_id="py-race",
                step_id=step_ids[index],
                result=f"done:{index}",
            ),
        ),
    )

    steps = {step["step_id"]: step for step in _read_dispatch_bundle(store_dir, "py-race")["steps"]}
    assert steps[step_ids[0]]["result"] == "done:0"
    assert steps[step_ids[1]]["result"] == "done:1"


def test_python_queue_uses_shared_lock_instead_of_per_bundle_locks(tmp_path: Path) -> None:
    store_dir = tmp_path / "queue"
    py_queue.run("persist", _store_payload(store_dir, work_id="py-race", bundle=_bundle(1)))
    payload = py_queue.run("next", _store_payload(store_dir, work_id="py-race"))
    py_queue.run("record", _store_payload(store_dir, work_id="py-race", step_id=payload["step"]["step_id"]))

    dispatch_dir = store_dir / "dispatch"
    assert not (dispatch_dir / "py-race.json.lock").exists()
    assert ((dispatch_dir / "py-race.json").stat().st_mode & 0o777) == 0o600
    if py_queue._fcntl is not None:
        lock_path = dispatch_dir / ".dispatch.lock"
        assert lock_path.exists()
        assert (lock_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode migration only")
def test_python_queue_migrates_store_and_dispatch_modes(tmp_path: Path) -> None:
    parent_mode = tmp_path.stat().st_mode
    store_dir = tmp_path / "queue"
    store_dir.mkdir(parents=True)
    store_dir.chmod(0o755)
    db = store_dir / "work_queue.sqlite"
    db.write_bytes(b"")
    db.chmod(0o644)
    dispatch = store_dir / "dispatch"
    dispatch.mkdir()
    dispatch.chmod(0o755)
    bundle = dispatch / "loose.json"
    bundle.write_text('{"work_id":"loose","steps":[]}', encoding="utf-8")
    bundle.chmod(0o644)

    py_queue.run("enqueue", _store_payload(store_dir, work_id="m1", prompt="p"))
    py_queue.run("next", _store_payload(store_dir, work_id="loose"))

    assert (store_dir.stat().st_mode & 0o777) == 0o700
    assert (db.stat().st_mode & 0o777) == 0o600
    assert (dispatch.stat().st_mode & 0o777) == 0o700
    assert (bundle.stat().st_mode & 0o777) == 0o600
    for side in (store_dir / "work_queue.sqlite-wal", store_dir / "work_queue.sqlite-shm"):
        if side.exists():
            assert (side.stat().st_mode & 0o777) == 0o600
    # no recursive parent tightening
    assert tmp_path.stat().st_mode == parent_mode


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode migration only")
def test_dispatch_only_first_access_sets_store_and_dispatch_0700(tmp_path: Path) -> None:
    """Dispatch-only path (no enqueue) must tighten application queue leaf + dispatch child."""
    parent_mode = tmp_path.stat().st_mode
    store_dir = tmp_path / "fresh_queue"
    # store_dir does not exist yet — first access is pure dispatch persist
    bundle = {
        "work_id": "d1",
        "steps": [
            {
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 1,
                "content": "x",
                "status": "queued",
                "result": "",
                "error": "",
            }
        ],
    }
    py_queue.run("persist", {**_store_payload(store_dir, work_id="d1"), "bundle": bundle})
    assert store_dir.is_dir()
    assert (store_dir.stat().st_mode & 0o777) == 0o700
    dispatch = store_dir / "dispatch"
    assert dispatch.is_dir()
    assert (dispatch.stat().st_mode & 0o777) == 0o700
    assert tmp_path.stat().st_mode == parent_mode


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode migration only")
def test_python_queue_ignores_missing_sidecars_and_rejects_symlinks(tmp_path: Path) -> None:
    store_dir = tmp_path / "queue"
    store_dir.mkdir()
    # missing WAL/SHM must not raise on first open
    py_queue.run("enqueue", _store_payload(store_dir, work_id="s1", prompt="p"))

    victim = tmp_path / "outside-target"
    victim.write_text("SECRET", encoding="utf-8")
    victim.chmod(0o644)
    victim_mode = victim.stat().st_mode
    victim_text = victim.read_text(encoding="utf-8")

    # planted bundle symlink: actual next must error and leave victim untouched
    dispatch = store_dir / "dispatch"
    dispatch.mkdir(exist_ok=True)
    link = dispatch / "alias.json"
    link.symlink_to(victim)
    with pytest.raises(RuntimeError, match=r"symlink|expected"):
        py_queue.run("next", _store_payload(store_dir, work_id="alias"))
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == victim_text
    assert victim.stat().st_mode == victim_mode

    # planted lock symlink: persist must error and leave lock-target untouched
    lock_victim = tmp_path / "lock-victim"
    lock_victim.write_text("LOCK", encoding="utf-8")
    lock_victim.chmod(0o644)
    lock_mode = lock_victim.stat().st_mode
    lock_text = lock_victim.read_text(encoding="utf-8")
    lock_link = dispatch / ".dispatch.lock"
    if lock_link.exists() or lock_link.is_symlink():
        lock_link.unlink()
    lock_link.symlink_to(lock_victim)
    with pytest.raises(RuntimeError, match=r"symlink|expected"):
        py_queue.run(
            "persist",
            {
                **_store_payload(store_dir, work_id="safe"),
                "bundle": {
                    "work_id": "safe",
                    "steps": [
                        {
                            "step_id": "s1",
                            "segment_id": "g1",
                            "checksum": "c1",
                            "token_estimate": 1,
                            "content": "x",
                            "status": "queued",
                            "result": "",
                            "error": "",
                        }
                    ],
                },
            },
        )
    assert lock_link.is_symlink()
    assert lock_victim.read_text(encoding="utf-8") == lock_text
    assert lock_victim.stat().st_mode == lock_mode


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode migration only")
def test_python_queue_rejects_db_symlink_without_touching_victim(tmp_path: Path) -> None:
    store_dir = tmp_path / "queue"
    store_dir.mkdir()
    victim = tmp_path / "real.sqlite"
    victim.write_bytes(b"VICTIM-DB")
    victim.chmod(0o644)
    mode = victim.stat().st_mode
    data = victim.read_bytes()
    (store_dir / "work_queue.sqlite").symlink_to(victim)
    with pytest.raises(RuntimeError, match=r"symlink|expected"):
        py_queue.run("enqueue", _store_payload(store_dir, work_id="x", prompt="p"))
    assert victim.read_bytes() == data
    assert victim.stat().st_mode == mode


def test_python_queue_concurrent_dequeue_serializes_select_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python fallback dequeue uses a transaction so two workers cannot claim one row."""
    store_dir = tmp_path / "queue"
    for index in range(2):
        py_queue.run(
            "enqueue",
            _store_payload(store_dir, work_id=f"w{index}", prompt=f"prompt {index}", priority=index),
        )

    original_open_db = py_queue._open_db
    select_barrier = threading.Barrier(2)

    class SlowConnection:
        def __init__(self, conn) -> None:
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb) -> bool | None:
            return self._conn.__exit__(exc_type, exc, tb)

        def execute(self, sql: str, parameters: tuple[object, ...] = ()):
            if "SELECT work_id, prompt, surface, priority, metadata_json FROM work_queue" in sql:
                with suppress(threading.BrokenBarrierError):
                    select_barrier.wait(timeout=_RACE_TIMEOUT_SECONDS)
            return self._conn.execute(sql, parameters)

    def open_db(path: Path) -> SlowConnection:
        return SlowConnection(original_open_db(path))

    monkeypatch.setattr(py_queue, "_open_db", open_db)

    results = _run_concurrently(2, lambda _index: py_queue.run("dequeue", _store_payload(store_dir)))
    work_ids = [str(result["item"]["work_id"]) for result in results if result["ready"]]

    assert len(work_ids) == 2
    assert len(set(work_ids)) == 2


_CROSS_PROCESS_WORKER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from cluxion_runtime.resources import py_queue
store, prefix, count = sys.argv[2], sys.argv[3], int(sys.argv[4])
results = []
for index in range(count):
    results.append(py_queue.run("enqueue", {"store_dir": store, "work_id": f"{prefix}-{index}", "prompt": "x"}))
print(json.dumps(results))
"""


def test_python_queue_cross_process_enqueue_is_lossless_and_duplicate_free(tmp_path: Path) -> None:
    """Real processes (own connections, fresh store) — threads cannot cover this race."""
    processes, per_process = 12, 4
    store_dir = tmp_path / "queue"
    src_dir = str(Path(py_queue.__file__).resolve().parents[3])

    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CROSS_PROCESS_WORKER, src_dir, str(store_dir), f"p{index}", str(per_process)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for index in range(processes)
    ]
    accepted = 0
    for child in children:
        stdout, _ = child.communicate(timeout=120)
        assert child.returncode == 0, stdout
        accepted += sum(1 for result in json.loads(stdout) if result.get("accepted"))

    total = processes * per_process
    rows, distinct, min_seq, max_seq = (
        sqlite3.connect(store_dir / "work_queue.sqlite")
        .execute("SELECT COUNT(*), COUNT(DISTINCT sequence), MIN(sequence), MAX(sequence) FROM work_queue")
        .fetchone()
    )
    assert accepted == total
    assert rows == total
    assert distinct == total
    assert (min_seq, max_seq) == (1, total)
