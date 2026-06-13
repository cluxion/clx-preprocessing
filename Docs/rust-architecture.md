# Rust Architecture

## 원칙

Preprocessing 플러그인은 **Rust가 메인**, **Python은 연결층**입니다.

```
Hermes / Claude / Codex / Grok
        ↓  (adapter: plugin, skill, config snippet)
cluxion_agentplugin_preprocessing  (Python: register, CLI, schemas)
        ↓  subprocess JSON / in-process
cluxion_runtime                    (Python: plan, preprocess, intent)
        ↓  optional
cluxion-queue                      (Rust: queue + dispatch)
```

## Rust: `cluxion-queue`

| 명령 | 역할 |
|------|------|
| `enqueue` / `dequeue` / `peek` | 우선순위 작업큐 (SQLite WAL) |
| `persist` / `next` / `record` / `brief` | segment dispatch (atomic JSON) |

빌드:

```bash
cargo build --release --manifest-path rust/cluxion_queue/Cargo.toml
```

## Python 역할

- Hermes `register()` 및 10-tool schema (`cluxion` toolset)
- `cluxion-runtime plan` CLI
- clarification, intent, answer_policy (결정론적 — AI 호출 없음)
- Rust 미설치 시 Python file-based dispatch fallback

## 범용 연동

동일 `cluxion-runtime` 바이너리를 `--surface hermes|claude|codex|grok_build`로 호출합니다.  
에이전트별 차이는 adapter manifest뿐이며 **core는 공유**합니다.