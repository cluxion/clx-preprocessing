# Install And Operations (legacy reference)

> 최신 문서: [`../Docs/installation.md`](../Docs/installation.md)

## 설치

```bash
pip install cluxion-agentplugin-preprocessing
cluxion-preprocess check
cluxion-preprocess enable    # Hermes
```

## 연결된 AI 연동

- Hermes: `cluxion_*` 도구 (plugin enable 후)
- Hermes 슬래시 (0.3.23+): `/loopauto`, `/clx-doctor` — `/` 자동완성
- Claude: `skills/clx-preprocess/SKILL.md`, `commands/clx-plan.md` (`--surface claude`)
- Codex: `skills/clx-preprocess/SKILL.md`, `commands/clx-plan.md` (`--surface codex`)
- 공통: `cluxion-runtime plan --surface <hermes|claude|codex|grok_build>` (host를 명시; 자동 감지 없음)

## Rust 큐 (선택)

```bash
cargo build --release --manifest-path rust/cluxion_queue/Cargo.toml
export CLUXION_QUEUE_BIN=/path/to/cluxion-queue
```

## 배포 전 점검

```bash
uv run ruff check .
uv run pytest
cluxion-preprocess check
cluxion-runtime plan --surface hermes --prompt "smoke"
```

모델 endpoint·provider 설정은 **host agent 문서**를 참고하세요. 전처리 플러그인은 모델을 소유하지 않습니다.
