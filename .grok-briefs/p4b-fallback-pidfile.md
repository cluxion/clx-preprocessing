# Task: python fallback daemon — remove pidfile on idle exit (consistency with native)

## Context
P4 added idle self-exit. The native daemon (rust/cluxion_queue/src/guard.rs run_daemon) removes its
own pidfile on idle exit. The PYTHON FALLBACK (`_run_python_daemon` / `_daemon_loop_step` in
src/cluxion_runtime/guard_daemon_host.py) returns on idle but does NOT remove its pidfile
(around line 197-198 / 234). It's harmless in practice (daemon_status uses `_is_our_daemon` to
filter a stale pidfile), but for consistency and a clean lifecycle the fallback should remove its
pidfile on idle exit, exactly like native.

## Implement (src/cluxion_runtime/guard_daemon_host.py)
- When `_run_python_daemon` exits because of idle (keep_running False from `_check_idle_exit`),
  remove its own pidfile (`store_dir / PID_FILE_NAME`) before returning — mirroring native
  run_daemon's `remove_file(pid_path)` on idle.
- Use the PID_FILE_NAME constant consistently (import from guard_bridge or define identically).

## Invariants (MUST hold)
- Native path UNCHANGED. The P2 dual-cadence loop and the P4 idle-decision logic UNCHANGED — only
  add pidfile cleanup on the fallback's idle-exit branch.
- Non-idle behavior unchanged; report-only preserved.

## Tests
- Extend tests: fallback idle exit removes the pidfile (one-iteration helper writes a pidfile, then
  a stale heartbeat triggers idle exit, assert pidfile gone). `uv run pytest tests/runtime/` green.

## Out of scope
- No version bump / build / publish. No change to native, enforce, ownership, or cadence.

## Done
python fallback removes its pidfile on idle exit (consistent with native); a test proves it; all
tests green. Concise diff summary.
