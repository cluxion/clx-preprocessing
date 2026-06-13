========= Written in Korean first, then English ==========

======== 한국어 ========

# cluxion-agentplugin-preprocessing

AI 코딩 에이전트(Hermes Agent, Claude Code, Codex)를 위한 전처리 플러그인입니다. 작업이 시작되기
*전에* 정리를 해 줍니다: 에이전트가 모르는 것은 모른다고 인정하게 하고, 애매한 요청은 행동하기 전에
사용자에게 명확히 묻게 하며, 긴 작업을 안정적으로 큐에 넣고, 폭주하는 프로세스가 기기를 다운시키지
못하게 막고, 대화가 너무 길어지면 자동으로 압축합니다.

## 설치

```bash
pip install cluxion-agentplugin-preprocessing

# 선택: 사용자 본인의 로그인된 Chrome으로 웹 검색
pip install 'cluxion-agentplugin-preprocessing[browser]'
playwright install chromium
```

### Hermes Agent에서 사용

```bash
cluxion-preprocess enable     # ~/.hermes/config.yaml 에 플러그인을 추가합니다
# 그 다음 Hermes 재시작
```

Hermes를 통해 제공되는 로컬 모델(vLLM/MLX)에서도 동일하게 동작합니다.

## 기능

활성화하면 에이전트가 아래 도구들을 얻고, 자동으로 호출합니다.

- **정직함과 명확화** — 에이전트가 추측하기 전에 먼저 묻고, 근거를 댈 수 없는 답을 지어내지 않습니다.
- **작업 큐** — 긴 작업을 하나의 프롬프트에 넘치게 담는 대신 추적 가능한 세그먼트로 분할합니다.
- **자원 가드** — 폭주 프로세스가 RAM을 모두 잡아먹지 못하게 막는 가벼운 감시기. Hermes 세션마다
  자동으로 시작됩니다.
- **컨텍스트 압축** — 대화가 모델 컨텍스트의 약 70%를 넘으면, 의도와 최근 대화를 보존하며 압축합니다.
- **내 Chrome으로 웹 검색** — 본인의 로그인된 브라우저 세션으로 Google / Naver / Perplexity / 사내
  페이지를 검색합니다(위의 `[browser]` extra 필요).

## 문제 해결

| 증상 | 해결 |
|---|---|
| `playwright_not_installed` | `pip install 'cluxion-agentplugin-preprocessing[browser]' && playwright install chromium` |
| Hermes에 도구가 안 보임 | `cluxion-preprocess enable` 실행 후 Hermes 재시작 |

## 라이선스

Apache-2.0

============ English ==========

# cluxion-agentplugin-preprocessing

A preprocessing plugin for AI coding agents (Hermes Agent, Claude Code, Codex). It tidies
things up *before* work starts: it makes the agent admit when it doesn't know, asks you to
clarify vague requests before acting, queues long tasks reliably, keeps runaway processes
from taking down your machine, and auto-compresses the conversation when it grows too long.

## Install

```bash
pip install cluxion-agentplugin-preprocessing

# optional: web search through your own logged-in Chrome
pip install 'cluxion-agentplugin-preprocessing[browser]'
playwright install chromium
```

### Use with Hermes Agent

```bash
cluxion-preprocess enable     # adds the plugin to ~/.hermes/config.yaml
# then restart Hermes
```

The tools also work with local models (vLLM/MLX) served through Hermes.

## What you get

Once enabled, your agent gains these tools and calls them automatically:

- **Honesty & clarification** — the agent asks before guessing and won't fake an answer it
  can't back up.
- **Work queue** — long tasks are split into tracked segments instead of overflowing a
  single prompt.
- **Resource guard** — a lightweight watcher that stops runaway processes from eating all
  your RAM. It starts automatically with each Hermes session.
- **Context compression** — shrinks the conversation once it passes ~70% of the model's
  window, keeping your intent and recent turns.
- **Web search via your Chrome** — searches Google / Naver / Perplexity / internal pages
  through your own logged-in browser session (needs the `[browser]` extra above).

## Troubleshooting

| Problem | Fix |
|---|---|
| `playwright_not_installed` | `pip install 'cluxion-agentplugin-preprocessing[browser]' && playwright install chromium` |
| Tools don't appear in Hermes | run `cluxion-preprocess enable`, then restart Hermes |

## License

Apache-2.0
