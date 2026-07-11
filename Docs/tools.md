# Tools and Modes

연결된 AI가 호출하는 도구·모드입니다. 플러그인은 JSON 계약만 반환하고 completion은 host가 수행합니다.

## Hermes slash commands (0.3.23+)

| Slash | Maps to |
|---|---|
| `/loopauto <prompt>` | `cluxion_plan` with explicit `loop_auto=true` |
| `/clx-doctor` | `cluxion_doctor` / `cluxion-preprocess doctor` |

`/` 입력 시 🔌 자동완성. 상세: `cluxion-plugins-guide.md` §2-A.

## Plugin tools (`cluxion` toolset, 17 tools)

Registered tools (exhaustive):

| # | Tool | 용도 |
|---|------|------|
| 1 | `cluxion_plan` | 전처리·방향·큐·리소스 plan |
| 2 | `cluxion_clarify` | 명확화 전용 |
| 3 | `cluxion_bootstrap` | 로컬 runtime 의존성 설치 |
| 4 | `cluxion_serve_local` | vLLM-MLX 로컬 endpoint 준비 |
| 5 | `cluxion_hermes_config` | Hermes custom provider config 렌더 |
| 6 | `cluxion_queue_next` | 다음 segment payload |
| 7 | `cluxion_queue_record` | segment 처리 결과 기록 |
| 8 | `cluxion_queue_brief` | 최종 synthesis용 briefing |
| 9 | `cluxion_loop_auto` | 큐 자동 드레인 |
| 10 | `cluxion_context_compress` | 대화 컨텍스트 압축 |
| 11 | `cluxion_guard` | 실시간 리소스 guard |
| 12 | `cluxion_web_search` | 로그인된 Chrome으로 웹 검색 |
| 13 | `cluxion_browser_open` | Chrome에서 URL 열기 |
| 14 | `cluxion_browser_extract` | 현재 페이지 텍스트 추출 |
| 15 | `cluxion_browser_click` | 페이지 요소 클릭 |
| 16 | `cluxion_browser_type` | 입력 필드에 타이핑 |
| 17 | `cluxion_doctor` | 결정론적 헬스 체크 |

### `cluxion_plan`

작업을 harness plan으로 변환합니다.

입력: `prompt`, `priority`, `cwd`, `metadata` 등

출력:

- `preprocessing.mode`
- `preprocessing.answer_policy`
- `host_execution.strategy`
- `clarification.required` / `clarification.questions`
- `queue_backend`: `rust` | `python`

### `cluxion_clarify`

명확화 전용. `cluxion_plan`과 동일한 평가 경로.

### Queue tools

| Tool | 설명 |
|------|------|
| `cluxion_queue_next` | 다음 segment payload |
| `cluxion_queue_record` | segment 처리 결과 기록 |
| `cluxion_queue_brief` | 최종 synthesis용 briefing |
| `cluxion_loop_auto` | 큐 자동 드레인 (`hermes -z` per segment). 명시적 opt-in 전용 |

`/loopAuto` 프롬프트 지시어는 prefix를 제거하고 `loop_auto=true`를 설정합니다.
자동 드레인은 큐 대상(`host_execution.queue_required=true`)일 때만 실행되며, 짧은 fast-path prompt는 queue를 강제하지 않습니다.

### `cluxion_bootstrap`

로컬 runtime 의존성 (기본 `vllm-mlx`) 설치 또는 업그레이드.

입력: `upgrade`, `dry_run`, `packages`

### `cluxion_serve_local`

Cluxion-managed vLLM-MLX 로컬 모델 endpoint 준비. `start=false`면 명령만 반환.

입력: `model` (필수), `host`, `port`, `max_tokens`, `auto_install`, `upgrade_runtime`, `start`

### `cluxion_hermes_config`

로컬 OpenAI-compatible endpoint용 Hermes custom provider config 렌더.

입력: `model` (필수), `host`, `port`, `context_length`, `provider_key`, `display_name`

