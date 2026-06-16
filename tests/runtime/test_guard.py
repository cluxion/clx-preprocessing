"""Resource guard: structural backend checks plus the fail-closed
ownership gate. CPU numbers are non-deterministic, so cross-backend
assertions are structural (keys, ranges), not exact equality."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from cluxion_runtime.resources import guard_bridge, queue_bridge

_LOCAL_BIN = Path(__file__).resolve().parents[2] / "rust" / "cluxion_queue" / "target" / "release" / "cluxion-queue"

BACKENDS = ["python"]
if importlib.util.find_spec("cluxion_queue_native") is not None:
    BACKENDS.append("native")
if _LOCAL_BIN.exists() or shutil.which("cluxion-queue"):
    BACKENDS.append("subprocess")


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch):
    monkeypatch.setenv(queue_bridge.QUEUE_BACKEND_ENV, request.param)
    if request.param == "subprocess" and _LOCAL_BIN.exists():
        monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, str(_LOCAL_BIN))
    return request.param


_SAMPLE_KEYS = {
    "ok",
    "total_ram_mb",
    "available_ram_mb",
    "swap_used_mb",
    "cpu_percent",
    "process_count",
    "zombie_count",
    "zombie_pids",
    "sampled_at_ms",
}


def test_sample_structure(backend: str) -> None:
    result = guard_bridge.sample({"cpu_sample_ms": 100})
    assert set(result) >= _SAMPLE_KEYS
    assert result["ok"] is True
    assert result["total_ram_mb"] > 0
    # sysinfo (Rust backends) can legitimately report available=0 under macOS
    # memory pressure (observed with ~20GB swap in use), so only bound the range.
    assert 0 <= result["available_ram_mb"] <= result["total_ram_mb"]
    assert result["process_count"] > 1
    assert result["zombie_count"] >= len([]) and len(result["zombie_pids"]) <= 50


def test_scan_owned_roots_gate(backend: str) -> None:
    me = os.getpid()
    owned = guard_bridge.scan([me], cpu_threshold=0.0, rss_threshold_mb=0)
    assert owned["ok"] is True
    assert owned["owned_alive"] >= 1
    assert owned["owned_roots"] == [me]


def test_scan_without_roots_owns_nothing(backend: str) -> None:
    result = guard_bridge.scan([], cpu_threshold=0.0, rss_threshold_mb=0)
    assert result["owned_alive"] == 0
    for entry in result["hot"] + result["zombies"]:
        assert entry["owned"] is False


def test_python_ownership_walk_is_fail_closed() -> None:
    parents = {10: 5, 5: 1}
    assert guard_bridge._is_owned(10, [5], parents)
    assert guard_bridge._is_owned(10, [1], parents)
    assert not guard_bridge._is_owned(10, [99], parents)
    # Unknown lineage -> external; no roots -> nothing owned.
    assert not guard_bridge._is_owned(42, [5], parents)
    assert not guard_bridge._is_owned(10, [], parents)


@pytest.mark.skipif(not _LOCAL_BIN.exists(), reason="release binary not built")
def test_daemon_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, str(_LOCAL_BIN))
    # str store_dir is a regression case: a live run passed str and crashed
    # on Path-only handling, so the lifecycle test exercises the str form.
    started = guard_bridge.start_daemon(store_dir=str(tmp_path), interval_ms=100, window=5)
    assert started["ok"] is True and started["started"] is True
    try:
        state = None
        for _ in range(30):
            time.sleep(0.1)
            state = guard_bridge.read_daemon_state(store_dir=tmp_path)
            if state is not None:
                break
        assert state is not None, "daemon never published state"
        assert state["stale"] is False
        assert state["current"]["total_ram_mb"] > 0
        assert state["window"]["samples"] >= 1
        assert guard_bridge.daemon_status(store_dir=tmp_path)["running"] is True
    finally:
        stopped = guard_bridge.stop_daemon(store_dir=tmp_path)
    assert stopped["ok"] is True and stopped["stopped"] is True
    assert guard_bridge.daemon_status(store_dir=tmp_path)["running"] is False


def test_stop_daemon_refuses_foreign_pid(tmp_path: Path) -> None:
    # Fail-closed kill gate: a pidfile pointing at a process that is not
    # our guard daemon (here: the test process itself) must not be signalled.
    (tmp_path / guard_bridge.PID_FILE_NAME).write_text(str(os.getpid()), encoding="utf-8")
    result = guard_bridge.stop_daemon(store_dir=tmp_path)
    assert result["stopped"] is False
    assert result["reason"] == "identity_mismatch"


def test_touch_heartbeat_updates_mtime(tmp_path: Path) -> None:
    heartbeat = tmp_path / guard_bridge.HEARTBEAT_FILE_NAME
    stale_mtime = time.time() - 3600
    heartbeat.touch()
    os.utime(heartbeat, (stale_mtime, stale_mtime))
    before = heartbeat.stat().st_mtime

    guard_bridge.touch_heartbeat(store_dir=tmp_path)

    assert heartbeat.stat().st_mtime > before


def test_start_daemon_touches_heartbeat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, "/nonexistent/cluxion-queue")
    monkeypatch.setattr(guard_bridge, "_which", lambda _binary: False)
    monkeypatch.setattr(guard_bridge, "_native_guard_available", lambda: False)
    monkeypatch.setattr(queue_bridge, "_native", None)
    heartbeat = tmp_path / guard_bridge.HEARTBEAT_FILE_NAME
    assert not heartbeat.exists()

    started = guard_bridge.start_daemon(store_dir=tmp_path, interval_ms=100, window=5)
    assert started["ok"] is True and started["started"] is True
    assert started["host"] == "python"
    try:
        assert heartbeat.exists()
        assert heartbeat.stat().st_mtime > 0
    finally:
        guard_bridge.stop_daemon(store_dir=tmp_path)


class _FakeProcess:
    def __init__(self, cmdline: list[str]) -> None:
        self._cmdline = cmdline

    def cmdline(self) -> list[str]:
        return self._cmdline


def test_is_our_daemon_accepts_python_host_cmdline(monkeypatch) -> None:
    python_host = [
        sys.executable,
        "-m",
        "cluxion_runtime.guard_daemon_host",
        "/tmp/store",
        "100",
        "5",
    ]
    monkeypatch.setattr(psutil, "Process", lambda _pid: _FakeProcess(python_host))
    assert guard_bridge._is_our_daemon(42) is True


def test_is_our_daemon_rejects_foreign_cmdline(monkeypatch) -> None:
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: _FakeProcess([sys.executable, "-c", "import time; time.sleep(60)"])
    )
    assert guard_bridge._is_our_daemon(42) is False


@pytest.mark.skipif(importlib.util.find_spec("cluxion_queue_native") is None, reason="native module not built")
def test_daemon_lifecycle_python_host(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, "/nonexistent/cluxion-queue")
    monkeypatch.setattr(guard_bridge, "_which", lambda _binary: False)
    started = guard_bridge.start_daemon(store_dir=tmp_path, interval_ms=100, window=5)
    assert started["ok"] is True and started["started"] is True
    assert started["host"] == "python"
    try:
        state = None
        for _ in range(30):
            time.sleep(0.1)
            state = guard_bridge.read_daemon_state(store_dir=tmp_path)
            if state is not None:
                break
        assert state is not None, "daemon never published state"
        assert state["stale"] is False
        assert guard_bridge.daemon_status(store_dir=tmp_path)["running"] is True
    finally:
        stopped = guard_bridge.stop_daemon(store_dir=tmp_path)
    assert stopped["ok"] is True and stopped["stopped"] is True


@pytest.fixture
def sleeper():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield child
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=5)


def test_enforce_requires_roots() -> None:
    result = guard_bridge.enforce([])
    assert result["ok"] is False
    assert result["error"] == "owned_roots_required"


def test_enforce_dry_run_reports_without_signalling(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    result = guard_bridge.enforce(
        [os.getpid()], cpu_threshold=0.0, rss_threshold_mb=0, dry_run=True, store_dir=tmp_path
    )
    assert result["ok"] is True and result["dry_run"] is True
    pids = {entry["pid"] for entry in result["candidates"]}
    assert sleeper.pid in pids  # owned descendant over (zero) threshold
    assert os.getpid() not in pids  # self is protected
    assert 1 not in pids  # external lineage never becomes a candidate
    assert result["terminated"] == [] and result["killed"] == []
    assert sleeper.poll() is None  # dry run: child untouched


def test_enforce_never_targets_root_itself(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    result = guard_bridge.enforce(
        [sleeper.pid], cpu_threshold=0.0, rss_threshold_mb=0, dry_run=True, store_dir=tmp_path
    )
    assert sleeper.pid not in {entry["pid"] for entry in result["candidates"]}
    assert any(entry["pid"] == sleeper.pid and entry["reason"] == "owned_root" for entry in result["skipped"])
    assert sleeper.poll() is None


def test_enforce_protect_list_is_honored(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    result = guard_bridge.enforce(
        [os.getpid()],
        cpu_threshold=0.0,
        rss_threshold_mb=0,
        dry_run=True,
        protect=[sleeper.pid],
        store_dir=tmp_path,
    )
    assert sleeper.pid not in {entry["pid"] for entry in result["candidates"]}
    assert any(entry["pid"] == sleeper.pid and entry["reason"] == "protected" for entry in result["skipped"])


def test_enforce_apply_terminates_owned_runaway(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    result = guard_bridge.enforce(
        [os.getpid()],
        cpu_threshold=0.0,
        rss_threshold_mb=0,
        dry_run=False,
        grace_seconds=3.0,
        store_dir=tmp_path,
    )
    assert result["ok"] is True and result["dry_run"] is False
    assert sleeper.pid in result["terminated"] + result["killed"]
    # psutil reaps the direct child during wait_procs, so Popen.wait() cannot
    # see the signal status (ECHILD -> 0); the process being gone is the assert.
    sleeper.wait(timeout=5)
    assert sleeper.poll() is not None


def _write_daemon_state(
    tmp_path: Path,
    *,
    samples: int = 25,
    cpu_avg: float = 10.0,
    min_available_ram_mb: float = 8192.0,
    age_ms: int = 0,
) -> None:
    import json

    state = {
        "ok": True,
        "current": {"total_ram_mb": 16384, "available_ram_mb": 8192, "cpu_percent": cpu_avg},
        "window": {
            "samples": samples,
            "cpu_avg": cpu_avg,
            "cpu_peak": cpu_avg,
            "min_available_ram_mb": min_available_ram_mb,
        },
        "interval_ms": 200,
        "updated_at_ms": int(time.time() * 1000) - age_ms,
    }
    (tmp_path / guard_bridge.STATE_FILE_NAME).write_text(json.dumps(state), encoding="utf-8")


def test_auto_enforce_requires_roots(tmp_path: Path) -> None:
    result = guard_bridge.auto_enforce([], store_dir=tmp_path)
    assert result["ok"] is False and result["triggered"] is False
    assert result["error"] == "owned_roots_required"


def test_auto_enforce_fail_closed_without_daemon_state(tmp_path: Path) -> None:
    result = guard_bridge.auto_enforce([os.getpid()], store_dir=tmp_path)
    assert result["ok"] is False and result["triggered"] is False
    assert result["error"] == "daemon_state_missing"


def test_auto_enforce_fail_closed_on_stale_state(tmp_path: Path) -> None:
    _write_daemon_state(tmp_path, cpu_avg=99.0, age_ms=guard_bridge.STALE_AFTER_MS + 1_000)
    result = guard_bridge.auto_enforce([os.getpid()], store_dir=tmp_path)
    assert result["ok"] is False and result["triggered"] is False
    assert result["error"] == "daemon_state_stale"


def test_auto_enforce_fail_closed_while_window_warms_up(tmp_path: Path) -> None:
    _write_daemon_state(tmp_path, samples=3, cpu_avg=99.0)
    result = guard_bridge.auto_enforce([os.getpid()], min_samples=25, store_dir=tmp_path)
    assert result["ok"] is False and result["triggered"] is False
    assert result["error"] == "window_warming_up"
    assert result["window"]["samples"] == 3


def test_auto_enforce_not_triggered_below_sustained_thresholds(tmp_path: Path) -> None:
    _write_daemon_state(tmp_path, cpu_avg=20.0, min_available_ram_mb=8192.0)
    result = guard_bridge.auto_enforce([os.getpid()], store_dir=tmp_path)
    assert result["ok"] is True and result["triggered"] is False
    assert "candidates" not in result  # below threshold: no process scan at all
    assert result["window"]["cpu_avg"] == 20.0


def test_auto_enforce_triggers_on_sustained_cpu_dry_run(tmp_path: Path, sleeper: subprocess.Popen) -> None:
    _write_daemon_state(tmp_path, cpu_avg=97.5, min_available_ram_mb=8192.0)
    result = guard_bridge.auto_enforce([os.getpid()], cpu_threshold=0.0, rss_threshold_mb=0, store_dir=tmp_path)
    assert result["ok"] is True and result["triggered"] is True
    assert result["dry_run"] is True
    assert any("cpu_avg" in reason for reason in result["trigger_reasons"])
    assert sleeper.pid in {entry["pid"] for entry in result["candidates"]}
    assert result["terminated"] == [] and result["killed"] == []
    assert sleeper.poll() is None  # dry run: child untouched


def test_auto_enforce_triggers_on_ram_floor(tmp_path: Path) -> None:
    _write_daemon_state(tmp_path, cpu_avg=5.0, min_available_ram_mb=512.0)
    result = guard_bridge.auto_enforce([os.getpid()], cpu_threshold=1e9, rss_threshold_mb=10**9, store_dir=tmp_path)
    assert result["triggered"] is True
    assert any("min_available_ram_mb" in reason for reason in result["trigger_reasons"])
    # Sustained pressure was real, but the per-process enforce thresholds are
    # set impossibly high here, so the dry run must report zero candidates.
    assert result["candidates"] == []


def test_snapshot_prefers_fresh_daemon_state(tmp_path: Path, monkeypatch) -> None:
    import json

    from cluxion_runtime.resources import rust_bridge

    monkeypatch.setenv(queue_bridge.QUEUE_STORE_ENV, str(tmp_path))
    state = {
        "ok": True,
        "current": {
            "total_ram_mb": 4096,
            "available_ram_mb": 2048,
            "swap_used_mb": 7,
            "cpu_percent": 12.5,
        },
        "updated_at_ms": int(time.time() * 1000),
    }
    (tmp_path / guard_bridge.STATE_FILE_NAME).write_text(json.dumps(state), encoding="utf-8")
    snapshot = rust_bridge.collect_resource_snapshot()
    assert snapshot.total_ram_mb == 4096
    assert snapshot.available_ram_mb == 2048
    assert snapshot.swap_used_mb == 7
    assert snapshot.cpu_percent == 12.5

    # Stale state must fall back to live psutil numbers.
    state["updated_at_ms"] = int(time.time() * 1000) - 60_000
    (tmp_path / guard_bridge.STATE_FILE_NAME).write_text(json.dumps(state), encoding="utf-8")
    fallback = rust_bridge.collect_resource_snapshot()
    assert fallback.total_ram_mb != 4096 or fallback.available_ram_mb != 2048


def test_guard_python_sample_with_cpu_sample_ms_0_is_nonblocking(monkeypatch):
    import cluxion_runtime.resources.guard_bridge as gb
    t0 = time.time()
    res = gb._python_sample({"cpu_sample_ms": 0})
    dt = time.time() - t0
    assert dt < 0.05, "should be fast non-blocking"
    assert "cpu_percent" in res
    assert res["ok"]
