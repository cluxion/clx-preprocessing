"""Pure-Python guard daemon host: dual-cadence tick tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from cluxion_runtime.guard_daemon_host import (
    DEFAULT_IDLE_TTL_MS,
    HEARTBEAT_FILE_NAME,
    PID_FILE_NAME,
    PROC_SCAN_EVERY_N_TICKS,
    STATE_WRITE_EVERY_N_TICKS,
    ProcessScanCache,
    _daemon_loop_step,
    _python_daemon_tick,
    _run_python_daemon,
    _write_state_if_changed,
    is_idle,
)


def _process_table_available() -> bool:
    try:
        next(psutil.process_iter(["pid"]))
        return True
    except (StopIteration, PermissionError, psutil.Error):
        return False


pytestmark = pytest.mark.skipif(not _process_table_available(), reason="process table unavailable in sandbox")

_TOP_LEVEL_KEYS = ("ok", "current", "window", "interval_ms", "updated_at_ms")
_CURRENT_KEYS = (
    "ok",
    "total_ram_mb",
    "available_ram_mb",
    "swap_used_mb",
    "cpu_percent",
    "process_count",
    "zombie_count",
    "zombie_pids",
    "sampled_at_ms",
)
_WINDOW_KEYS = ("samples", "cpu_avg", "cpu_peak", "min_available_ram_mb")


def _assert_state_schema(state: dict[str, Any], *, interval_ms: int) -> None:
    for key in _TOP_LEVEL_KEYS:
        assert key in state, f"missing top-level key: {key}"
    assert state["ok"] is True
    assert isinstance(state["interval_ms"], int)
    assert state["interval_ms"] == interval_ms
    assert isinstance(state["updated_at_ms"], int)

    current = state["current"]
    for key in _CURRENT_KEYS:
        assert key in current, f"missing current key: {key}"
    assert current["ok"] is True
    assert isinstance(current["total_ram_mb"], int) and current["total_ram_mb"] > 0
    assert isinstance(current["available_ram_mb"], int)
    assert isinstance(current["swap_used_mb"], int)
    assert isinstance(current["cpu_percent"], (int, float))
    assert isinstance(current["process_count"], int)
    assert isinstance(current["zombie_count"], int)
    assert isinstance(current["zombie_pids"], list)
    assert all(isinstance(pid, int) for pid in current["zombie_pids"])
    assert isinstance(current["sampled_at_ms"], int)

    window = state["window"]
    for key in _WINDOW_KEYS:
        assert key in window, f"missing window key: {key}"
    assert isinstance(window["samples"], int)
    assert isinstance(window["cpu_avg"], (int, float))
    assert isinstance(window["cpu_peak"], (int, float))
    assert isinstance(window["min_available_ram_mb"], int)


def test_python_daemon_state_json_key_order_matches_rust() -> None:
    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    cpu_window: list[float] = []
    ram_window: list[int] = []
    state, _ = _python_daemon_tick(
        process_cache, cpu_window, ram_window, 10, 1000, tick=0
    )
    payload = json.dumps(state, separators=(",", ":"))
    assert payload.startswith(
        '{"ok":true,"current":{"ok":true,"total_ram_mb":'
    )
    assert ',"window":{"samples":' in payload
    assert ',"interval_ms":1000,"updated_at_ms":' in payload


def test_python_daemon_tick_scan_tick_schema_and_window() -> None:
    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    cpu_window: list[float] = []
    ram_window: list[int] = []
    interval_ms = 1000
    window = 10

    state, cache = _python_daemon_tick(
        process_cache, cpu_window, ram_window, window, interval_ms, tick=0
    )
    _assert_state_schema(state, interval_ms=interval_ms)
    assert state["current"]["process_count"] > 0
    assert state["window"]["samples"] == 1
    assert cache.process_count == state["current"]["process_count"]


def test_python_daemon_tick_cheap_tick_reuses_process_cache() -> None:
    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    cpu_window: list[float] = []
    ram_window: list[int] = []
    interval_ms = 1000
    window = 10

    _python_daemon_tick(process_cache, cpu_window, ram_window, window, interval_ms, tick=0)

    from cluxion_runtime.guard_daemon_host import _epoch_ms

    stale_cache = ProcessScanCache(
        process_count=1, zombie_count=2, zombie_pids=[99, 100], scanned_at_ms=_epoch_ms()
    )
    cheap_state, returned_cache = _python_daemon_tick(
        stale_cache, cpu_window, ram_window, window, interval_ms, tick=1
    )
    _assert_state_schema(cheap_state, interval_ms=interval_ms)
    assert cheap_state["current"]["process_count"] == 1
    assert cheap_state["current"]["zombie_count"] == 2
    assert cheap_state["current"]["zombie_pids"] == [99, 100]
    assert returned_cache == stale_cache

    from cluxion_runtime.guard_daemon_host import PROC_SCAN_MIN_INTERVAL_MS

    aged_cache = ProcessScanCache(
        process_count=1,
        zombie_count=2,
        zombie_pids=[99, 100],
        scanned_at_ms=_epoch_ms() - PROC_SCAN_MIN_INTERVAL_MS - 1,
    )
    rescan_state, refreshed_cache = _python_daemon_tick(
        aged_cache, cpu_window, ram_window, window, interval_ms, tick=PROC_SCAN_EVERY_N_TICKS
    )
    _assert_state_schema(rescan_state, interval_ms=interval_ms)
    assert refreshed_cache != aged_cache
    assert refreshed_cache.process_count > 0
    assert rescan_state["current"]["process_count"] == refreshed_cache.process_count


def test_python_daemon_tick_window_grows_one_per_tick() -> None:
    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    cpu_window: list[float] = []
    ram_window: list[int] = []
    interval_ms = 1000
    window = 10

    state0, process_cache = _python_daemon_tick(
        process_cache, cpu_window, ram_window, window, interval_ms, tick=0
    )
    assert state0["window"]["samples"] == 1

    state1, process_cache = _python_daemon_tick(
        process_cache, cpu_window, ram_window, window, interval_ms, tick=1
    )
    assert state1["window"]["samples"] == 2
    assert len(cpu_window) == 2
    assert len(ram_window) == 2


@pytest.mark.parametrize("tick", [0, 1, 4, 5])
def test_python_daemon_tick_scan_cadence(tick: int) -> None:
    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    cpu_window: list[float] = []
    ram_window: list[int] = []

    from cluxion_runtime.guard_daemon_host import PROC_SCAN_MIN_INTERVAL_MS, _epoch_ms

    # Scan throttling is wall-clock based: a fresh cache always scans, a
    # recently scanned cache is reused, a stale one rescans.
    _, cache = _python_daemon_tick(process_cache, cpu_window, ram_window, 5, 1000, tick=tick)
    assert cache.process_count > 0, "fresh cache must trigger a scan regardless of tick"

    recent = cache
    _, reused = _python_daemon_tick(recent, cpu_window, ram_window, 5, 1000, tick=tick + 1)
    assert reused == recent, "recent scan must be reused"

    stale = ProcessScanCache(
        process_count=recent.process_count,
        zombie_count=recent.zombie_count,
        zombie_pids=list(recent.zombie_pids),
        scanned_at_ms=_epoch_ms() - PROC_SCAN_MIN_INTERVAL_MS - 1,
    )
    _, rescanned = _python_daemon_tick(stale, cpu_window, ram_window, 5, 1000, tick=tick + 2)
    assert rescanned.scanned_at_ms > stale.scanned_at_ms, "stale cache must rescan"


def test_is_idle_detects_stale_and_fresh_heartbeats() -> None:
    ttl = 600_000
    now = 1_700_000_000_000
    assert is_idle(now - ttl - 1, now, ttl)
    assert not is_idle(now - ttl, now, ttl)
    assert not is_idle(now - 1, now, ttl)


def test_daemon_loop_step_exits_idle_and_removes_pidfile(tmp_path: Path) -> None:
    heartbeat = tmp_path / HEARTBEAT_FILE_NAME
    heartbeat.touch()
    stale_mtime = time.time() - 700
    os.utime(heartbeat, (stale_mtime, stale_mtime))
    pid_path = tmp_path / PID_FILE_NAME
    pid_path.write_text("4242", encoding="utf-8")

    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    keep_running, _, _ = _daemon_loop_step(
        tmp_path,
        process_cache=process_cache,
        cpu_window=[],
        ram_window=[],
        window=5,
        interval_ms=1000,
        tick=0,
        idle_ttl_ms=600_000,
        now_ms=int(time.time() * 1000),
    )

    assert keep_running is False
    assert not pid_path.exists()


def test_run_python_daemon_idle_exit_removes_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / HEARTBEAT_FILE_NAME
    heartbeat.touch()
    stale_mtime = time.time() - 700
    os.utime(heartbeat, (stale_mtime, stale_mtime))
    pid_path = tmp_path / PID_FILE_NAME
    pid_path.write_text("4242", encoding="utf-8")

    monkeypatch.setattr("cluxion_runtime.guard_daemon_host.time.sleep", lambda _: None)

    _run_python_daemon(str(tmp_path), 1000, 5)

    assert not pid_path.exists()


def test_daemon_loop_step_keeps_running_with_fresh_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / HEARTBEAT_FILE_NAME
    heartbeat.touch()
    pid_path = tmp_path / PID_FILE_NAME
    pid_path.write_text("4242", encoding="utf-8")

    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    keep_running, refreshed_cache, _ = _daemon_loop_step(
        tmp_path,
        process_cache=process_cache,
        cpu_window=[],
        ram_window=[],
        window=5,
        interval_ms=1000,
        tick=0,
        idle_ttl_ms=600_000,
        now_ms=int(time.time() * 1000),
    )

    assert keep_running is True
    assert pid_path.exists()
    assert refreshed_cache.process_count > 0
    assert (tmp_path / "guard_state.json").exists()


def test_default_idle_ttl_is_two_minutes() -> None:
    assert DEFAULT_IDLE_TTL_MS == 120_000


def test_write_state_if_changed_skips_identical_fingerprint(tmp_path: Path) -> None:
    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    state, _ = _python_daemon_tick(process_cache, [], [], 5, 1000, tick=0)
    state_path = tmp_path / "guard_state.json"

    fp = _write_state_if_changed(tmp_path, state, last_fingerprint=None, tick=1)
    first_mtime = state_path.stat().st_mtime
    assert state_path.exists()

    _write_state_if_changed(tmp_path, state, last_fingerprint=fp, tick=2)
    assert state_path.stat().st_mtime == first_mtime

    _write_state_if_changed(tmp_path, state, last_fingerprint=fp, tick=STATE_WRITE_EVERY_N_TICKS)
    assert state_path.stat().st_mtime >= first_mtime


def test_process_status_rows_failure_returns_none_and_engages_psutil_fallback(monkeypatch) -> None:
    # regression: [] here made _scan_process_fields report zero processes
    # instead of falling back to the budgeted psutil walk.
    from cluxion_runtime import guard_daemon_host

    def broken_ps(*_args, **_kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(guard_daemon_host.subprocess, "run", broken_ps)
    assert guard_daemon_host._process_status_rows() is None
    cache = guard_daemon_host._scan_process_fields()
    assert cache.process_count > 0
