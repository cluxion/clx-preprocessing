"""Parity tests: the three queue backends must behave identically."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

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


def test_missing_required_field_raises(backend) -> None:
    with pytest.raises(RuntimeError, match="work_id"):
        queue_bridge.enqueue_work({"prompt": "no id"})


def test_resolve_backend_honors_env(backend) -> None:
    assert queue_bridge.resolve_backend() == backend
