# Task: native-less Python fallback for the guard daemon host

## Context
`src/cluxion_runtime/guard_daemon_host.py` is the daemon entry used when the Rust CLI
binary is absent. Today, if `import cluxion_queue_native` fails it returns
`native_module_missing` and exits 1 — so on a platform that only received the pure
py3-none-any wheel (no bundled native module), the resource-guard DAEMON never runs (no
rolling resource state). The queue already degrades to a pure-Python backend; the daemon
must too. Add a pure-Python daemon fallback that mirrors the Rust `run_daemon`.

## Reference: the Rust daemon (already optimized in THIS repo)
`rust/cluxion_queue/src/guard.rs::run_daemon` uses a FIXED DUAL-CADENCE loop:
- EVERY tick (`interval_ms`, default 1000): refresh memory+cpu, build a `current`
  snapshot, push cpu & available-ram into a rolling window, write state (tmp + atomic
  rename).
- EVERY `PROC_SCAN_EVERY_N_TICKS = 5` ticks: a full process scan → process_count,
  zombie_count, zombie_pids — CACHED and reused on cheap ticks.
- The FIRST tick (tick 0) scans immediately so process fields are never uninitialized.
The Python fallback MUST produce schema-identical state JSON and the same cadence.

## Implement (in guard_daemon_host.py only)
- When native IS importable: keep current behavior exactly (call `run_guard_daemon`).
- When native is NOT importable: run `_run_python_daemon(store_dir, interval_ms, window)`:
  - cheap tick: `psutil.virtual_memory()`, `psutil.swap_memory()`,
    `psutil.cpu_percent(interval=None)` → total_ram_mb / available_ram_mb / swap_used_mb /
    cpu_percent; push cpu & available_ram into the window; write state atomically
    (`os.replace`).
  - every 5th tick: `psutil.process_iter(["status"])` → process_count + zombie pids
    (sorted, truncated to 50); cache them.
  - first tick (tick 0) scans immediately.
- Refactor the per-tick work into a testable helper, e.g.
  `_python_daemon_tick(state, window_cpu, window_ram, window, tick) -> (state_dict, cache)`,
  so tests can call ONE tick without the infinite loop.

## Schema (MUST be byte-identical to the Rust daemon)
Top-level: `ok, current, window{samples,cpu_avg,cpu_peak,min_available_ram_mb},
interval_ms, updated_at_ms`.
`current`: `ok, total_ram_mb, available_ram_mb, swap_used_mb, cpu_percent,
process_count, zombie_count, zombie_pids, sampled_at_ms`. Same types/units (MB = bytes //
1_048_576; *_ms = epoch millis).

## Parity / clamps
Default interval 1000, window 10 (match `guard_bridge.DEFAULT_INTERVAL_MS` / `DEFAULT_WINDOW`).
Clamp interval_ms to >= 100, window to >= 1. You MAY reuse `guard_bridge._python_sample`
logic, but the dual-cadence split is REQUIRED — do NOT call `process_iter` every tick.

## Invariants (MUST hold)
1. State written every cheap tick (<= interval_ms) → never stale (< 3000ms STALE_AFTER_MS).
2. JSON schema byte-identical to the Rust daemon (consumers are schema-agnostic to producer).
3. Report-only: the fallback never signals/kills any process.
4. Native path behavior UNCHANGED when native is available.

## Tests (must pass; add coverage)
- New `tests/runtime/` coverage: force native unavailable, call `_python_daemon_tick`
  once on a scan tick and once on a cheap tick; assert (a) all required keys + types,
  (b) process fields cached/reused between scan ticks, (c) window grows by one per tick.
- `uv run pytest tests/runtime/` green (existing native-path tests stay green).

## Out of scope (DO NOT)
- No maturin / wheel / build / pip install / version bump / PyPI publish.
- No changes to Rust, to enforce/auto_enforce, to ownership, or to the native path.

## Done
`guard_daemon_host` runs a schema-correct dual-cadence pure-Python daemon when native is
missing; tests green; invariants hold. Report a concise diff summary.
