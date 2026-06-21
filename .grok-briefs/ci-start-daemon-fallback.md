# Task: start_daemon must spawn the Python fallback host when native is absent (CI fix)

## Context
CI (clean ubuntu, native .so NOT built) failed: `test_start_daemon_touches_heartbeat` —
`start_daemon` returns `started=False` when native is absent. Root cause: `guard_bridge.start_daemon`
only spawns when the Rust CLI binary exists OR `_native_guard_available()` is True; with neither
(no-native CI), it returns `binary_not_found`/`started=False`. But the pure-Python fallback daemon
`_run_python_daemon` (in `guard_daemon_host.py`) runs WITHOUT native (psutil only). So the fallback
is currently unreachable via start_daemon, and any platform without native gets no guard daemon.

## Fix (src/cluxion_runtime/resources/guard_bridge.py `start_daemon`)
- Make the python host the UNIVERSAL fallback: when neither the Rust CLI binary nor native is
  available, STILL spawn `python -m cluxion_runtime.guard_daemon_host <store> <interval> <window>`.
  `guard_daemon_host.main` already self-selects native vs pure-Python at runtime (native import →
  run_guard_daemon; ImportError → _run_python_daemon). So the host always runs.
- Keep existing precedence: Rust CLI binary host if present; otherwise the python host (which uses
  native when importable, else pure-Python). Only return an error if `sys.executable` itself is
  somehow unavailable (won't happen).
- heartbeat is still touched at spawn (existing P4 behavior).

## Invariants (MUST hold)
- start_daemon returns `started=True` + a pid whenever it can spawn the python host (i.e. always).
- Native present → still uses native (via python host or binary host); NO regression.
- Report-only / lifecycle / pidfile gates unchanged.

## Tests
- `test_start_daemon_touches_heartbeat` must pass with native ABSENT: monkeypatch so native is
  unavailable (binary missing AND native module None), assert start_daemon spawns the python host,
  `started=True`, heartbeat touched.
- Existing daemon tests stay green. `uv run pytest tests/runtime/` green in a no-native env
  (mock `queue_bridge._native = None` / `_native_guard_available()` False).

## Out of scope
- No version bump. No change to the daemon loop / cadence / idle-exit logic.

## Done
start_daemon spawns the python fallback host when native is absent (fallback daemon now reachable),
test passes without native; all tests green. Concise diff summary.
