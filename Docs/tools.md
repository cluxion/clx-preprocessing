# Tools and Modes

연결된 AI가 호출하는 도구·모드입니다. 플러그인은 JSON 계약만 반환하고 completion은 host가 수행합니다.

## Hermes slash commands (0.3.23+)

| Slash | Maps to |
|---|---|
| `/loopauto <prompt>` | `cluxion_plan` with `/loopAuto` + `loop_auto` |
| `/cluxion-doctor` | `cluxion_doctor` / `cluxion-preprocess doctor` |

`/` 입력 시 🔌 자동완성. 상세: `cluxion-plugins-guide.md` §2-A.

## Plugin tools (`cluxion` toolset, 16 tools)

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
| `cluxion_loop_auto` | 큐 자동 드레인 (`hermes -z` per segment). plan 기본 `loop_auto: true` |

`/loopauto` 슬래시·`/loopAuto` 프롬프트 지시어·`loop_auto: true` 파라미터는 동일 경로.
비활성: `CLUXION_LOOP_AUTO=0`, `loop_auto: false`, `loop_auto_dry_run: true`(시뮬레이션).

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
`owned_roots`로 프로세스 ownership scan (fail-closed). `action=start`/`stop`은 200ms Rust polling daemon 제어.
`action=enforce`는 등록된 owned 프로세스만 대상 (기본 dry-run, `apply=true`로 실제 signal).
`action=auto-enforce`는 daemon rolling window의 sustained pressure에서만 enforce (daemon 없음/ stale 시 fail-closed).

Daemon 시작: `cluxion-queue guard-daemon` 바이너리가 없으면
`python -m cluxion_runtime.guard_daemon_host` fallback (PyPI wheel 사용자도 daemon 사용 가능).

입력: `action` (`status`|`start`|`stop`|`enforce`|`auto-enforce`), `owned_roots`, `interval_ms`, `window`,
`cpu_threshold`, `rss_threshold_mb`, `grace_seconds`, `apply`, `protect`, `sustained_cpu`, `ram_floor_mb`, `min_samples`

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
2. `cluxion_loop_auto` / 내장 `loop_auto` — `queue_next` → `hermes -z` → `queue_record` 반복 → `queue_brief`
3. 완료 마커: `SEGMENT_COMPLETE`, `WORK_REMAINS:`, `TASK_COMPLETE`

segment checksum은 synthesis 시 보존합니다 (`required_checks`).