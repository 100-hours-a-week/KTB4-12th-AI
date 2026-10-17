# AI 통합 실행

현재 실행·검증은 [QUICKSTART](QUICKSTART.md), 데이터 경계와 설정은 [SINGLE_SERVER_GUIDE](SINGLE_SERVER_GUIDE.md)를 따릅니다. DB 변경은 [RECOVERY](RECOVERY.md)의 명시적 migration 경로만 사용합니다.

`containers/compose.guarded.yaml`은 실제 서비스 후보, `containers/compose.sync.yaml`은 같은 Search 코드의 별도 동기화 작업, `containers/compose.smoke.yaml`은 무료 합성 검증 전용입니다. Console·fake BE는 합성 경로에서만 실행합니다. 기존 `run_native.py`, `catalog_transition.py`, `launch_live_validation.py` 등은 과거 형식의 호환·회귀 도구이며 현재 배포 entrypoint가 아닙니다.

[변경 근거](EMMET_PR15_CHANGES.md) · [BE export 초안](BE_EXPORT_CONTRACT_DRAFT.md) · [사전 계산 파일 호환성](PRECOMPUTED_CATALOG_COMPATIBILITY.md) · [릴리스 확인 사항](RELEASE_READINESS.md)
