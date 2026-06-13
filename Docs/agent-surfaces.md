# Agent Surfaces

동일한 `cluxion-runtime` CLI를 thin adapter로 각 에이전트에 연결합니다.

## 공통 진입

```bash
cluxion-runtime plan --json-stdin --surface <surface>
```

stdin JSON 최소 필드: `{ "prompt": "..." }`

지원 surface: `hermes`, `claude`, `codex`, `grok_build`, `api`

## Hermes Agent

- PyPI entry point: `cluxion-agentplugin-preprocessing`
- `cluxion-preprocess enable`로 config 활성화
- toolset 이름: `cluxion`
- Tools (10): `cluxion_plan`, `cluxion_clarify`, `cluxion_bootstrap`, `cluxion_serve_local`, `cluxion_hermes_config`, `cluxion_queue_next`, `cluxion_queue_record`, `cluxion_queue_brief`, `cluxion_context_compress`, `cluxion_guard`

## Claude Code

`adapters/claude/` 참고:

- `.claude-plugin/plugin.json`
- `skills/preprocess/SKILL.md` — plan 호출 가이드

## Codex

`adapters/codex/config-snippet.toml` — config 조각 예시

## Grok Build

project agent config에 `cluxion-runtime plan --surface grok_build` 연동

## 원칙

- 각 surface에서 **host agent가 모델·인증을 소유**
- Cluxion은 JSON 계약만 반환
- 플러그인 활성화는 사용자 opt-in