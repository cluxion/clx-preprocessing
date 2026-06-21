# Task: preprocessing — fix 5 live-audit defects (1 P2 hallucination-guard + 4 P3)

## Context
Installed build is 0.3.17. A live adversarial audit (running the installed site-packages build) confirmed 5 REAL defects. Fix all of them in the repo `src/`. Do NOT regress the 13 live-verified working functions (register() wiring, CLI check, doctor ok, 70% trigger guard, auto-compress middleware full-pipeline, Rust work-queue lifecycle, clarification gate, resource guard, Rust Stage-1 parity, hallucination guard for invented numbers). Do NOT bump the version in pyproject (deploy is handled separately by the caller). Do NOT touch `.grok-briefs/`.

## Defect 1 (P2 — CORE FEATURE BUG): Hallucination guard false-negative on dotted version/identifier numbers
File: `src/cluxion_runtime/core/llm_compress.py`, function `_token_traceable_in_source` (≈ lines 132-163).
Problem: A fabricated dotted-numeric token (version/IP/port-like) is WRONGLY judged traceable, so the guard fails to strip it. With source containing only `0.3.17`, the following are all wrongly judged traceable=True and survive in the summary: `0.3.7`, `0.3.1`, `3.17`, `0.17`, `0.31.7`, `1.3.17`. Two faulty code paths:
  (a) `all_digits_traceable` (≈143-156): splits the token via `re.findall(r"\d+")` into per-digit groups and returns True if EACH group occurs ANYWHERE in source independently — so `0.3.7` (groups 0,3,7) each individually occur in `0.3.17`.
  (b) `without_dots` branch (≈158-161): strips dots from BOTH token and source then substring-matches — so `0.31.7`→`0317` matches `0.3.17`→`0317`.
Fix: Treat version-like / multi-group dotted-or-underscored numeric tokens ATOMICALLY. If `norm_token` matches `^\d+(?:[._]\d+)+$` (i.e. a version / IP / dotted-numeric id with 2+ numeric groups), it is traceable ONLY if it appears in `norm_source` as a CONTIGUOUS normalized substring (the existing `norm_token in norm_source` at line 136, optionally plus contiguous numeric_variants of the WHOLE token). For such tokens, DO NOT fall through to the per-digit-group path (a) or the dot-stripping path (b). 
Preserve everything else: single numeric-group tokens keep the existing `_numeric_variants` normalization — `482k`==`482,000`, `5433`, `14mo`==`14months`, `72h`==`72-hour` MUST still be judged traceable. Alpha-bearing identifiers (`aurora-stg-3`, `recon_v4.py`) keep current single-group + alpha_prefix-in-source behavior.
Invariant + tests (add): source `"deployed version 0.3.17 of the package"` → `0.3.7`,`0.3.1`,`3.17`,`0.17`,`0.31.7`,`1.3.17` are NOT traceable (stripped); `0.3.17` IS traceable (kept). Source `"daily 482,000 events"` → summary `482k` KEPT. `_apply_hallucination_guard("... bumped to version 1.2.3 ...", source_with_only_0.3.17)` strips `1.2.3`.

## Defect 2 (P3): cluxion_context_compress TOOL path returns Stage-1-only context still above the 70% trigger
Symptom: the `cluxion_context_compress` tool routes (when cluxion_queue_native is importable) to the Rust `queue_bridge.compress_context` native backend, which runs only Stage-1 (truncate/digest) and can return `usage_after` of 1.0–1.3x (ABOVE the 70% trigger). It does emit an `ai_summary_request`, but a host that does not consume it is left above target. The `llm_request` auto-compress middleware is NOT affected (it imports the full Python `compress()` and reaches target) — only the explicit tool path is.
Fix: Make the tool path honest and safe (pick the cleaner of):
  (a) After the Rust Stage-1, if `usage_after` still exceeds the trigger ratio, continue into the Python full pipeline (forget + truncate_pinned_recent) so the returned context is at/below target; OR
  (b) Set explicit fields on the tool result: `reached_target: false` and `requires_summary: true` whenever the returned usage is above target, AND make the schema/docstring state unmistakably that the caller MUST act on `ai_summary_request`.
Never silently return above-trigger context that appears complete. Prefer (a) if it does not double-compress; else (b).
Invariant: tool result has `usage_after <= trigger`, OR an explicit `reached_target=false`. Middleware path unchanged.

## Defect 3 (P3 — PERFORMANCE): guard daemon steady-state cost + orphan lingering
Files: `src/cluxion_runtime/guard_daemon_host.py` (Python host), and if needed `rust/cluxion_queue/src/guard.rs` (note: editing Rust requires a maturin rebuild — PREFER solving in the Python host where possible).
Problem: the daemon rewrites `guard_state.json` every ~1s and proc-scans ~980 processes periodically (1-2% CPU bursts), and survives the parent session as a PPID-1 orphan until the 10-min idle TTL.
Fix (reduce steady-state cost; keep correctness):
  - Throttle state-file writes when idle: only rewrite `guard_state.json` when the state actually changed, or every Nth idle tick — not every 1s.
  - Shorten default idle TTL from 600000ms to 120000ms (2 min) so an orphan self-exits sooner; AND/OR register an `on_session_end` (or atexit/clean-shutdown) path that calls `stop_daemon` on clean session end so termination does not rely solely on the TTL.
  - KEEP: idempotent start (already_running check), fail-closed ownership gate, dual-cadence proc-scan (proc scan every Nth tick), heartbeat liveness semantics. Do not introduce a busy loop or duplicate daemons.
Invariant: idle daemon averages well under 1% CPU; state writes drop substantially when idle; orphan self-exits within the new (shorter) TTL; no duplicate daemons; ownership gate still fail-closed.

## Defect 4 (P3): guard tool returns useless empty-stderr error on an off-schema action
File: the guard tool/CLI handler in `cluxion_runtime` (where `runner.guard({'action': ...})` dispatches).
Problem: an action outside the known set (e.g. `'sample'`) returns `{'error':'cluxion-runtime failed','stderr':'','returncode':1}` with no actionable message.
Fix: validate `action` against the known set `{status, start, stop, enforce, auto-enforce}`; on mismatch emit an explicit message `"unknown guard action: <X> (expected: status|start|stop|enforce|auto-enforce)"` into stderr/JSON with a nonzero returncode, BEFORE attempting to run.
Invariant: off-schema action → clear named error mentioning the unknown action and the valid set. Valid actions behave exactly as before.

## Defect 5 (P3): plugin.yaml manifest is stale
File: `plugin.yaml`.
Problem: version is `0.1.9` (package is 0.3.x) and it lists only 7 of the 16 tools actually registered in `register(ctx)`.
Fix: sync `plugin.yaml` so the version matches the package version (read it from package metadata if the manifest format allows, else set it to the current package version string) and the `tools` list matches the 16 registered tools: plan, clarify, bootstrap, serve_local, hermes_config, queue_next, queue_record, queue_brief, context_compress, guard, web_search, the 4 browser tools, and doctor. (Use the actual tool names as registered in code.)
Invariant: plugin.yaml version == package version; tools list == the registered tool set.

## Done criteria
- `uv run pytest tests/runtime/` GREEN. `uv run ruff check .` pass.
- New tests for Defect 1 (version atomic matching, normalization preserved) and Defect 4 (off-schema action message).
- No version bump in pyproject. No edits under `.grok-briefs/`. Provide a concise per-defect diff summary.
