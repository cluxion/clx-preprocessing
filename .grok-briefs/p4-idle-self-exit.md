# Task: Idle self-exit for the guard daemon (P4 — orphan lifecycle)

## Context
`guard_watch.on_session_start` ensures the daemon on every hermes session (idempotent), but there is
NO session-end stop and the daemon is detached (`start_new_session=True`). Once started it lives
forever as an orphan (PPID=1) — observed: a 16h orphan. With the P2 dual-cadence fix the daemon now
uses ~1% CPU, but it should still self-terminate after a stretch of no hermes activity, so orphans
don't accumulate. Implement a heartbeat-driven idle self-exit.

## Design (heartbeat)
- A heartbeat file `guard_heartbeat` in the daemon `store_dir`; its mtime = last hermes activity.
- hermes hooks (`guard_watch.on_session_start` AND `post_tool_call`) TOUCH the heartbeat. post_tool_call
  is already throttled (~30s), so touching there is cheap and frequent enough.
- `start_daemon` TOUCHES the heartbeat once at spawn (so a fresh daemon isn't immediately "idle").
- The daemon checks staleness: if `now - heartbeat_mtime > IDLE_TTL_MS`, it exits CLEANLY (remove its
  own pidfile, then return / exit 0). Check every tick (cheap stat) or every few ticks.
- `IDLE_TTL_MS` default = 600_000 (10 min), overridable via env `CLUXION_GUARD_IDLE_TTL_MS`. MUST be
  comfortably larger than post_tool_call's ~30s throttle so active sessions NEVER trigger exit.

## Where
- Rust `rust/cluxion_queue/src/guard.rs::run_daemon`: add the heartbeat staleness check in the loop;
  on idle, break and return Ok. Add an IDLE_TTL constant; read the env override at the entry that
  passes params (or in run_daemon). Refactor the idle decision into a small testable fn
  (e.g. `fn is_idle(heartbeat_mtime_ms, now_ms, ttl_ms) -> bool`). Stay report-only.
- Python `src/cluxion_runtime/guard_daemon_host.py::_run_python_daemon`: mirror the same idle check +
  clean pidfile removal + return. Same testable-helper refactor.
- `src/cluxion_runtime/resources/guard_bridge.py`: heartbeat path constant + a
  `touch_heartbeat(store_dir)` helper; `start_daemon` touches it at spawn.
- `src/cluxion_agentplugin_preprocessing/guard_watch.py`: call `touch_heartbeat` in `on_session_start`
  and `post_tool_call` (best-effort, never raise into the host — match the existing try/except style).

## Invariants (MUST hold — do NOT break)
1. While ANY hermes session is active (post_tool_call firing within IDLE_TTL), the daemon STAYS UP.
2. On idle exit the daemon removes ITS OWN pidfile, so the next `on_session_start` cleanly restarts it
   (`daemon_status` reports not-running after idle exit).
3. Report-only preserved; published state JSON schema UNCHANGED; stale/window semantics unchanged.
4. The P2 dual-cadence loop (1000ms tick, refresh_processes every PROC_SCAN_EVERY_N_TICKS=5 ticks) is
   preserved EXACTLY — you are ONLY adding an idle-exit check, not changing cadence.
5. Pidfile identity gates (`stop_daemon`/`_is_our_daemon`) keep working.

## Tests (must pass; add coverage)
- Rust unit test of the idle-decision fn: stale heartbeat → true; fresh → false. Don't unit-test the
  infinite loop.
- Python test: `_run_python_daemon` idle helper exits on a stale heartbeat AND removes the pidfile;
  fresh heartbeat keeps running (one-iteration helper).
- guard_bridge: `touch_heartbeat` updates mtime; `start_daemon` touches it.
- `cargo test --manifest-path rust/cluxion_queue/Cargo.toml` and `uv run pytest tests/runtime/` green.

## Out of scope (DO NOT)
- No version bump / build / wheel / pip install / publish.
- No change to enforce/auto_enforce/ownership, no change to the P2 cadence values.

## Done
Daemon self-exits cleanly after IDLE_TTL with no heartbeat (removing its pidfile), stays up during
active sessions, schema/report-only/P2-cadence all preserved, tests green. Concise diff summary.
