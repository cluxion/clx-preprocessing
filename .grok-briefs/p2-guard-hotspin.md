# Task: Eliminate guard-daemon CPU hot-spin (P2) — fixed dual-cadence refactor

## Context
The Rust guard daemon `run_daemon` in `rust/cluxion_queue/src/guard.rs` burns ~13% CPU
continuously (measured: 16h26m elapsed → 128min CPU). Root cause: every 200ms it calls
`sys.refresh_processes(ProcessesToUpdate::All, true)` over ~881 processes (full-table
syscalls) AND writes `guard_state.json` (write+rename). Both at 5Hz.

Goal: cut the hot loop to ~1% CPU by **decoupling cadence** — keep cheap resource
sampling + state writes frequent (so freshness invariants hold), but run the expensive
full process scan infrequently. DO NOT add adaptive acceleration in this task (a later
task handles that); implement a simple, correct, fixed dual-cadence loop.

## Files in scope
- `rust/cluxion_queue/src/guard.rs` — refactor `run_daemon` + constants ONLY.
- `src/cluxion_runtime/resources/guard_bridge.py` — adjust `DEFAULT_INTERVAL_MS`, `DEFAULT_WINDOW` for parity.

Do NOT touch: `sample()`, `scan()`, `sample_from()` single-shot semantics used by the
non-daemon code paths, the ownership gate, `enforce()`, or any Python kill path.

## Algorithm (implement exactly)
Rewrite the `run_daemon` loop (guard.rs ~line 161-204) as a tick loop:

- Tick cadence = `interval` (the daemon's `interval_ms`, new default 1000ms).
- EVERY tick (cheap): `sys.refresh_memory()` + `sys.refresh_cpu_usage()`; compute
  cpu_percent / available_ram_mb / total_ram_mb / swap_used_mb; push cpu & ram into the
  rolling window; build `current` JSON; write state (tmp + atomic rename). **Do NOT call
  refresh_processes on cheap ticks.**
- EVERY Nth tick (expensive), where `N = PROC_SCAN_EVERY_N_TICKS = 5`: call
  `sys.refresh_processes(ProcessesToUpdate::All, true)`, recompute `process_count`,
  `zombie_count`, `zombie_pids` (same logic as current `sample_from`), and CACHE them in
  loop-local variables.
- The FIRST iteration must do an expensive scan immediately (tick 0), so the cached
  process_count/zombie fields are populated from the start (never emit a tick with
  uninitialized/zero process fields before the first scan).
- On cheap ticks, the `current` JSON reuses the CACHED process_count/zombie_count/
  zombie_pids from the last expensive scan.

Add a `PROC_SCAN_EVERY_N_TICKS: u64 = 5` constant near the other daemon constants.

## Constant changes
- `guard.rs`: `DEFAULT_DAEMON_INTERVAL_MS` 200 → **1000**; `DEFAULT_DAEMON_WINDOW` 25 → **10**; add `PROC_SCAN_EVERY_N_TICKS = 5`.
- `guard_bridge.py`: `DEFAULT_INTERVAL_MS` 200 → **1000**; `DEFAULT_WINDOW` 25 → **10**. Leave `STALE_AFTER_MS = 3000` unchanged.

## Invariants (MUST hold — verify, do not break)
1. State file is written at least every cheap tick (≤1000ms) → always < STALE_AFTER_MS (3000ms). `read_daemon_state` must never see it stale under normal operation.
2. The published JSON schema is UNCHANGED. Top-level: `ok, current, window{samples,cpu_avg,cpu_peak,min_available_ram_mb}, interval_ms, updated_at_ms`. `current` keys: `ok, total_ram_mb, available_ram_mb, swap_used_mb, cpu_percent, process_count, zombie_count, zombie_pids, sampled_at_ms`. Same types.
3. Report-only: the daemon never signals/kills any process.
4. `interval.max(100)` clamp stays; window capacity = `window.max(1)`.
5. Window holds cpu & ram samples exactly as before (one push per cheap tick).

## Tests (must pass; add new coverage)
- `cargo test --manifest-path rust/cluxion_queue/Cargo.toml` — all pass.
- `pytest tests/runtime/test_guard.py` — all pass (run from repo root; use the repo venv or `uv run`).
- ADD a Rust unit test asserting: (a) after the loop body's scan-tick logic, process
  fields are cached and reused on non-scan ticks; (b) state JSON has all required keys.
  If a full daemon loop is hard to unit-test (infinite loop), refactor the per-tick body
  into a small pure-ish helper (e.g. `fn build_state(...)`/`fn scan_tick(...)`) that tests
  can call directly, WITHOUT changing the published behavior.

## Out of scope (DO NOT do)
- No maturin build, no wheel repack, no pip install, no version bump, no PyPI publish.
- No adaptive/backoff logic (separate task).
- No changes to enforce/auto_enforce/ownership logic.

## Done criteria
Code compiles, `cargo test` + `pytest tests/runtime/test_guard.py` green, invariants 1-5
hold, and the loop calls `refresh_processes` only on every 5th tick. Report a concise diff
summary of what changed.
