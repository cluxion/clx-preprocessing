# Task: Suppress LLM Stage2 summarization hallucination (anti-hallucination instruction + deterministic post-summary identifier guard)

## Context (workflow-verified across MULTIPLE live hermes -z runs)
A 5-agent adversarial audit of `summarize_messages` (Stage2, real `hermes -z`) on code / conversation /
Korean / mixed inputs found: intent, decisions, and identifiers are FAITHFULLY preserved, BUT the LLM
is NON-DETERMINISTIC and OCCASIONALLY appends an invented trailing identifier — observed repeatedly as
`Hot: Redis:6390` / `Hot: Redis 6390`, where `6390` appears NOWHERE in the source (grep-verified). This
is a real hallucination: context compression must NEVER inject a fact/identifier/number not in the
original (the user's core concern: "exceeding context / wrong info = hallucination"). IMPORTANT:
normalizations are NOT hallucinations and MUST be kept (e.g. `482k` ↔ `482,000`, `14mo` ↔ `14 months`,
`72h` ↔ `72-hour`).

## Fix (core/llm_compress.py — two layers)
### 1. Strengthen `_SUMMARY_INSTRUCTIONS`
Add an explicit anti-hallucination clause while keeping existing preservation + <10% + STRICT JSON rules:
"ONLY summarize content actually present in the message. NEVER invent, add, infer, or fabricate any
identifier, number, name, port, path, or fact that is not in the original. If unsure whether something
is in the source, OMIT it."

### 2. Deterministic post-summary identifier guard (runs on the LLM output before returning)
- Extract candidate "hard tokens" from each returned summary: standalone numbers and number-bearing
  identifiers (e.g. `6390`, `SEC-1190`, `5433`, `recon_v4.py`, `aurora-stg-3`).
- For each hard token, test presence in the ORIGINAL message text under NORMALIZATION: lowercase; strip
  commas/spaces; account for `k`/`m`/`만`/`억`/unit-word suffixes; substring match. If a hard token is
  NOT traceable to the source after normalization → it is a fabrication.
- Action: STRIP the fabricated token (and a trivially attached label such as a leading `Hot:` fragment)
  from the summary, preserving the rest (faithful content stays). If stripping would corrupt the summary
  structure, fall back to returning None (→ deterministic stages) — preserving the existing fail-safe.
- BE CONSERVATIVE about false positives: a token that matches the source after normalization (482k ↔
  482,000) MUST be kept. When in doubt, KEEP (prefer a benign keep over wrongly stripping a legitimate
  source value). Only strip tokens with NO normalized trace in the source.
- Record a `hallucination_stripped` count in the result/log for observability.

## Invariants (MUST hold)
- No fabricated identifier/number survives in the returned summary.
- All source-present identifiers and legitimate normalized values are preserved (NO over-stripping).
- Fail-safe preserved: any error in the guard → return None (deterministic fallback), never crash.
- Intent/decisions/identifiers preservation behavior otherwise unchanged.

## Tests (must pass; add coverage)
- Mock `summarize_messages`' LLM call to return a summary containing a fabricated `6390` not in source →
  guard strips `6390` (and a leading `Hot:` label if attached); source-present identifiers remain.
- Normalized value: source contains `482,000`, summary contains `482k` → KEPT (not stripped).
- Korean: source `일평균 482,000건`, summary `482k/day` → KEPT.
- All-fabricated edge → returns None (deterministic fallback). `uv run pytest tests/runtime/` green.

## Out of scope
- No version bump (redeploy handled separately). No change to deterministic Stage1 or the middleware.

## Done
LLM Stage2 summaries never contain fabricated identifiers/numbers (anti-hallucination instruction +
deterministic post-guard, conservative on normalization so legitimate values survive), fail-safe
preserved, tests green. Concise diff summary.
