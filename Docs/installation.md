# Installation

## 요구 사항

- Python 3.11+
- (선택) Rust toolchain — `cluxion-queue` 빌드용

## 설치

```bash
pip install cluxion-agentplugin-preprocessing
cluxion-preprocess check
```

개발:

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## Hermes Agent

```bash
cluxion-preprocess enable
cluxion-preprocess status
hermes tools list   # cluxion toolset 확인
```

레거시 entry point `hermes-cluxion`도 호환됩니다.

### 슬래시 커맨드 (0.3.23+)

Hermes 세션에서 `/` 입력 → `/loopauto`, `/cluxion-doctor` 🔌 자동완성.

```
/loopauto 긴 작업을 순서대로 처리하고 증거를 남겨줘
/cluxion-doctor
```

전체 5종 플러그인 슬래시 표: `cluxion-plugins-guide.md` §2-A.

## 연결된 AI 연동

- Hermes: `cluxion_*` 도구 자동 등록
- Claude: `adapters/claude/skills/preprocess/SKILL.md`
- Codex: `adapters/codex/config-snippet.toml`
- 공통 CLI: `cluxion-runtime plan --surface <hermes|claude|codex|grok_build>`

## CLI

| 명령 | 설명 |
|------|------|
| `cluxion-preprocess check` | runtime·Rust 큐 가용성 |
| `cluxion-preprocess enable` / `disable` | Hermes config |
| `cluxion-runtime plan` | harness plan JSON |
| `cluxion-runtime queue-next` | 다음 segment |

## Rust work queue (선택)

```bash
cargo build --release --manifest-path rust/cluxion_queue/Cargo.toml
export CLUXION_QUEUE_BIN=/path/to/cluxion-queue
```

바이너리가 없어도 Python fallback으로 동작합니다.

`cluxion_guard` daemon도 동일: `cluxion-queue` 바이너리가 없으면
`python -m cluxion_runtime.guard_daemon_host`로 시작됩니다 (wheel 설치 시 기본 제공).