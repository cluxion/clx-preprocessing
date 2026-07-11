"""Parity tests: the three queue backends must behave identically."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cluxion_runtime.core.dispatch_store import RUNNING_LEASE_SECONDS
from cluxion_runtime.resources import queue_bridge

_LOCAL_BIN = Path(__file__).resolve().parents[2] / "rust" / "cluxion_queue" / "target" / "release" / "cluxion-queue"

BACKENDS = ["python"]
if importlib.util.find_spec("cluxion_queue_native") is not None:
    BACKENDS.append("native")
if _LOCAL_BIN.exists() or shutil.which("cluxion-queue"):
    BACKENDS.append("subprocess")


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch, tmp_path):
    monkeypatch.setenv(queue_bridge.QUEUE_BACKEND_ENV, request.param)
    if request.param == "subprocess" and _LOCAL_BIN.exists():
        monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, str(_LOCAL_BIN))
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
