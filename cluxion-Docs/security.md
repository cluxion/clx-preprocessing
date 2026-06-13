# Security

> 공개 배포용 보안 원칙. 연동 설명은 [`../Docs/`](../Docs/) 참고.

## 원칙

- API 키·OAuth credential·secret 파일을 저장하지 않습니다.
- subprocess는 배열 인자만 사용 (`shell=True` 금지).
- Hermes config 변경 시 timestamp backup 후 atomic replace.
- 큐·dispatch 데이터는 **사용자 디스크 경로**에만 저장, 원격 전송 없음.
- host agent venv를 오염시키지 않도록 runtime 의존성은 격리 경로 사용 (코드 내부 정책).

## Host 경계

- 모델·provider·인증은 **연결된 AI / host agent** 소유.
- Cluxion은 plan·게이트 JSON만 반환.

## 배포 전 점검

```bash
uv run ruff check .
uv run pytest
uv run python -m build
uv run twine check dist/*
cluxion-preprocess check
cluxion-runtime plan --surface hermes --prompt "smoke"
```