"""Resource guard bridge: real-time sampling, fail-closed process scanning,
lifecycle control for the Rust guard daemon, and opt-in escalation.

Backend chain mirrors queue_bridge (native -> subprocess -> python/psutil).
The guard is report-first: scanning never acts on anything, and enforce()
is dry-run by default. When enforcement is explicitly applied it signals
only processes whose lineage provably reaches a registered owned root,
re-verified immediately before each signal (psutil's create_time identity
defends against PID reuse). Self, its ancestors, the registered roots
themselves, and the guard daemon are never signalled. External processes
are reported, never touched — see the blind-kill ownership-gate design.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from cluxion_runtime.resources import queue_bridge

if TYPE_CHECKING:
    from collections.abc import Mapping

STATE_FILE_NAME = "guard_state.json"
PID_FILE_NAME = "guard_daemon.pid"
DEFAULT_INTERVAL_MS = 200
DEFAULT_WINDOW = 25
STALE_AFTER_MS = 3_000
_MAX_REPORTED = 50
DEFAULT_ENFORCE_CPU = 90.0
DEFAULT_ENFORCE_RSS_MB = 4096
DEFAULT_GRACE_SECONDS = 3.0
DEFAULT_SUSTAINED_CPU = 85.0
DEFAULT_RAM_FLOOR_MB = 1024
_CPU_WINDOW_SECONDS = 0.1


def sample(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One full system sample (RAM/swap/CPU/zombies)."""
    body = dict(payload or {})
    backend = queue_bridge.resolve_backend()
    if backend == "native":
        return queue_bridge._invoke_native("guard-sample", body)
    if backend == "subprocess":
        return queue_bridge._invoke_subprocess("guard-sample", body)
    return _python_sample(body)


def scan(
    owned_roots: list[int],
    *,
    cpu_threshold: float = 50.0,
    rss_threshold_mb: int = 1024,
) -> dict[str, Any]:
    """Scan processes against registered owner roots.

    Fail-closed: only lineage provably reaching an owned root is `owned`;
    everything else is external and must only be reported, never acted on.
    """
    body: dict[str, Any] = {
        "owned_roots": [int(pid) for pid in owned_roots],
        "cpu_threshold": float(cpu_threshold),
        "rss_threshold_mb": int(rss_threshold_mb),
    }
    backend = queue_bridge.resolve_backend()
    if backend == "native":
        return queue_bridge._invoke_native("guard-scan", body)
    if backend == "subprocess":
        return queue_bridge._invoke_subprocess("guard-scan", body)
    return _python_scan(body)


