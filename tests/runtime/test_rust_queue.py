from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_rust_queue_enqueue_dequeue(tmp_path: Path) -> None:
    binary = Path(__file__).resolve().parents[2] / "rust" / "cluxion_queue" / "target" / "release" / "cluxion-queue"
    if not binary.exists():
        return
    store_dir = tmp_path / "queue"
    payload = {"work_id": "w1", "prompt": "hello", "surface": "hermes", "priority": 1, "store_dir": str(store_dir)}
    enqueue = subprocess.run(
        [str(binary), "enqueue"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert enqueue.returncode == 0, enqueue.stderr
    dequeue = subprocess.run(
        [str(binary), "dequeue"],
        input=json.dumps({"store_dir": str(store_dir)}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert dequeue.returncode == 0, dequeue.stderr
    body = json.loads(dequeue.stdout)
    assert body["ok"] is True
    assert body["ready"] is True
    assert body["item"]["work_id"] == "w1"


def test_queue_bridge_detects_local_binary(monkeypatch) -> None:
    from cluxion_runtime.resources import queue_bridge

    binary = Path(__file__).resolve().parents[2] / "rust" / "cluxion_queue" / "target" / "release" / "cluxion-queue"
    if not binary.exists():
        return
    monkeypatch.setenv("CLUXION_QUEUE_BIN", str(binary))
    assert queue_bridge.queue_available() is True