### `cluxion_context_compress`

context 사용률이 trigger ratio(기본 70%)를 넘으면 target ratio(기본 30%)까지 압축.
결정론적 단계(truncate, dedup, digest) 후에도 target에 못 미치면 `ai_summary_request`를 반환.

입력: `messages`, `model`, `context_limit_tokens`, `trigger_ratio`, `target_ratio`, `keep_recent_turns`

### `cluxion_guard`

실시간 리소스 guard. `action=status`는 RAM/swap/CPU/zombie 샘플과 daemon 상태 반환;
`owned_roots`로 프로세스 ownership scan (fail-closed). `owned_roots`/`protect`는 **루트 PID 정수 리스트**다 (파일 경로 아님). `action=start`/`stop`은 1s polling daemon (process scan every 5s) 제어.

Guard runtime state precedence: `CLUXION_GUARD_STORE_DIR` > plugin home default `~/.local/share/cluxion-agentplugin-preprocessing/queue`. `CLUXION_QUEUE_STORE_DIR` is for queue data only and does not move `guard_state.json`/`guard_heartbeat`.
`action=enforce`는 등록된 owned 프로세스만 대상 (기본 dry-run, `apply=true`로 실제 signal).
`action=auto-enforce`는 daemon rolling window의 sustained pressure에서만 enforce (daemon 없음/ stale 시 fail-closed).

Daemon 시작: `cluxion-queue guard-daemon` 바이너리가 없으면
`python -m cluxion_runtime.guard_daemon_host` fallback (PyPI wheel 사용자도 daemon 사용 가능).

입력: `action` (`status`|`start`|`stop`|`enforce`|`auto-enforce`), `owned_roots`, `interval_ms`, `window`,
`cpu_threshold`, `rss_threshold_mb`, `grace_seconds`, `apply`, `protect`, `sustained_cpu`, `ram_floor_mb`, `min_samples`

### `cluxion_web_search`

사용자 본인의 Chrome 세션(로그인·쿠키)으로 웹 검색.

입력: `query` (필수), `engine`, `max_links`, `max_chars`

### Browser tools

| Tool | 설명 |
|------|------|
| `cluxion_browser_open` | URL 열기 |
| `cluxion_browser_extract` | 현재 페이지 텍스트 추출 |
| `cluxion_browser_click` | CSS selector 클릭 |
| `cluxion_browser_type` | 입력 필드에 텍스트 입력 |

`[browser]` extra + Playwright Chromium 필요.

### `cluxion_doctor`

임베디드 결정론적 헬스 체크 (`cluxion-preprocess doctor`와 동일 경로).

입력: `verbose` (optional)

## `host_execution.strategy`

| Strategy | 연결된 AI 동작 |
|----------|----------------|
| `current_turn_direct_answer` | 즉시 답변 |
| `current_turn_verify_then_answer` | 검증 후 답변 |
| `single_host_task` | 단일 작업 |
| `durable_segment_queue` | segment 큐 순회 |
| `ask_user_before_queue` | 사용자 질문 후 진행 |

## Queued workflow

### 수동 (호스트 반복)

1. `cluxion_plan` — `mode=queued`
2. `cluxion_queue_next` — segment를 **연결된 AI**가 처리
3. `cluxion_queue_record` — 결과 저장
4. `cluxion_queue_brief` — 최종 보고

### 자동 loopAuto (0.3.22+)

1. `cluxion_plan` (또는 `/loopauto` 슬래시) — 큐 등록
2. `cluxion_loop_auto` / `loop_auto=true` / queue-eligible `/loopAuto` prefix — `queue_next` → `hermes -z` → `queue_record` 반복 → `queue_brief`
3. 완료 마커: `SEGMENT_COMPLETE`, `WORK_REMAINS:`, `TASK_COMPLETE`

segment checksum은 synthesis 시 보존합니다 (`required_checks`).
