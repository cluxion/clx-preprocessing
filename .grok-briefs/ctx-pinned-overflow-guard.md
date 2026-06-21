# Task: Prevent 70% context overflow when all messages are pinned (hallucination guard reinforcement)

## Context (LIVE-VERIFIED defect)
The auto-compress middleware works for normal conversations (usage 1.25 → 0.30, verified). BUT an edge
case leaves context ABOVE 70% → hallucination: when the pinned messages (first user = intent + the most
recent `keep_recent` turns) are EACH large, `compress()` hits `over_target_pinned_only` and returns them
unchanged. Live result: 5 pinned messages each ~40k tokens = usage 0.80, the middleware returns the
request unchanged → 80% context reaches the model. The user explicitly stated exceeding 70% = hallucination,
so this MUST be guarded.

## Fix (core/context_compress.py — add a last-resort pinned-recent truncate stage)
- After the existing stages, if total STILL > target AND remaining content is effectively all pinned
  (the `over_target_pinned_only` condition), add a FINAL stage that truncates the pinned RECENT turns
  (head + tail excerpt, reuse the _stage_truncate head/tail constants) — oldest pinned-recent first —
  until total <= target.
- The FIRST user message (task intent) is NEVER truncated — absolute preservation.
- If even the intent message ALONE exceeds target (a single giant first message), still truncate
  everything else to minimize overflow and set a clear `forced_over_target: true` flag so the host knows
  70% is physically unavoidable in that rare case.
- Recompute usage_after; the result's `over_target_pinned_only` should only remain true in the
  lone-giant-intent case (now reflected by `forced_over_target`).
- No middleware change needed if compress() now handles the all-pinned case — but verify
  plugin.py `_auto_compress_middleware` returns the shrunk messages and the edge case ends <= target.

## Invariants (MUST hold)
- First user message (intent) preserved verbatim unless it ALONE exceeds target (then forced_over_target).
- Normal multi-message conversations behave EXACTLY as before — this only adds a last-resort path.
- usage_after <= target whenever physically possible.
- Stage1 determinism / Rust parity unaffected (this is a Python-side last-resort stage; document that the
  Rust mirror need not replicate it, consistent with the existing LLM/forget Python-only divergence).

## Tests (must pass)
- The live edge case: 5 pinned messages each ~40k tokens (usage 0.80) → after compress, usage <= target
  (or forced_over_target with intent preserved if intent alone is huge). Intent text preserved.
- Lone giant intent (single first message > target): forced_over_target=True, everything else truncated.
- The normal 26-message case still compresses to ~target. Existing tests green. `uv run pytest tests/runtime/`.

## Out of scope
- No version bump (redeploy handled separately). No change to normal-path behavior or the 70% trigger.

## Done
compress() truncates pinned-recent turns as a last resort so context never silently exceeds 70% (intent
always preserved; forced_over_target flag for the unavoidable lone-giant case); the live edge case now
ends at/under target; tests green. Concise diff summary.
