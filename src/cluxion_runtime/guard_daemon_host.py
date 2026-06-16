"""Blocking guard daemon entry point for PyPI wheels without the CLI binary."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

STATE_FILE_NAME = "guard_state.json"
HEARTBEAT_FILE_NAME = "guard_heartbeat"
PID_FILE_NAME = "guard_daemon.pid"
DEFAULT_IDLE_TTL_MS = 600_000
PROC_SCAN_EVERY_N_TICKS = 5
_MAX_REPORTED_PIDS = 50
_MIN_INTERVAL_MS = 100
_MIN_WINDOW = 1


@dataclass
class ProcessScanCache:
    process_count: int
    zombie_count: int
    zombie_pids: list[int]


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _scan_process_fields() -> ProcessScanCache:
    zombie_pids: list[int] = []
    count = 0
    for proc in psutil.process_iter(["status"]):
        count += 1
        try:
            if proc.info["status"] == psutil.STATUS_ZOMBIE:
                zombie_pids.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    zombie_pids.sort()
    zombie_count = len(zombie_pids)
    return ProcessScanCache(
        process_count=count,
        zombie_count=zombie_count,
        zombie_pids=zombie_pids[:_MAX_REPORTED_PIDS],
    )


def _cheap_sample() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu = psutil.cpu_percent(interval=None)
    return {
        "total_ram_mb": memory.total // 1_048_576,
        "available_ram_mb": memory.available // 1_048_576,
        "swap_used_mb": swap.used // 1_048_576,
        "cpu_percent": float(cpu),
    }


def _build_current_snapshot(
    process_cache: ProcessScanCache,
    cheap: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "total_ram_mb": cheap["total_ram_mb"],
        "available_ram_mb": cheap["available_ram_mb"],
        "swap_used_mb": cheap["swap_used_mb"],
        "cpu_percent": cheap["cpu_percent"],
        "process_count": process_cache.process_count,
        "zombie_count": process_cache.zombie_count,
        "zombie_pids": list(process_cache.zombie_pids),
        "sampled_at_ms": _epoch_ms(),
    }


def _push_window_sample(
    cpu_window: list[float],
    ram_window: list[int],
    window: int,
    cpu: float,
    ram: int,
) -> None:
    if len(cpu_window) == window:
        cpu_window.pop(0)
        ram_window.pop(0)
    cpu_window.append(cpu)
    ram_window.append(ram)


def _build_daemon_state(
    current: dict[str, Any],
    cpu_window: list[float],
    ram_window: list[int],
    interval_ms: int,
) -> dict[str, Any]:
    return {
        "ok": True,
        "current": current,
        "window": {
            "samples": len(cpu_window),
            "cpu_avg": sum(cpu_window) / len(cpu_window),
            "cpu_peak": max(cpu_window),
            "min_available_ram_mb": min(ram_window) if ram_window else 0,
        },
        "interval_ms": interval_ms,
        "updated_at_ms": _epoch_ms(),
    }


def _python_daemon_tick(
    process_cache: ProcessScanCache,
    cpu_window: list[float],
    ram_window: list[int],
    window: int,
    interval_ms: int,
    tick: int,
) -> tuple[dict[str, Any], ProcessScanCache]:
    if tick % PROC_SCAN_EVERY_N_TICKS == 0:
        process_cache = _scan_process_fields()
    cheap = _cheap_sample()
    current = _build_current_snapshot(process_cache, cheap)
    _push_window_sample(
        cpu_window,
        ram_window,
        window,
        float(current["cpu_percent"]),
        int(current["available_ram_mb"]),
    )
    state = _build_daemon_state(current, cpu_window, ram_window, interval_ms)
    return state, process_cache


def _write_state_atomically(store_dir: Path, state: dict[str, Any]) -> None:
    state_path = store_dir / STATE_FILE_NAME
    tmp_path = store_dir / f"{STATE_FILE_NAME}.tmp"
    tmp_path.write_bytes(json.dumps(state, separators=(",", ":")).encode("utf-8"))
    os.replace(tmp_path, state_path)


def _idle_ttl_ms() -> int:
    raw = os.environ.get("CLUXION_GUARD_IDLE_TTL_MS")
    if raw is None:
        return DEFAULT_IDLE_TTL_MS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_IDLE_TTL_MS


def is_idle(heartbeat_mtime_ms: int, now_ms: int, ttl_ms: int) -> bool:
    return max(0, now_ms - heartbeat_mtime_ms) > ttl_ms


def _heartbeat_mtime_ms(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return None


def _remove_pidfile(base: Path) -> None:
    (base / PID_FILE_NAME).unlink(missing_ok=True)


def _check_idle_exit(base: Path, idle_ttl_ms: int, *, now_ms: int | None = None) -> bool:
    heartbeat_path = base / HEARTBEAT_FILE_NAME
    mtime = _heartbeat_mtime_ms(heartbeat_path)
    if mtime is None:
        return False
    now = _epoch_ms() if now_ms is None else now_ms
    if is_idle(mtime, now, idle_ttl_ms):
        _remove_pidfile(base)
        return True
    return False


def _daemon_loop_step(
    base: Path,
    *,
    process_cache: ProcessScanCache,
    cpu_window: list[float],
    ram_window: list[int],
    window: int,
    interval_ms: int,
    tick: int,
    idle_ttl_ms: int,
    now_ms: int | None = None,
) -> tuple[bool, ProcessScanCache]:
    if _check_idle_exit(base, idle_ttl_ms, now_ms=now_ms):
        return False, process_cache
    state, process_cache = _python_daemon_tick(
        process_cache,
        cpu_window,
        ram_window,
        window,
        interval_ms,
        tick,
    )
    _write_state_atomically(base, state)
    return True, process_cache


def _run_python_daemon(store_dir: str, interval_ms: int, window: int) -> None:
    base = Path(store_dir)
    base.mkdir(parents=True, exist_ok=True)
    interval_ms = max(_MIN_INTERVAL_MS, int(interval_ms))
    window = max(_MIN_WINDOW, int(window))
    idle_ttl_ms = _idle_ttl_ms()

    process_cache = ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    cpu_window: list[float] = []
    ram_window: list[int] = []
    tick = 0

    while True:
        keep_running, process_cache = _daemon_loop_step(
            base,
            process_cache=process_cache,
            cpu_window=cpu_window,
            ram_window=ram_window,
            window=window,
            interval_ms=interval_ms,
            tick=tick,
            idle_ttl_ms=idle_ttl_ms,
        )
        if not keep_running:
            return
        tick += 1
        time.sleep(interval_ms / 1000.0)


def main() -> int:
    if len(sys.argv) != 4:
        print(json.dumps({"ok": False, "error": "usage"}), file=sys.stderr)
        return 1
    _, store_dir, interval_ms, window = sys.argv
    try:
        import cluxion_queue_native
    except ImportError:
        _run_python_daemon(store_dir, int(interval_ms), int(window))
        return 0
    run_guard_daemon = getattr(cluxion_queue_native, "run_guard_daemon", None)
    if run_guard_daemon is None:
        print(json.dumps({"ok": False, "error": "run_guard_daemon_missing"}), file=sys.stderr)
        return 1
    run_guard_daemon(store_dir, int(interval_ms), int(window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())