# Legacy Docs (cluxion-Docs)

> **공개 문서 기준: [`../Docs/`](../Docs/)**  
> 이 폴더는 이전 `hermes-cluxion` 패키징 시기의 **레거시 참고**입니다.

## 현재 방향 (요약)

- **연결된 AI**가 `cluxion_plan`·큐 도구를 호출하고 JSON 계약을 따릅니다.
- **모델·OAuth·provider**는 host agent가 소유합니다. 플러그인은 plan·게이트만 반환합니다.
- 플러그인 내부에서 **별도 LLM을 호출하지 않습니다.**

사용자·에이전트 연동 설명은 **`Docs/README.md`** 를 따르세요.

## 레거시 목차 (기술 참고)

| 문서 | 내용 |
|------|------|
| [architecture.md](./architecture.md) | 패키지 경계 (레거시 명칭 포함) |
| [harness-logic.md](./harness-logic.md) | intent·전처리·큐·resource |
| [honesty-preprocessing.md](./honesty-preprocessing.md) | answer_policy 계약 |
| [install-and-operations.md](./install-and-operations.md) | 설치 요약 → `Docs/installation.md` |
| [security.md](./security.md) | 공개 배포 보안 원칙 |