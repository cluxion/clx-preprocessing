# Brief: Rebase cluxion-Agentplugin-preprocessing on the Codex plugin standard + fix loop_auto runaway

Repo: /Users/kimtaekyu/Documents/Develop_Fold/Secret_Project/MacBot/cluxion-Agentplugin-preprocessing
Work directly in this repo. Local commits allowed. NEVER push, publish to PyPI, or start background daemons.

## Diagnosed defects (verified by live audit, 2026-07-02)

1. **Not working at all in practice.** The plugin is wired Hermes-only, and even that wiring is broken:
   - `~/.hermes/config.yaml` `plugins.enabled` contains only `cluxion-agentplugin-supercoder`; this plugin is installed in the Hermes venv (0.3.24, entry point `hermes_agent.plugins` present) but NOT enabled, so `/loopauto` and `/cluxion-doctor` are never registered.
   - `adapters/codex/config-snippet.toml` references `[plugins.cluxion_preprocess] command = [...]` — that schema does not exist in Codex. Real Codex plugins are marketplace plugins (see below). So the Codex integration was fictional.
   - `adapters/claude/.claude-plugin/plugin.json` exists but is stale (version 0.2.0 vs dist 0.3.24) and is not installed anywhere.
2. **Infinite-loop / runaway behavior** in `src/cluxion_runtime/core/loop_auto.py` + `src/cluxion_runtime/cli.py`:
   - `cli.py:171-184`: `should_auto_loop_plan()` auto-starts `run_loop_auto()` inside the `plan` command when the prompt has a `/loopAuto` prefix or `loop_auto=True` — the caller gets no response until the 600 s deadline if segments fail.
   - `loop_auto.py:163-233`: the drain loop keeps iterating while the dispatch queue has `queued` items; the deadline is the ONLY brake. No no-progress detection.
   - `loop_auto.py:177-191`: segment retry loop; if hermes never emits the `WORK_REMAINS_PREFIX` marker, it burns retries + wall clock silently.
   - `loop_auto.py:336-337`: missing hermes binary surfaces late as RuntimeError inside the loop instead of failing fast up front.
3. **Silent-fail Hermes source patch**: `src/cluxion_agentplugin_preprocessing/hermes_deliver_patch.py` string-replaces Hermes source at `on_session_start`; when anchors mismatch (Hermes updated) or the Hermes root is not found, it degrades silently and deliver=agent features just don't exist.
4. `plugin.yaml` version 0.3.23 drifted from dist 0.3.24.

## New direction (user decision, canonical)

**The Codex plugin standard becomes the baseline packaging** (Codex is open source). The same artifact must install natively in Codex CLI and Claude Code; Hermes remains supported as an adapter.

The real Codex plugin format (verify live examples on this machine):
- Marketplace = git repo registered as `[marketplaces.<name>]` in `~/.codex/config.toml`; plugins enabled via `[plugins."<plugin>@<marketplace>"] enabled = true`.
- Plugin layout is Claude-plugin compatible: `.claude-plugin/plugin.json` (name/version/description, `commands`, `skills` dirs), `commands/*.md`, `skills/*/SKILL.md`, optional hooks/MCP.
- Live references: `~/.codex/plugins/cache/claude-plugins-official/*` and `~/.codex/plugins/cache/ponytail/ponytail/4.8.4/` (multi-surface plugin done right).

## Tasks (priority order)

### T1 — loop_auto safety (fix the runaway first)
- Add a hard iteration/segment cap (config, sane default e.g. 25) in addition to the deadline.
- No-progress abort: if a drain iteration ends with the identical queue state as the previous iteration, abort with a diagnostic error instead of spinning to deadline.
- Fail fast BEFORE entering the loop: hermes binary resolvable, dispatch store writable; otherwise return a structured error immediately.
- Marker-missing handling: if the completion marker never appears, fail the segment with a clear reason after the retry cap — never silently consume the deadline.
- `plan` must never block on loop_auto implicitly: auto-trigger requires an explicit opt-in flag (`--loop-auto` / payload `loop_auto=true`). A `/loopAuto` prompt prefix alone must NOT start a blocking drain inside `plan`; it should enqueue and return the contract JSON telling the host agent to drive the loop (or call the dedicated `loop-auto` command).
- Regression tests: no-progress abort, iteration cap, marker-missing fail-fast, missing-binary fail-fast. Tests must not spawn runaway processes (resource-preflight discipline).

### T2 — Codex-baseline packaging
- Restructure so the repo is a valid Codex/Claude marketplace plugin: root `.claude-plugin/plugin.json` (version synced with pyproject), `commands/`, `skills/` whose content instructs the host agent to call the `cluxion-runtime` CLI JSON contract (`cluxion-runtime plan --surface codex|claude --json-stdin`, queue-next/record/brief, doctor). Model the layout on the live ponytail/claude-plugins-official examples.
- Delete `adapters/codex/config-snippet.toml` (invented schema). Replace with real install docs: `codex plugin marketplace add <git-url-or-path>` + `codex plugin add`, and the Claude Code equivalent.
- Fold `adapters/claude/` into the canonical root plugin (one artifact, not per-surface forks); update stale versions.
- Keep Python package + `hermes_agent.plugins` entry point working (Hermes adapter). Surface detection stays via `--surface`.

### T3 — Hermes patch made explicit and honest
- Remove auto-apply of `hermes_deliver_patch` from `on_session_start`. It becomes explicit opt-in: `cluxion-preprocess hermes-patch apply`, with anchor verification before writing and an exact status report (applied / already / anchors-mismatch / no-hermes). Never silent partial.
- Core features must degrade gracefully without the patch: `/loopauto`-equivalent works through the plugin's own tools without the Hermes source patch wherever possible.

### T4 — Doctor covers the real failure modes
Add/extend probes for: (a) plugin present but missing from `~/.hermes/config.yaml plugins.enabled`, (b) hermes binary not resolvable, (c) deliver patch anchor mismatch/partial, (d) dispatch dir not writable, (e) plugin.json/pyproject/plugin.yaml version drift. Each probe: clear verdict + one-line fix instruction.

## Constraints
- English for all code, comments, docstrings, logs. No `# type: ignore`, no bare except, no stubs/mocks-as-implementation, no fake success paths.
- SRP file-size discipline (~200 lines/file) as in the existing codebase.
- Run the full test suite when done; report pass/fail honestly with the command output.
- Do not modify anything under `~/.hermes/`, `~/.codex/`, `~/.claude/` — the plugin repo only. Config changes for the user's machine go into docs/install instructions, not applied.
- Commit in logical units with English messages, no AI attribution.

## Done criteria
- All new + existing tests green locally.
- `cluxion-runtime plan --surface codex --json-stdin` returns promptly (< 2 s) with contract JSON for a `/loopAuto`-prefixed prompt (no blocking drain).
- Repo root validates as a Codex/Claude marketplace plugin (structure matches live examples).
- A written summary of changes + how each diagnosed defect was resolved.
