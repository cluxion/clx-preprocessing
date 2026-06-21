# Task: Auto-trigger context compression (llm_request middleware) + Korean intent keywords

## Part 1 — Auto-trigger via llm_request middleware (the 70% guard automation)
Context: `compress()` now does LLM summarization + hybrid forgetting, but it only fires when the host
AI calls the `cluxion_context_compress` tool. The user wants it AUTOMATIC: whenever outbound context
exceeds 70%, compress BEFORE the LLM call. Investigation confirmed the ONLY hook that can SHRINK the
message array is `register_middleware("llm_request", ...)` — observer hooks (pre_llm_call) can only
ADD context. `apply_llm_request_middleware` passes `api_kwargs` (with the full `messages` array) and
a middleware may return `{"request": {...}}` to replace it.

Implement in `plugin.py register()`:
- Register an `llm_request` middleware IF the host supports it (older hermes lacks it → skip silently;
  the tool path still works):
  ```
  register_mw = getattr(ctx, "register_middleware", None)
  if callable(register_mw):
      register_mw("llm_request", _auto_compress_middleware)
  ```
- `_auto_compress_middleware(request, original_request=None, model=None, **kw)`:
  - Extract messages: `request.get("messages")` (or `"input"` for codex_responses). Absent → return None.
  - Cheaply estimate tokens (estimate_tokens over contents); resolve context limit from `model` via the
    existing MODEL_CONTEXT / `_resolve_context_limit`.
  - usage < 0.70 → return None (NO-OP; runs on EVERY llm call so this path must be cheap).
  - usage ≥ 0.70 → call `runner.context_compress`/`compress()` (Stage1→2→3) to bring to target; return
    `{"request": {**request, "messages": <shrunk>}, "source": "preprocessing"}`.
  - FAIL-SAFE: ANY exception → return None (original request unchanged). Never emit a malformed array.
- **RECURSION GUARD (critical):** Stage2 calls `hermes -z`, spawning a new hermes that ALSO loads this
  plugin+middleware. Prevent infinite recursion: in `llm_compress`, set an env var
  `CLUXION_PREPROCESS_IN_COMPRESS=1` around the `hermes -z` subprocess; and `_auto_compress_middleware`
  must return None immediately if that env var is set. Also gate on `CLUXION_PREPROCESS_AUTOCOMPRESS`
  (default on) so users can disable.

## Part 2 — Korean intent keywords (hybrid_forget + LLM instructions)
The user works in Korean. `core/hybrid_forget.py:_IMPORTANCE_KEYWORDS` is English-only, so Korean
decisions/intent score low and risk being forgotten in Stage 3. Add Korean importance keywords next to
the English ones: 결정, 의도, 방향, 수정, 오류, 구현, 경로, 파일, 명령, 필수, 반드시, 미해결, 할일,
변경, 핵심, 중요. Also update `core/llm_compress.py:_SUMMARY_INSTRUCTIONS` to say preserve intent
"regardless of language (Korean or English)".

## Invariants (MUST hold)
- 70% guard: usage never silently exceeds 70% when autocompress is on.
- Recursion impossible (env guard). Middleware no-op path is cheap. Fail-safe: exception → None.
- compress() pipeline behavior UNCHANGED; this only adds the auto-trigger + keyword coverage.
- pinned/intent preserved; Korean keywords now let non-pinned Korean decisions survive Stage3.

## Tests (must pass)
- Middleware: usage<70%→None; usage≥70%→returns shrunk request (fewer tokens); recursion env set→None;
  exception in compress→None (original unchanged); missing register_middleware→skip cleanly.
- Korean: a Korean decision message scores above junk and survives Stage3.
- `uv run pytest tests/runtime/` green (existing tests stay green).

## Out of scope
- No version bump / build / publish. No change to compress() Stage logic beyond the keyword list +
  the instruction string.

## Done
context compression auto-fires via llm_request middleware above 70% (recursion-safe, fail-safe,
env-gated); Korean intent keywords added; tests green. Concise diff summary.
