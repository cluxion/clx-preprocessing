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
