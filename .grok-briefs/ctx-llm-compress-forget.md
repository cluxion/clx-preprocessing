# Task: LLM context compression + hybrid forgetting (preprocessing core)

## Context
`core/context_compress.py` `compress()` runs deterministic stages (truncate→dedup→digest) under a
70% trigger / 30% target, then STOPS by returning `ai_summary_request` (a request payload) WITHOUT
calling any LLM. No forgetting exists. There is NO LLM-call infra and NO forgetforge link today.
User decisions (confirmed): use the MAIN model via `hermes -z` for summarization; forgetting is
HYBRID (forgetforge cold-demote for recoverable, permanent delete for true junk).
GOAL: after compress(), context usage MUST be brought to/under the target so the host model never
exceeds 70% (hallucination guard). PRESERVE USER INTENT/DIRECTION above all else.

## Design — extend compress() into Stage1(det) → Stage2(LLM) → Stage3(forget)
Keep Stage1 (truncate/dedup/digest), the 70% trigger, and the pinned logic UNCHANGED.

### Stage 2 — LLM summarization (NEW; runs when Stage1 leaves total > target)
- Reuse the existing `_build_summary_request` to pick targets (non-pinned, largest first).
- New module `core/llm_compress.py`, e.g. `summarize_messages(messages, indices, instructions, *, model=None, timeout_s=...) -> dict[int,str]`:
  - Build ONE prompt with the target messages clearly delimited + indexed, plus instructions:
    "Summarize each message by importance. PRESERVE ABOVE ALL: the user's intent and direction,
    decisions made, unresolved items, file paths / identifiers / commands. Compress everything else.
    Each summary < 10% of the original. Output STRICT JSON: {\"<index>\": \"<summary>\", ...} only."
  - Call the main model via `hermes -z <prompt>` as a subprocess. If `cluxion_hermes_call.core` is
    importable, reuse its hermes-invocation/oneshot helper; else a minimal subprocess wrapper
    (`hermes -z`, optional `-m <model>`). Honor a timeout. Capture stdout.
  - Parse the JSON; replace each target message's content with its summary; recompute tokens via
    `estimate_tokens`.
  - FAIL-SAFE: on LLM error/timeout/unparseable JSON, DO NOT crash and DO NOT lose messages — keep
    Stage1 output and return the existing `ai_summary_request` (current behavior) instead.
- pinned messages (first user = intent, recent turns) are NEVER summarized.

### Stage 3 — hybrid forgetting (NEW; runs when Stage2 still leaves total > target)
- Score remaining NON-pinned messages by importance, lowest first: older position = lower;
  digest/duplicate markers = lower; lack of decision/identifier/path keywords = lower. NEVER score
  or touch pinned/intent messages.
- Remove lowest-importance until total ≤ target. For each:
  - RECOVERABLE (still plausibly useful content): cold-demote — persist it via forgetforge so it is
    recoverable, then drop from context. Use the forgetforge CLI if available (e.g.
    `forgetforge store <id> --content ... ` then mark cold, per forgetforge's documented API/tiers);
    if forgetforge is NOT installed, fall back to permanent removal but set a `dropped_without_backup`
    flag in the result.
  - TRUE JUNK (dedup/digest leftovers, pure noise): remove permanently.
- After Stage3, total MUST be ≤ target. If it cannot (all remaining are pinned), return
  `over_target_pinned_only: true` so the host knows pinned content alone exceeds target.

## Invariants (MUST hold)
- usage_after ≤ target_ratio whenever possible; NEVER silently exceed 70% — report if forced.
- User intent (first user message), decisions, identifiers/paths preserved through ALL stages.
- Fail-safe: any LLM or forgetforge failure degrades gracefully — never lose messages, never crash.
- Stage1 determinism preserved (Rust parity for Stage1 only). Stage2/3 are Python-only (LLM/IO);
  document that the Rust mirror does NOT replicate LLM calls (intentional divergence).
- Backward compatible: existing compress() callers/tests pass. New behavior is additive and gated by
  payload flags (e.g. `enable_llm_summary`, `enable_forget`, default ON but auto-degrade if no LLM).

## Tests (must pass; add coverage)
- Stage2: monkeypatch the hermes -z call to return canned JSON; assert targets summarized, pinned
  untouched, tokens drop; assert JSON-parse-failure falls back to ai_summary_request with NO message loss.
- Stage3: assert lowest-importance demoted/removed until ≤ target; intent/pinned preserved; with
  forgetforge absent, falls back to removal + `dropped_without_backup` flag.
- End-to-end: a payload exceeding 70% ends ≤ target (or `over_target_pinned_only`).
- `uv run pytest tests/runtime/` green; existing context-compress parity tests stay green.

## Out of scope (DO NOT)
- No version bump / build / publish. No auto-trigger hook wiring (separate task — this is the
  compress() pipeline logic only). No change to Stage1 determinism or the 70% trigger constants.

## Done
compress() performs real LLM summarization (hermes -z) then hybrid forgetting, bringing usage to/under
target with user intent preserved and fail-safe degradation on any LLM/forgetforge failure; tests green.
Report a concise diff summary.
