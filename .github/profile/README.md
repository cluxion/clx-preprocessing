# hermes-cluxion

`hermes-cluxion` is a lightweight Cluxion harness plugin for Hermes Agent.

It keeps Hermes in control of OAuth, provider auth, active model selection, tools, permissions, and final AI completion calls. Cluxion adds deterministic work planning around Hermes: preprocessing, intent routing, honesty contracts, queued segment dispatch, resource admission, and optional local `vllm-mlx` endpoint setup.

## Install

```bash
python -m pip install "hermes-cluxion==0.1.9"
hermes-cluxion enable
hermes-cluxion check
```

Confirm the plugin is enabled:

```bash
hermes tools list
```

Expected:

```text
Plugin toolsets (cli):
  ✓ enabled  cluxion  🔌 Cluxion
```

## Hermes Tools

- `cluxion_plan`
- `cluxion_bootstrap`
- `cluxion_serve_local`
- `cluxion_hermes_config`
- `cluxion_queue_next`
- `cluxion_queue_record`
- `cluxion_queue_brief`

## Execution Model

- Cloud AI calls stay inside Hermes.
- Local model calls also stay inside Hermes after switching to a custom local provider.
- Cluxion returns `host_execution`, `answer_policy`, queue metadata, and resource decisions.
- Short simple prompts do not pay queue or resource snapshot cost.
- Short verification prompts get required checks without heavy preprocessing.
- Long work uses durable queued segment dispatch and final briefing synthesis.

## Local MLX Flow

```bash
hermes-cluxion bootstrap --upgrade
hermes-cluxion hermes-config \
  --model mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit \
  --port 23003 \
  --context-length 131072
cluxion-runtime serve-local \
  --model mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit \
  --port 23003 \
  --upgrade-runtime
```

Then switch Hermes:

```text
/model custom:cluxion-local:mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit
```

Full guide: [README](../../README.md)
