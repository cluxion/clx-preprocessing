"""Parity tests: the three queue backends must behave identically."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from cluxion_runtime.core.dispatch_store import RUNNING_LEASE_SECONDS
from cluxion_runtime.resources import queue_bridge

BACKENDS = ["python", "native", "subprocess"]


@pytest.fixture(scope="session")
def current_rust_artifacts(tmp_path_factory):
    """Build and isolate the current native wheel/CLI; never trust ambient installs."""
    project = Path(__file__).resolve().parents[2]
    native_project = project / "rust" / "cluxion_queue"
    build_root = tmp_path_factory.mktemp("current-rust-artifacts")
    wheel_dir = build_root / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
            str(native_project),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("cluxion_queue_native-*.whl"))
    assert len(wheels) == 1, wheels
    target = build_root / "site"
    with zipfile.ZipFile(wheels[0]) as archive:
        archive.extractall(target)

    patch = pytest.MonkeyPatch()
    patch.syspath_prepend(str(target))
    patch.delitem(sys.modules, "cluxion_queue_native.cluxion_queue_native", raising=False)
    patch.delitem(sys.modules, "cluxion_queue_native", raising=False)
    importlib.invalidate_caches()
    native = importlib.import_module("cluxion_queue_native")
    assert Path(native.__file__).is_relative_to(target)

    subprocess.run(
        [
            "cargo",
            "build",
            "--offline",
            "--locked",
            "--release",
            "--manifest-path",
            str(native_project / "Cargo.toml"),
            "--bin",
            "cluxion-queue",
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    binary = native_project / "target" / "release" / "cluxion-queue"
    assert binary.is_file()
    yield native, binary
    patch.undo()


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch, tmp_path, current_rust_artifacts):
    native, binary = current_rust_artifacts
    monkeypatch.setenv(queue_bridge.QUEUE_BACKEND_ENV, request.param)
    if request.param == "native":
        monkeypatch.setattr(queue_bridge, "_native", native)
    elif request.param == "subprocess":
        monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, str(binary))
    monkeypatch.setenv(queue_bridge.QUEUE_STORE_ENV, str(tmp_path / "queue"))
    return request.param


def test_enqueue_dequeue_roundtrip(backend) -> None:
    result = queue_bridge.enqueue_work({"work_id": "w1", "prompt": "hello", "surface": "hermes", "priority": 1})
    assert result["ok"] is True
    assert result["accepted"] is True

    item = queue_bridge.dequeue_work()
    assert item["ok"] is True
    assert item["ready"] is True
    assert item["item"]["work_id"] == "w1"
    assert item["item"]["priority"] == 1

    empty = queue_bridge.dequeue_work()
    assert empty["ready"] is False


def test_duplicate_work_id_preserves_admission_sequence(backend) -> None:
    """Re-enqueue of an existing work_id must return its stored sequence (not MAX+1).

    ON CONFLICT already keeps sequence/created_at; the response must match that
    admission identity so callers do not invent a tail position. Order stays a, b.
    """
    first = queue_bridge.enqueue_work({"work_id": "a", "prompt": "first-a"})
    assert first["ok"] is True
    assert first["sequence"] == 1

    second = queue_bridge.enqueue_work({"work_id": "b", "prompt": "first-b"})
    assert second["ok"] is True
    assert second["sequence"] == 2

    re_a = queue_bridge.enqueue_work({"work_id": "a", "prompt": "re-a"})
    assert re_a["ok"] is True
    assert re_a["accepted"] is True
    assert re_a["sequence"] == 1, "duplicate work_id must return original admission sequence"
    assert "stored" not in re_a
    assert "idempotent" not in re_a
    assert "requeued" not in re_a

    peek = queue_bridge.peek_order()
    assert [row["work_id"] for row in peek["order"]] == ["a", "b"]
    assert [row["sequence"] for row in peek["order"]] == [1, 2]

    assert queue_bridge.dequeue_work()["item"]["work_id"] == "a"
    assert queue_bridge.dequeue_work()["item"]["work_id"] == "b"


def test_priority_ordering(backend) -> None:
    for wid, prio in (("low", 9), ("high", 1), ("mid", 5)):
        queue_bridge.enqueue_work({"work_id": wid, "prompt": wid, "priority": prio})
    order = [queue_bridge.dequeue_work()["item"]["work_id"] for _ in range(3)]
    assert order == ["high", "mid", "low"]


def test_status_counts(backend) -> None:
    queue_bridge.enqueue_work({"work_id": "a", "prompt": "a"})
    queue_bridge.enqueue_work({"work_id": "b", "prompt": "b"})
    queue_bridge.dequeue_work()
    status = queue_bridge.queue_status()
    assert status["pending"] == 1
    assert status["running"] == 1


def test_dispatch_lifecycle(backend) -> None:
    bundle = {
        "work_id": "d1",
        "steps": [
            {
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 10,
                "content": "one",
                "status": "queued",
                "result": "",
                "error": "",
            },
            {
                "step_id": "s2",
                "segment_id": "g2",
                "checksum": "c2",
                "token_estimate": 12,
                "content": "two",
                "status": "queued",
                "result": "",
                "error": "",
            },
        ],
    }
    assert queue_bridge.persist_dispatch_bundle("d1", bundle)["stored"] is True

    first = queue_bridge.next_dispatch_step("d1")
    assert first["ready"] is True
    assert first["step"]["step_id"] == "s1"

    rec = queue_bridge.record_dispatch_step("d1", "s1", result="done-1")
    assert rec["recorded"] is True
    assert rec["synthesis_ready"] is False

    second = queue_bridge.next_dispatch_step("d1")
    assert second["step"]["step_id"] == "s2"
    queue_bridge.record_dispatch_step("d1", "s2", result="done-2")

    brief = queue_bridge.build_briefing("d1")
    assert brief["ready"] is True
    assert "done-1" in brief["briefing_prompt"]
    assert "done-2" in brief["briefing_prompt"]


def _two_step_bundle(work_id: str) -> dict:
    return {
        "work_id": work_id,
        "steps": [
            {
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 10,
                "content": "one",
                "status": "queued",
                "result": "",
                "error": "",
            },
            {
                "step_id": "s2",
                "segment_id": "g2",
                "checksum": "c2",
                "token_estimate": 12,
                "content": "two",
                "status": "queued",
                "result": "",
                "error": "",
            },
        ],
    }


def test_dispatch_stale_running_step_reclaimed(backend, tmp_path: Path) -> None:
    """All backends must re-claim a 'running' step whose lease expired (killed worker)."""
    queue_bridge.persist_dispatch_bundle("d-stale", _two_step_bundle("d-stale"))
    assert queue_bridge.next_dispatch_step("d-stale")["step"]["step_id"] == "s1"

    bundle_path = tmp_path / "queue" / "dispatch" / "d-stale.json"
    stored = json.loads(bundle_path.read_text(encoding="utf-8"))
    for step in stored["steps"]:
        if step["step_id"] == "s1":
            step["updated_at"] = time.time() - RUNNING_LEASE_SECONDS - 1
    bundle_path.write_text(json.dumps(stored), encoding="utf-8")

    reclaimed = queue_bridge.next_dispatch_step("d-stale")
    assert reclaimed["ready"] is True
    assert reclaimed["step"]["step_id"] == "s1"

    fresh = queue_bridge.next_dispatch_step("d-stale")
    assert fresh["step"]["step_id"] == "s2"  # freshly re-leased s1 must not be reclaimed


def test_dispatch_retryable_failure_requeues_step(backend) -> None:
    """failed+retryable parks the step in retry_wait so the next drain retries it."""
    queue_bridge.persist_dispatch_bundle("d-retry", _two_step_bundle("d-retry"))
    assert queue_bridge.next_dispatch_step("d-retry")["step"]["step_id"] == "s1"

    rec = queue_bridge.record_dispatch_step("d-retry", "s1", error="worker crashed", failed=True, retryable=True)
    assert rec["recorded"] is True
    assert rec["status"] == "retry_wait"
    assert rec["synthesis_ready"] is False

    retaken = queue_bridge.next_dispatch_step("d-retry")
    assert retaken["step"]["step_id"] == "s1"


def test_dispatch_terminal_record_replay_is_idempotent_and_conflict_safe(backend, tmp_path: Path) -> None:
    work_id = "d-terminal"
    queue_bridge.persist_dispatch_bundle(work_id, _two_step_bundle(work_id))
    first = queue_bridge.record_dispatch_step(work_id, "s1", result="FIRST")
    assert first["recorded"] is True

    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    first_bytes = bundle_path.read_bytes()

    replay = queue_bridge.record_dispatch_step(work_id, "s1", result="FIRST")
    assert replay["ok"] is True
    assert replay["recorded"] is True
    assert replay["idempotent"] is True
    assert bundle_path.read_bytes() == first_bytes

    conflict = queue_bridge.record_dispatch_step(work_id, "s1", result="SECOND")
    assert conflict == {
        "ok": False,
        "error": "step_already_recorded",
        "work_id": work_id,
        "step_id": "s1",
        "recorded": False,
        "stored_status": "succeeded",
        "stored_result": "FIRST",
        "stored_error": "",
    }
    assert bundle_path.read_bytes() == first_bytes


def test_dispatch_retry_wait_can_transition_to_terminal(backend) -> None:
    work_id = "d-retry-terminal"
    queue_bridge.persist_dispatch_bundle(work_id, _two_step_bundle(work_id))
    retry = queue_bridge.record_dispatch_step(
        work_id,
        "s1",
        error="temporary",
        failed=True,
        retryable=True,
    )
    assert retry["status"] == "retry_wait"

    completed = queue_bridge.record_dispatch_step(work_id, "s1", result="recovered")
    assert completed["ok"] is True
    assert completed["status"] == "succeeded"


def _owned_two_step(work_id: str, *, cwd: str, scope: str, session_id: str = "") -> dict:
    body = _two_step_bundle(work_id)
    body["schema_version"] = 2
    body["owner"] = {"cwd": cwd, "session_id": session_id, "scope": scope}
    return body


def test_identical_repersist_preserves_progress_bytes(backend, tmp_path: Path) -> None:
    work_id = "d-idem"
    bundle = _owned_two_step(work_id, cwd="/tmp/idem", scope="project:/tmp/idem")
    assert queue_bridge.persist_dispatch_bundle(work_id, bundle)["stored"] is True
    assert queue_bridge.next_dispatch_step(work_id)["step"]["step_id"] == "s1"
    rec = queue_bridge.record_dispatch_step(work_id, "s1", result="kept")
    assert rec["recorded"] is True

    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    before_bytes = bundle_path.read_bytes()
    before = json.loads(before_bytes.decode("utf-8"))

    again = queue_bridge.persist_dispatch_bundle(work_id, bundle)
    assert again["ok"] is True
    assert again.get("stored") is False or again.get("idempotent") is True
    assert bundle_path.read_bytes() == before_bytes
    after = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert after["steps"] == before["steps"]
    assert after["steps"][0]["status"] == "succeeded"
    assert after["steps"][0]["result"] == "kept"


def test_conflicting_sequence_repersist_does_not_mutate(backend, tmp_path: Path) -> None:
    work_id = "d-conflict"
    first = _owned_two_step(work_id, cwd="/tmp/c", scope="project:/tmp/c")
    assert queue_bridge.persist_dispatch_bundle(work_id, first)["stored"] is True
    assert queue_bridge.next_dispatch_step(work_id)["step"]["step_id"] == "s1"
    queue_bridge.record_dispatch_step(work_id, "s1", result="kept")

    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    before_bytes = bundle_path.read_bytes()
    before = json.loads(before_bytes.decode("utf-8"))

    conflict = _owned_two_step(work_id, cwd="/tmp/c", scope="project:/tmp/c")
    conflict["steps"][0]["checksum"] = "CHANGED"
    result = queue_bridge.persist_dispatch_bundle(work_id, conflict)
    assert result.get("stored") is False
    assert result.get("error") == "dispatch_bundle_conflict" or result.get("ok") is False
    assert "dispatch_bundle_conflict" in str(result.get("error", result))
    assert bundle_path.read_bytes() == before_bytes
    assert json.loads(bundle_path.read_text(encoding="utf-8")) == before


def test_owner_equal_repersist_is_idempotent(backend, tmp_path: Path) -> None:
    work_id = "d-owner-idem"
    bundle = _owned_two_step(work_id, cwd="/tmp/a", scope="project:/tmp/a")
    assert queue_bridge.persist_dispatch_bundle(work_id, bundle)["stored"] is True
    assert queue_bridge.next_dispatch_step(work_id)["step"]["step_id"] == "s1"
    queue_bridge.record_dispatch_step(work_id, "s1", result="kept")
    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    before = bundle_path.read_bytes()

    again = queue_bridge.persist_dispatch_bundle(work_id, bundle)
    assert again["ok"] is True
    assert again.get("stored") is False or again.get("idempotent") is True
    assert bundle_path.read_bytes() == before


def test_owner_mismatch_repersist_is_typed_conflict(backend, tmp_path: Path) -> None:
    work_id = "d-owner-conflict"
    first = _owned_two_step(work_id, cwd="/tmp/a", scope="project:/tmp/a")
    assert queue_bridge.persist_dispatch_bundle(work_id, first)["stored"] is True
    assert queue_bridge.next_dispatch_step(work_id)["step"]["step_id"] == "s1"
    queue_bridge.record_dispatch_step(work_id, "s1", result="kept")
    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    before = bundle_path.read_bytes()

    foreign = _owned_two_step(work_id, cwd="/tmp/b", scope="project:/tmp/b")
    result = queue_bridge.persist_dispatch_bundle(work_id, foreign)
    assert result.get("ok") is False
    assert result.get("error") == "dispatch_owner_conflict"
    assert result.get("stored") is False
    assert bundle_path.read_bytes() == before


def test_malformed_schema_v2_owner_is_rejected_before_first_write(backend, tmp_path: Path) -> None:
    work_id = "d-invalid-owner"
    bundle = _two_step_bundle(work_id)
    bundle["schema_version"] = 2
    bundle["owner"] = {"cwd": [], "session_id": "", "scope": "project:/tmp/a"}

    result = queue_bridge.persist_dispatch_bundle(work_id, bundle)

    assert result.get("ok") is False
    assert result.get("stored") is False
    assert result.get("error") == "invalid_dispatch_owner"
    assert not (tmp_path / "queue" / "dispatch" / f"{work_id}.json").exists()


@pytest.mark.parametrize("schema_version", ["1", None, True, 1.0, 3])
def test_malformed_schema_version_is_never_downgraded_to_legacy_v1(
    backend, tmp_path: Path, schema_version: object
) -> None:
    work_id = "d-invalid-schema"
    bundle = _two_step_bundle(work_id)
    bundle["schema_version"] = schema_version

    result = queue_bridge.persist_dispatch_bundle(work_id, bundle)

    assert result.get("ok") is False
    assert result.get("stored") is False
    assert result.get("error") == "invalid_dispatch_owner"
    assert not (tmp_path / "queue" / "dispatch" / f"{work_id}.json").exists()


def test_ownerless_v1_repersist_fails_closed(backend, tmp_path: Path) -> None:
    work_id = "d-v1-ownerless"
    v1 = _two_step_bundle(work_id)
    v1["schema_version"] = 1
    assert queue_bridge.persist_dispatch_bundle(work_id, v1)["stored"] is True
    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    before = bundle_path.read_bytes()

    owned = _owned_two_step(work_id, cwd="/tmp/a", scope="project:/tmp/a")
    result = queue_bridge.persist_dispatch_bundle(work_id, owned)
    assert result.get("ok") is False
    assert result.get("error") == "dispatch_owner_conflict"
    assert bundle_path.read_bytes() == before


def test_schema_v1_ownerless_still_drains(backend) -> None:
    work_id = "d-v1-drain"
    v1 = _two_step_bundle(work_id)
    v1["schema_version"] = 1
    assert queue_bridge.persist_dispatch_bundle(work_id, v1)["stored"] is True
    assert queue_bridge.next_dispatch_step(work_id)["step"]["step_id"] == "s1"
    queue_bridge.record_dispatch_step(work_id, "s1", result="one")
    assert queue_bridge.next_dispatch_step(work_id)["step"]["step_id"] == "s2"
    queue_bridge.record_dispatch_step(work_id, "s2", result="two")
    brief = queue_bridge.build_briefing(work_id)
    assert brief["ready"] is True


@pytest.mark.parametrize(
    "corrupt_body",
    [
        b"not-json{{{",
        b"[]",
        b'{"work_id":"d-corrupt","steps":"nope"}',
        b'{"work_id":"d-corrupt","steps":[1,2]}',
    ],
)
def test_corrupt_existing_bundle_is_fail_closed(backend, tmp_path: Path, corrupt_body: bytes) -> None:
    work_id = "d-corrupt"
    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(corrupt_body)
    before = bundle_path.read_bytes()

    with pytest.raises(RuntimeError):
        queue_bridge.persist_dispatch_bundle(work_id, _two_step_bundle(work_id))

    assert bundle_path.read_bytes() == before == corrupt_body


# --- Queue work-item owner isolation (metadata reserved keys, no schema migration) ---


def _owner_metadata(cwd: str, scope: str, session_id: str = "") -> dict:
    return {
        "owner_cwd": cwd,
        "owner_session_id": session_id,
        "owner_scope": scope,
    }


def _queue_db_row(tmp_path: Path, work_id: str) -> tuple:
    import sqlite3

    db = tmp_path / "queue" / "work_queue.sqlite"
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT prompt, surface, priority, status, metadata_json, sequence, created_at, updated_at "
            "FROM work_queue WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    finally:
        conn.close()


def test_queue_cross_owner_is_typed_conflict_and_preserves_row(backend, tmp_path: Path) -> None:
    work_id = "q-cross"
    first = queue_bridge.enqueue_work(
        {
            "work_id": work_id,
            "prompt": "owned-a",
            "metadata": _owner_metadata("/tmp/a", "project:/tmp/a"),
        }
    )
    assert first["ok"] is True
    assert first["accepted"] is True
    before = _queue_db_row(tmp_path, work_id)

    conflict = queue_bridge.enqueue_work(
        {
            "work_id": work_id,
            "prompt": "owned-b",
            "metadata": _owner_metadata("/tmp/b", "project:/tmp/b"),
        }
    )
    assert conflict == {
        "ok": False,
        "accepted": False,
        "error": "queue_owner_conflict",
        "work_id": work_id,
    }
    assert _queue_db_row(tmp_path, work_id) == before


def test_queue_exactly_one_side_ownerless_is_conflict(backend, tmp_path: Path) -> None:
    work_id = "q-one-ownerless"
    owned = queue_bridge.enqueue_work(
        {
            "work_id": work_id,
            "prompt": "owned",
            "metadata": _owner_metadata("/tmp/a", "project:/tmp/a"),
        }
    )
    assert owned["ok"] is True
    before = _queue_db_row(tmp_path, work_id)

    conflict = queue_bridge.enqueue_work({"work_id": work_id, "prompt": "ownerless"})
    assert conflict["ok"] is False
    assert conflict["accepted"] is False
    assert conflict["error"] == "queue_owner_conflict"
    assert conflict["work_id"] == work_id
    assert _queue_db_row(tmp_path, work_id) == before

    work_id_b = "q-one-ownerless-b"
    first = queue_bridge.enqueue_work({"work_id": work_id_b, "prompt": "ownerless-first"})
    assert first["ok"] is True
    before_b = _queue_db_row(tmp_path, work_id_b)
    conflict_b = queue_bridge.enqueue_work(
        {
            "work_id": work_id_b,
            "prompt": "owned-second",
            "metadata": _owner_metadata("/tmp/a", "project:/tmp/a"),
        }
    )
    assert conflict_b["error"] == "queue_owner_conflict"
    assert _queue_db_row(tmp_path, work_id_b) == before_b


def test_queue_same_owner_exact_and_changed_requeue(backend, tmp_path: Path) -> None:
    """Same-owner duplicates preserve legacy requeue (running->pending), original response shape."""
    work_id = "q-same-owner"
    owner = _owner_metadata("/tmp/a", "project:/tmp/a")
    payload = {
        "work_id": work_id,
        "prompt": "same",
        "surface": "hermes",
        "priority": 1,
        "metadata": owner,
    }
    first = queue_bridge.enqueue_work(payload)
    assert first["ok"] is True
    assert first["accepted"] is True
    assert "stored" not in first
    assert "idempotent" not in first
    assert "requeued" not in first
    assert queue_bridge.dequeue_work()["item"]["work_id"] == work_id
    before = _queue_db_row(tmp_path, work_id)
    assert before[3] == "running"

    again = queue_bridge.enqueue_work(payload)
    assert again["ok"] is True
    assert again["accepted"] is True
    assert again["sequence"] == first["sequence"]
    assert "stored" not in again
    assert "idempotent" not in again
    assert "requeued" not in again
    after_exact = _queue_db_row(tmp_path, work_id)
    assert after_exact[3] == "pending"
    assert after_exact[5] == before[5]
    assert after_exact[6] == before[6]

    changed = queue_bridge.enqueue_work(
        {"work_id": work_id, "prompt": "v2", "priority": 2, "metadata": owner}
    )
    assert changed["ok"] is True
    assert changed["accepted"] is True
    assert changed["sequence"] == 1
    assert "stored" not in changed
    after = _queue_db_row(tmp_path, work_id)
    assert after[0] == "v2"
    assert after[2] == 2
    assert after[3] == "pending"
    assert after[5] == before[5]
    assert after[6] == before[6]


def test_queue_both_ownerless_legacy_requeue(backend, tmp_path: Path) -> None:
    """Ownerless↔ownerless is not a conflict: exact and changed both requeue."""
    work_id = "q-ownerless"
    payload = {"work_id": work_id, "prompt": "same-ownerless", "priority": 2}
    first = queue_bridge.enqueue_work(payload)
    assert first["ok"] is True
    assert queue_bridge.dequeue_work()["ready"] is True
    before = _queue_db_row(tmp_path, work_id)
    assert before[3] == "running"

    again = queue_bridge.enqueue_work(payload)
    assert again["ok"] is True
    assert again["accepted"] is True
    assert again["sequence"] == first["sequence"]
    assert "stored" not in again
    assert "idempotent" not in again
    after_exact = _queue_db_row(tmp_path, work_id)
    assert after_exact[3] == "pending"
    assert after_exact[5] == before[5]

    changed = queue_bridge.enqueue_work({"work_id": work_id, "prompt": "v2"})
    assert changed["ok"] is True
    assert changed["accepted"] is True
    assert changed["sequence"] == first["sequence"]
    after = _queue_db_row(tmp_path, work_id)
    assert after[0] == "v2"
    assert after[3] == "pending"
    assert after[5] == before[5]
    assert after[6] == before[6]


@pytest.mark.parametrize(
    "metadata",
    [
        {"owner_cwd": "/tmp/a"},  # partial
        {"owner_cwd": "/tmp/a", "owner_session_id": ""},  # partial
        {"owner_cwd": "/tmp/a", "owner_scope": "project:/tmp/a"},  # missing session_id key
        {"owner_cwd": "", "owner_session_id": "", "owner_scope": "project:/tmp/a"},  # empty cwd
        {"owner_cwd": "/tmp/a", "owner_session_id": "", "owner_scope": ""},  # empty scope
        {"owner_cwd": 1, "owner_session_id": "", "owner_scope": "project:/tmp/a"},  # type
        {"owner_cwd": "/tmp/a", "owner_session_id": None, "owner_scope": "project:/tmp/a"},
    ],
)
def test_queue_partial_or_malformed_owner_is_invalid(backend, tmp_path: Path, metadata: dict) -> None:
    work_id = "q-invalid-owner"
    result = queue_bridge.enqueue_work({"work_id": work_id, "prompt": "x", "metadata": metadata})
    assert result.get("ok") is False
    assert result.get("accepted") is False
    assert result.get("error") == "invalid_queue_owner"
    assert result.get("work_id") == work_id
    db = tmp_path / "queue" / "work_queue.sqlite"
    if db.exists():
        assert _queue_db_row(tmp_path, work_id) is None


def test_queue_invalid_owner_on_existing_does_not_mutate(backend, tmp_path: Path) -> None:
    work_id = "q-invalid-existing"
    first = queue_bridge.enqueue_work(
        {
            "work_id": work_id,
            "prompt": "kept",
            "metadata": _owner_metadata("/tmp/a", "project:/tmp/a"),
        }
    )
    assert first["ok"] is True
    before = _queue_db_row(tmp_path, work_id)

    bad = queue_bridge.enqueue_work(
        {
            "work_id": work_id,
            "prompt": "mutate?",
            "metadata": {"owner_cwd": "/tmp/a", "owner_scope": "project:/tmp/a"},
        }
    )
    assert bad.get("error") == "invalid_queue_owner"
    assert bad.get("accepted") is False
    assert _queue_db_row(tmp_path, work_id) == before


@pytest.mark.parametrize(
    "seed_metadata_json",
    [
        "not-json{{{",
        "[1, 2, 3]",
        '{"owner_cwd":"/tmp/a"}',  # object with partial/invalid owner keys
    ],
)
def test_queue_existing_invalid_metadata_json_is_typed_invalid_unchanged(
    backend, tmp_path: Path, seed_metadata_json: str
) -> None:
    """Decode failure or non-object/invalid-object existing rows stay unchanged."""
    import sqlite3

    work_id = "q-corrupt-meta"
    seeded = queue_bridge.enqueue_work({"work_id": work_id, "prompt": "kept", "priority": 2})
    assert seeded["ok"] is True
    db = tmp_path / "queue" / "work_queue.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE work_queue SET metadata_json = ? WHERE work_id = ?",
            (seed_metadata_json, work_id),
        )
        conn.commit()
    finally:
        conn.close()
    before_row = _queue_db_row(tmp_path, work_id)
    before_bytes = db.read_bytes()
    assert before_row is not None
    assert before_row[4] == seed_metadata_json

    result = queue_bridge.enqueue_work(
        {
            "work_id": work_id,
            "prompt": "mutate?",
            "metadata": _owner_metadata("/tmp/a", "project:/tmp/a"),
        }
    )
    assert result == {
        "ok": False,
        "accepted": False,
        "error": "invalid_queue_owner",
        "work_id": work_id,
    }
    assert _queue_db_row(tmp_path, work_id) == before_row
    assert db.read_bytes() == before_bytes


def test_queue_metadata_none_is_ownerless_null_serialization(backend, tmp_path: Path) -> None:
    """Explicit metadata=None is valid ownerless; store JSON null (not {})."""
    work_id = "q-meta-null"
    result = queue_bridge.enqueue_work({"work_id": work_id, "prompt": "null-meta", "metadata": None})
    assert result["ok"] is True
    assert result["accepted"] is True

    row = _queue_db_row(tmp_path, work_id)
    assert row is not None
    assert row[4] == "null"
    assert json.loads(row[4]) is None

    item = queue_bridge.dequeue_work()
    assert item["ok"] is True
    assert item["ready"] is True
    assert item["item"]["work_id"] == work_id
    assert item["item"]["metadata"] is None


def test_native_reopens_after_stdlib_sqlite_reader_without_orphan_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current_rust_artifacts
) -> None:
    """A stdlib reader close must not detach later native writes into an orphan WAL."""
    import sqlite3

    native, _binary = current_rust_artifacts
    monkeypatch.setattr(queue_bridge, "_native", native)
    monkeypatch.setenv(queue_bridge.QUEUE_BACKEND_ENV, "native")
    monkeypatch.setenv(queue_bridge.QUEUE_STORE_ENV, str(tmp_path / "queue"))
    monkeypatch.setattr(
        queue_bridge.py_queue,
        "run",
        lambda *_args, **_kwargs: pytest.fail("forced native test fell back to Python"),
    )
    assert queue_bridge.resolve_backend() == "native"

    db = tmp_path / "queue" / "work_queue.sqlite"
    for index in range(32):
        work_id = f"cache-regression-{index}"
        assert queue_bridge.enqueue_work({"work_id": work_id, "prompt": "v1"})["ok"] is True
        assert queue_bridge.dequeue_work()["item"]["work_id"] == work_id

        reader = sqlite3.connect(db)
        try:
            assert reader.execute(
                "SELECT prompt, status FROM work_queue WHERE work_id = ?", (work_id,)
            ).fetchone() == ("v1", "running")
        finally:
            reader.close()

        requeued = queue_bridge.enqueue_work({"work_id": work_id, "prompt": "v2"})
        assert requeued["ok"] is True
        assert requeued["sequence"] == index + 1

        fresh = sqlite3.connect(db)
        try:
            assert fresh.execute(
                "SELECT prompt, status, sequence FROM work_queue WHERE work_id = ?", (work_id,)
            ).fetchone() == ("v2", "pending", index + 1)
        finally:
            fresh.close()
        assert queue_bridge.dequeue_work()["item"]["work_id"] == work_id


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX fail-closed symlink policy")
def test_persist_existing_dispatch_bundle_symlink_fail_closed(backend, tmp_path: Path) -> None:
    """Existing bundle path that is a symlink must fail closed; never replace it."""
    work_id = "d-sym"
    dispatch_dir = tmp_path / "queue" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "outside-victim.json"
    target_bytes = b'{"secret":"untouched-target-bytes"}'
    victim.write_bytes(target_bytes)
    bundle_path = dispatch_dir / f"{work_id}.json"
    bundle_path.symlink_to(victim)

    with pytest.raises(RuntimeError, match=r"symlink|expected"):
        queue_bridge.persist_dispatch_bundle(work_id, _two_step_bundle(work_id))

    assert bundle_path.is_symlink()
    assert victim.read_bytes() == target_bytes
    assert bundle_path.read_bytes() == target_bytes


def test_persist_invalid_utf8_bundle_is_fail_closed(backend, tmp_path: Path) -> None:
    """Invalid UTF-8 existing dispatch bytes must raise RuntimeError and stay byte-identical."""
    work_id = "d-utf8"
    bundle_path = tmp_path / "queue" / "dispatch" / f"{work_id}.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_body = b'{"work_id":"d-utf8","steps":[]}\xff'
    bundle_path.write_bytes(corrupt_body)
    before = bundle_path.read_bytes()

    with pytest.raises(RuntimeError):
        queue_bridge.persist_dispatch_bundle(work_id, _two_step_bundle(work_id))

    assert bundle_path.read_bytes() == before == corrupt_body


def test_missing_required_field_raises(backend) -> None:
    with pytest.raises(RuntimeError, match="work_id"):
        queue_bridge.enqueue_work({"prompt": "no id"})


def test_subprocess_timeout_falls_back_to_python(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(queue_bridge.QUEUE_BACKEND_ENV, raising=False)
    monkeypatch.setattr(queue_bridge, "resolve_backend", lambda: "subprocess")
    monkeypatch.setattr(queue_bridge.shutil, "which", lambda _binary: "/tmp/cluxion-queue")

    def timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["cluxion-queue", "enqueue"], timeout=kwargs["timeout"])

    monkeypatch.setattr(queue_bridge.subprocess, "run", timeout)

    result = queue_bridge.enqueue_work({"work_id": "timeout-fallback", "prompt": "hello"}, store_dir=tmp_path)

    assert result["ok"] is True
    assert result["accepted"] is True


def test_forced_subprocess_timeout_reports_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(queue_bridge.QUEUE_BACKEND_ENV, "subprocess")
    monkeypatch.setattr(queue_bridge.shutil, "which", lambda _binary: "/tmp/cluxion-queue")

    def timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["cluxion-queue", "enqueue"], timeout=kwargs["timeout"])

    monkeypatch.setattr(queue_bridge.subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="cluxion-queue enqueue timed out after 15s"):
        queue_bridge.enqueue_work({"work_id": "timeout-forced", "prompt": "hello"}, store_dir=tmp_path)


def test_resolve_backend_honors_env(backend) -> None:
    assert queue_bridge.resolve_backend() == backend


def test_work_id_alias_rejected_cross_backend(backend, tmp_path: Path) -> None:
    """vic!tim must never read/mutate a victim bundle on any backend."""
    if backend == "native" and not _native_has_strict_work_id():
        pytest.skip("installed native module predates strict work_id; covered by Rust unit tests")
    store = tmp_path / "queue"
    victim_bundle = _two_step_bundle("victim")
    queue_bridge.persist_dispatch_bundle("victim", victim_bundle, store_dir=store)
    with pytest.raises(RuntimeError, match=r"invalid|empty|work_id"):
        queue_bridge.next_dispatch_step("vic!tim", store_dir=store)
    with pytest.raises(RuntimeError, match=r"invalid|empty|work_id"):
        queue_bridge.record_dispatch_step("vic!tim", "s1", result="x", store_dir=store)
    with pytest.raises(RuntimeError, match=r"invalid|empty|work_id"):
        queue_bridge.build_briefing("vic!tim", store_dir=store)
    # victim remains untouched / still claimable
    claimed = queue_bridge.next_dispatch_step("victim", store_dir=store)
    assert claimed["ready"] is True
    assert claimed["step"]["step_id"] == "s1"


def test_work_id_unicode_valid_cross_backend(backend, tmp_path: Path) -> None:
    if backend == "native" and not _native_has_strict_work_id():
        pytest.skip("installed native module predates strict work_id; covered by Rust unit tests")
    store = tmp_path / "queue"
    work_id = "작업-테스트1"
    queue_bridge.persist_dispatch_bundle(work_id, _two_step_bundle(work_id), store_dir=store)
    claimed = queue_bridge.next_dispatch_step(work_id, store_dir=store)
    assert claimed["ready"] is True
    assert claimed["work_id"] == work_id
    rec = queue_bridge.record_dispatch_step(work_id, "s1", result="ok", store_dir=store)
    assert rec["recorded"] is True


def _native_has_strict_work_id() -> bool:
    """Detect whether the installed native extension rejects sanitized≠original ids."""
    import json
    import tempfile

    try:
        import cluxion_queue_native as native
    except ImportError:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        try:
            native.run(
                "next",
                json.dumps({"store_dir": str(store), "work_id": "vic!tim"}, ensure_ascii=False),
            )
        except RuntimeError as exc:
            return "invalid" in str(exc).lower()
        return False
