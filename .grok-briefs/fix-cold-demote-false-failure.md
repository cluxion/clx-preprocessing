# Task: Fix _cold_demote — remove invalid 'forgetforge cold' call that falsely reports backup failure

## Context (LIVE-VERIFIED cross-plugin bug)
preprocessing's `core/hybrid_forget.py::_cold_demote` runs TWO forgetforge commands:
`forgetforge store <id> --content <text>` then `forgetforge cold <id>`. BUT forgetforge has NO `cold`
subcommand (verified `forgetforge --help`: check/init/status/recall/keep/forget/unforget/
list-forgotten/prune/store/pruner-daemon/import-brief/hot-context/doctor). So `forgetforge cold <id>`
exits 2 → `CalledProcessError` → `_cold_demote` returns False — EVEN THOUGH the preceding `store`
already SUCCEEDED and persisted the memory at tier=cold (verified: forgetforge `store`'s default tier
IS cold). Consequence: a SUCCESSFUL cold-demote is wrongly reported as backup failure, so
`apply_hybrid_forget` sets `dropped_without_backup=True` incorrectly and the user is wrongly warned the
context was dropped without recovery — when it IS recoverable in forgetforge's cold tier
(recall / list-forgotten / unforget). This breaks the user's intended "recoverable → cold-demote" semantics.

## Fix (core/hybrid_forget.py `_cold_demote`)
- REMOVE the second subprocess call (`forgetforge cold <id>`) entirely. `forgetforge store` already
  persists the memory at the cold tier by default (store → tier=cold, verified), so the content is
  recoverable. Return True when the `store` call succeeds.
- KEEP existing guards: `forgetforge_available()` check; try/except for
  OSError/TimeoutExpired/CalledProcessError → return False; the timeout on the subprocess.
- Optionally pass `--importance` low if that better reflects demotion, but do NOT call a nonexistent
  subcommand. The minimal correct fix is: store only, return True on success.

## Invariants (MUST hold)
- _cold_demote returns True when `store` succeeds (memory recoverable at cold tier).
- forgetforge absent / store fails → returns False (→ dropped_without_backup, which is then CORRECT).
- No change to hybrid_forget scoring/removal logic or the forget/unforget DB layer.

## Tests (must pass; add coverage)
- Monkeypatch subprocess.run: a successful `store` → _cold_demote returns True AND no `cold` subcommand
  is invoked (assert the args of the (single) call are the `store` command).
- store raising CalledProcessError → _cold_demote returns False.
- forgetforge_available() False → returns False without calling subprocess.
- `uv run pytest tests/runtime/` green.

## Out of scope
- No version bump. No change to forgetforge. No change to apply_hybrid_forget beyond consuming the
  now-correct _cold_demote return.

## Done
_cold_demote stops calling the nonexistent `cold` subcommand and returns True on successful store
(cold-tier, recoverable), so dropped_without_backup is accurate; tests green. Concise diff summary.
