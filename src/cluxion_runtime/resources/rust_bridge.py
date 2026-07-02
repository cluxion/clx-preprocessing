"""Conservative resource monitor and admission policy."""

from __future__ import annotations

import psutil

from cluxion_runtime.core.types import ResourceDecision, ResourceSnapshot


def collect_resource_snapshot() -> ResourceSnapshot:
    """Collect a resource snapshot, preferring the guard daemon's live state.

    A fresh daemon sample is a zero-cost file read and reflects a 200ms
    polling loop; psutil remains the fallback when no daemon is running.
    """
    from cluxion_runtime.resources import guard_bridge

    state = guard_bridge.read_daemon_state()
    if state is not None and not state.get("stale"):
        current = state.get("current")
        if isinstance(current, dict) and int(current.get("total_ram_mb", 0)) > 0:
            return ResourceSnapshot(
                total_ram_mb=int(current["total_ram_mb"]),
                available_ram_mb=int(current.get("available_ram_mb", 0)),
                swap_used_mb=int(current.get("swap_used_mb", 0)),
                cpu_percent=float(current.get("cpu_percent", 0.0)),
            )
    memory = psutil.virtual_memory()
    try:
        swap_used_mb = int(psutil.swap_memory().used // 1_048_576)
    except OSError:
        swap_used_mb = 0
    return ResourceSnapshot(
        total_ram_mb=int(memory.total // 1_048_576),
        available_ram_mb=int(memory.available // 1_048_576),
        swap_used_mb=swap_used_mb,
        cpu_percent=float(psutil.cpu_percent(interval=None)),
    )


def evaluate_pressure(
    snapshot: ResourceSnapshot,
    *,
    active_agents: int = 0,
    requested_parallel: int = 1,
) -> ResourceDecision:
    """Return a pressure decision from bounded local resource data."""
    _ = active_agents
    return _fallback_pressure(snapshot, requested_parallel)


def capacity_decision(
    work_kind: str,
    snapshot: ResourceSnapshot,
    *,
    expected_ram_mb: int = 0,
    user_mode: str = "balanced",
    network_quality: str = "good",
    active_codex: int = 0,
    active_grok: int = 0,
    active_claude: int = 0,
    active_browser: int = 0,
    active_tests: int = 0,
    active_generic: int = 0,
    active_qwen_sessions: int = 0,
    qwen_session_limit: int = 1,
) -> ResourceDecision:
    """Compute a fail-closed capacity envelope for work dispatch."""
    pressure = evaluate_pressure(snapshot)
    if not pressure.allowed:
        return ResourceDecision(**{**pressure.__dict__, "work_kind": work_kind})
    _ = (
        user_mode,
        network_quality,
        active_codex,
        active_grok,
        active_claude,
        active_browser,
        active_tests,
        active_qwen_sessions,
        qwen_session_limit,
    )
    required = max(1, expected_ram_mb)
    if snapshot.available_ram_mb < required:
        return ResourceDecision(False, "deferred", "memory_budget_low", 0, work_kind, snapshot.available_ram_mb)
    slot_count = _slot_count(work_kind, active_generic=active_generic)
    return ResourceDecision(True, "normal", "capacity_available", slot_count, work_kind, snapshot.available_ram_mb)


def _slot_count(work_kind: str, *, active_generic: int) -> int:
    if work_kind == "qwen":
        return 1
    if work_kind in {"codex", "grok", "claude", "security"}:
        return 1
    if work_kind in {"browser", "test"}:
        return 2
    return max(1, 4 - max(0, active_generic))


def _fallback_pressure(snapshot: ResourceSnapshot, requested_parallel: int) -> ResourceDecision:
    ratio = snapshot.available_ram_mb / max(1, snapshot.total_ram_mb)
    if ratio < 0.06 or (snapshot.cpu_percent > 97.0 and ratio < 0.10):
        return ResourceDecision(
            False, "emergency_stop", "fallback_pressure_emergency", 0, "generic", snapshot.available_ram_mb
        )
    if ratio < 0.10 or (snapshot.swap_used_mb > 0 and ratio < 0.15):
        return ResourceDecision(
            False, "pause_new_agents", "fallback_pressure_pause", 0, "generic", snapshot.available_ram_mb
        )
    if ratio < 0.15 or snapshot.swap_used_mb > 0:
        return ResourceDecision(
            True, "sequential_only", "fallback_pressure_sequential", 1, "generic", snapshot.available_ram_mb
        )
    return ResourceDecision(
        True, "normal", "fallback_pressure_normal", requested_parallel, "generic", snapshot.available_ram_mb
    )


__all__ = ["capacity_decision", "collect_resource_snapshot", "evaluate_pressure"]
