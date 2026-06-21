# Task: preprocessing — fix forget(hybrid_forget) + LLM Stage2 PATH dependency (bins silently unresolved in the hermes runtime)

## Context (LIVE-VERIFIED user-facing defect)
0.3.19 installed. The hermes launcher `~/.local/bin/hermes` is a bash wrapper that does `exec <venv>/bin/hermes "$@"` WITHOUT adding `<venv>/bin` to PATH. The `forgetforge` binary is installed at `<venv>/bin/forgetforge`, which is NOT on the user's shell PATH (verified: `which forgetforge` fails). Therefore in the real hermes runtime:
- `core/hybrid_forget.py:21` `_FORGETFORGE_BIN = "forgetforge"` and `:78` `forgetforge_available() = shutil.which(_FORGETFORGE_BIN) is not None` → returns **False** → the ENTIRE forget(망각) feature is silently SKIPPED. The user's intended "forget unimportant context, keep important" never runs. (Reproduced: with `<venv>/bin` absent from PATH, `forgetforge_available()` is False; with it on PATH, True. The plugin compress pipeline's forget stage is dropped.)
- `core/llm_compress.py:22` `_HERMES_BIN = "hermes"` and `:57` which → same PATH dependence for LLM Stage2 summarization (hermes -z). It happens to resolve today via `~/.local/bin/hermes`, but is fragile.

This is a real, user-facing correctness bug: a core feature does not run depending on PATH. Do NOT bump the version. Do NOT touch `.grok-briefs/`.

## Fix — resolve bins by ABSOLUTE path relative to the running interpreter (same venv), with PATH fallback
The plugin always runs under `<venv>/bin/python`, so `os.path.dirname(sys.executable)` is exactly the directory where `forgetforge` / `hermes` console scripts live. Add a small resolver and use it in BOTH files.

```python
import os, sys, shutil

def _resolve_bin(name: str) -> str:
    candidate = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which(name) or name
```

### hybrid_forget.py
- `_FORGETFORGE_BIN = _resolve_bin("forgetforge")`
- Make availability robust (absolute file OR PATH):
```python
def forgetforge_available() -> bool:
    return (os.path.isfile(_FORGETFORGE_BIN) and os.access(_FORGETFORGE_BIN, os.X_OK)) or shutil.which(_FORGETFORGE_BIN) is not None
```
- All existing subprocess calls already use `_FORGETFORGE_BIN`; they now get the absolute path. Keep cold-demote/store behavior identical.

### llm_compress.py
- `_HERMES_BIN = _resolve_bin("hermes")` and make its availability check robust the same way. Prefer `<venv>/bin/hermes`, fall back to PATH. No change to the hermes -z invocation args or the anti-hallucination guard.

## Invariant + tests (MANDATORY)
- forget works when `forgetforge` exists in `<venv>/bin` EVEN IF it is not on PATH. Add a test that monkeypatches `os.environ['PATH']` to exclude the venv bin dir and asserts `forgetforge_available()` is still True (resolved via `sys.executable` dir) when the binary exists there; and False when the binary exists nowhere (monkeypatch `_resolve_bin`/`sys.executable` to a temp dir without it).
- No behavior change when forgetforge IS on PATH (existing tests stay green).
- cold-demote still stores at the recoverable cold tier and returns True.
- Same kind of test for `_HERMES_BIN` resolution (absolute preferred, PATH fallback).
- `uv run pytest tests/runtime/` GREEN; `uv run ruff check .` pass.

## Done
- forget and LLM Stage2 resolve their bins by absolute venv path (PATH-independent), so neither is silently skipped in the hermes runtime. Tests added proving PATH-independence. No version bump. No `.grok-briefs/` edits. Concise diff.