def enforce(
    owned_roots: list[int],
    *,
    cpu_threshold: float = DEFAULT_ENFORCE_CPU,
    rss_threshold_mb: int = DEFAULT_ENFORCE_RSS_MB,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    dry_run: bool = True,
    protect: list[int] | tuple[int, ...] = (),
    store_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Escalate against runaway OWNED processes only (dry-run by default).

    Candidate discovery is uncapped (direct psutil walk, not the capped scan
    report). A process is a candidate only when its lineage reaches an owned
    root, it exceeds a threshold, and it is not protected. The registered
    roots themselves are never candidates — killing a root kills the agent.
    Zombies are never signalled (already dead; their parent must reap them).
    """
    roots = [int(pid) for pid in owned_roots]
    if not roots:
        return {"ok": False, "error": "owned_roots_required", "dry_run": dry_run}
    protected = _protected_pids(protect, store_dir)

    procs: dict[int, psutil.Process] = {}
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)  # prime the per-process CPU window
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        procs[proc.pid] = proc
    time.sleep(_CPU_WINDOW_SECONDS)

    parents: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for pid, proc in procs.items():
        try:
            with proc.oneshot():
                info = {
                    "pid": pid,
                    "ppid": proc.ppid(),
                    "name": proc.name(),
                    "cpu_percent": float(proc.cpu_percent(None)),
                    "rss_mb": proc.memory_info().rss // 1_048_576,
                    "zombie": proc.status() == psutil.STATUS_ZOMBIE,
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        parents[pid] = int(info["ppid"])
        rows.append(info)

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    zombies_ignored = 0
    for info in rows:
        if not _is_owned(info["pid"], roots, parents):
            continue  # external: scan() reports it; enforce never touches it
        if info["zombie"]:
            zombies_ignored += 1
            continue
        if info["cpu_percent"] < cpu_threshold and info["rss_mb"] < rss_threshold_mb:
            continue
        if info["pid"] in roots:
            skipped.append({"pid": info["pid"], "reason": "owned_root"})
            continue
        if info["pid"] in protected:
            skipped.append({"pid": info["pid"], "reason": "protected"})
            continue
        candidates.append({key: info[key] for key in ("pid", "ppid", "name", "cpu_percent", "rss_mb")})

    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "owned_roots": roots,
        "candidates": candidates,
        "skipped": skipped,
        "zombies_ignored": zombies_ignored,
        "terminated": [],
        "killed": [],
    }
    if dry_run:
        return result

    to_wait: list[psutil.Process] = []
    for entry in candidates:
        proc = procs.get(int(entry["pid"]))
        if proc is None or not _still_owned_and_same(proc, roots):
            skipped.append({"pid": entry["pid"], "reason": "identity_or_ownership_changed"})
            continue
        try:
            proc.terminate()
            to_wait.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            skipped.append({"pid": entry["pid"], "reason": "gone_or_denied"})
    gone, alive = psutil.wait_procs(to_wait, timeout=max(0.1, float(grace_seconds)))
    result["terminated"] = sorted(proc.pid for proc in gone)
    killed: list[int] = []
    for proc in alive:
        try:
            proc.kill()
            killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    result["killed"] = sorted(killed)
    return result


def auto_enforce(
    owned_roots: list[int],
    *,
    sustained_cpu: float = DEFAULT_SUSTAINED_CPU,
    ram_floor_mb: int = DEFAULT_RAM_FLOOR_MB,
    min_samples: int = DEFAULT_WINDOW,
    cpu_threshold: float = DEFAULT_ENFORCE_CPU,
    rss_threshold_mb: int = DEFAULT_ENFORCE_RSS_MB,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    dry_run: bool = True,
    protect: list[int] | tuple[int, ...] = (),
    store_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Daemon-window gated enforcement (fail-closed, dry-run by default).

    Triggers only on *sustained* pressure measured by the guard daemon's
    rolling window (cpu_avg / min_available_ram_mb), never on a single
    sample spike. No daemon state, a stale state file, or a still-warming
    window means no judgement is possible, so nothing happens. When the
    window does show sustained pressure the actual signalling is delegated
    to enforce(), keeping a single kill path with its identity re-checks.
    """
    roots = [int(pid) for pid in owned_roots]
    if not roots:
        return {"ok": False, "triggered": False, "error": "owned_roots_required", "dry_run": dry_run}
    state = read_daemon_state(store_dir=store_dir)
    if state is None:
        return {"ok": False, "triggered": False, "error": "daemon_state_missing", "dry_run": dry_run}
    if state.get("stale", True):
        return {"ok": False, "triggered": False, "error": "daemon_state_stale", "dry_run": dry_run}
    window = state.get("window")
    window = window if isinstance(window, dict) else {}
    samples = int(window.get("samples", 0))
    summary: dict[str, Any] = {
        "samples": samples,
        "cpu_avg": window.get("cpu_avg"),
        "min_available_ram_mb": window.get("min_available_ram_mb"),
    }
    if samples < max(1, int(min_samples)):
        return {
            "ok": False,
            "triggered": False,
            "error": "window_warming_up",
            "dry_run": dry_run,
            "window": summary,
        }
    reasons: list[str] = []
    cpu_avg = window.get("cpu_avg")
    if cpu_avg is not None and float(cpu_avg) >= float(sustained_cpu):
        reasons.append(f"cpu_avg {float(cpu_avg):.1f} >= sustained_cpu {float(sustained_cpu):.1f}")
    min_ram = window.get("min_available_ram_mb")
    if min_ram is not None and float(min_ram) <= float(ram_floor_mb):
        reasons.append(f"min_available_ram_mb {float(min_ram):.0f} <= ram_floor_mb {int(ram_floor_mb)}")
    if not reasons:
        return {"ok": True, "triggered": False, "dry_run": dry_run, "window": summary}
    result = enforce(
        roots,
        cpu_threshold=cpu_threshold,
        rss_threshold_mb=rss_threshold_mb,
        grace_seconds=grace_seconds,
        dry_run=dry_run,
        protect=protect,
        store_dir=store_dir,
    )
    result["triggered"] = True
    result["trigger_reasons"] = reasons
    result["window"] = summary
    return result


def _still_owned_and_same(proc: psutil.Process, roots: list[int]) -> bool:
    """Last check before signalling: same process (create_time identity via
    is_running) and lineage still reaches an owned root."""
    try:
        if not proc.is_running():
            return False
        lineage = {parent.pid for parent in proc.parents()}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return any(root in lineage for root in roots)


def _protected_pids(protect: list[int] | tuple[int, ...], store_dir: Path | str | None) -> set[int]:
    protected = {os.getpid()} | {int(pid) for pid in protect}
    with contextlib.suppress(psutil.Error):
        protected |= {parent.pid for parent in psutil.Process().parents()}
    with contextlib.suppress(OSError, ValueError):
        protected.add(int((_store_base(store_dir) / PID_FILE_NAME).read_text(encoding="utf-8").strip()))
    return protected


def read_daemon_state(*, store_dir: Path | str | None = None) -> dict[str, Any] | None:
    """Read the daemon's published state. Returns None when absent; sets
    ``stale`` when the file is older than STALE_AFTER_MS."""
    base = _store_base(store_dir)
    state_path = base / STATE_FILE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    updated = int(state.get("updated_at_ms", 0))
    state["stale"] = (int(time.time() * 1000) - updated) > STALE_AFTER_MS
    return state


def start_daemon(
    *,
    store_dir: Path | str | None = None,
    interval_ms: int = DEFAULT_INTERVAL_MS,
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Spawn the Rust guard daemon (detached) and record its pidfile."""
    base = _store_base(store_dir)
    base.mkdir(parents=True, exist_ok=True)
    existing = daemon_status(store_dir=base)
    if existing["running"]:
        return {"ok": True, "started": False, "reason": "already_running", **existing}
    binary = queue_bridge._queue_binary()
    if Path(binary).exists() or _which(binary):
        host = "binary"
        cmd = [binary, "guard-daemon", str(base), str(int(interval_ms)), str(int(window))]
    elif _native_guard_available():
        host = "python"
        cmd = [
            sys.executable,
            "-m",
            "cluxion_runtime.guard_daemon_host",
            str(base),
            str(int(interval_ms)),
            str(int(window)),
        ]
    else:
        return {"ok": False, "started": False, "reason": "binary_not_found", "binary": binary}
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (base / PID_FILE_NAME).write_text(str(process.pid), encoding="utf-8")
    return {
        "ok": True,
        "started": True,
        "pid": process.pid,
        "interval_ms": int(interval_ms),
        "host": host,
    }


def stop_daemon(*, store_dir: Path | str | None = None) -> dict[str, Any]:
    """Stop the daemon we spawned. Fail-closed: the pid from our pidfile is
    signalled only after its identity is verified as our guard daemon."""
    base = _store_base(store_dir)
    pid_path = base / PID_FILE_NAME
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return {"ok": True, "stopped": False, "reason": "no_pidfile"}
    if not _is_our_daemon(pid):
        pid_path.unlink(missing_ok=True)
        return {"ok": False, "stopped": False, "reason": "identity_mismatch", "pid": pid}
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=3)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        proc.kill()
    pid_path.unlink(missing_ok=True)
    return {"ok": True, "stopped": True, "pid": pid}


def daemon_status(*, store_dir: Path | str | None = None) -> dict[str, Any]:
    base = _store_base(store_dir)
    try:
        pid = int((base / PID_FILE_NAME).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return {"running": False, "pid": None}
    return {"running": _is_our_daemon(pid), "pid": pid}


def _store_base(store_dir: Path | str | None) -> Path:
    if store_dir is None:
        return queue_bridge.default_store_dir()
    return Path(store_dir)


def _is_our_daemon(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if any("cluxion_runtime.guard_daemon_host" in part for part in cmdline):
        return True
    return any("guard-daemon" in part for part in cmdline) and any("cluxion-queue" in part for part in cmdline)


def _native_guard_available() -> bool:
    return queue_bridge._native is not None


def _which(binary: str) -> bool:
    import shutil

    return shutil.which(binary) is not None


def _python_sample(body: Mapping[str, Any]) -> dict[str, Any]:
    cpu_sample_ms = int(body.get("cpu_sample_ms", 100))
    interval = None if cpu_sample_ms <= 0 else cpu_sample_ms / 1000.0
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu = psutil.cpu_percent(interval=interval)
    zombie_pids: list[int] = []
    count = 0
    for proc in psutil.process_iter(["status"]):
        count += 1
        if proc.info["status"] == psutil.STATUS_ZOMBIE:
            zombie_pids.append(proc.pid)
    zombie_pids.sort()
    return {
        "ok": True,
        "total_ram_mb": memory.total // 1_048_576,
        "available_ram_mb": memory.available // 1_048_576,
        "swap_used_mb": swap.used // 1_048_576,
        "cpu_percent": float(cpu),
        "process_count": count,
        "zombie_count": len(zombie_pids),
        "zombie_pids": zombie_pids[:_MAX_REPORTED],
        "sampled_at_ms": int(time.time() * 1000),
    }


def _python_scan(body: Mapping[str, Any]) -> dict[str, Any]:
    owned_roots = [int(pid) for pid in body.get("owned_roots", [])]
    cpu_hot = float(body.get("cpu_threshold", 50.0))
    rss_hot_mb = int(body.get("rss_threshold_mb", 1024))
    parents: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cpu_percent", "memory_info", "status"]):
        info = proc.info
        if info["ppid"] is not None:
            parents[info["pid"]] = info["ppid"]
        rows.append(info)
    zombies: list[dict[str, Any]] = []
    hot: list[dict[str, Any]] = []
    owned_alive = 0
    for info in rows:
        owned = _is_owned(info["pid"], owned_roots, parents)
        is_zombie = info["status"] == psutil.STATUS_ZOMBIE
        if owned and not is_zombie:
            owned_alive += 1
        rss_mb = (info["memory_info"].rss // 1_048_576) if info["memory_info"] else 0
        entry = {
            "pid": info["pid"],
            "ppid": parents.get(info["pid"]),
            "name": info["name"] or "",
            "cpu_percent": float(info["cpu_percent"] or 0.0),
            "rss_mb": rss_mb,
            "owned": owned,
        }
        if is_zombie:
            if len(zombies) < _MAX_REPORTED:
                zombies.append(entry)
        elif (entry["cpu_percent"] >= cpu_hot or rss_mb >= rss_hot_mb) and len(hot) < _MAX_REPORTED:
            hot.append(entry)
    return {
        "ok": True,
        "owned_roots": owned_roots,
        "owned_alive": owned_alive,
        "zombies": zombies,
        "hot": hot,
        "scanned_at_ms": int(time.time() * 1000),
    }


def _is_owned(pid: int, owned_roots: list[int], parents: dict[int, int]) -> bool:
    if not owned_roots:
        return False
    current = pid
    for _ in range(64):
        if current in owned_roots:
            return True
        parent = parents.get(current)
        if parent is None or parent == current:
            return False
        current = parent
    return False


__all__ = [
    "auto_enforce",
    "daemon_status",
    "enforce",
    "read_daemon_state",
    "sample",
    "scan",
    "start_daemon",
    "stop_daemon",
]
