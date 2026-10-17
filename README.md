# KTB4-12th-AI

PR15가 병합된 develop 기준으로 Chat·Profiler·Search가 공용 상품 API와 하나의 PostgreSQL 카탈로그를 사용하는 통합 후보입니다. Search가 상품·임베딩·BE 동기화를 맡고, Profiler가 취향 해석·비선호·재랭킹·개인 프로필을 소유합니다. Chat은 기존 AI–BE HTTP/SSE 계약과 최대 3개 상품 카드를 유지합니다.

- [팀 실행 quickstart](integration/QUICKSTART.md)
- [설정·운영·마이그레이션](integration/SINGLE_SERVER_GUIDE.md)
- [업그레이드·백업·복구](integration/RECOVERY.md)
- [릴리스 범위·미완료 계약·팀 확인 사항](integration/RELEASE_READINESS.md)
- [에멧 PR15 변경 근거](integration/EMMET_PR15_CHANGES.md)
- [Chat 외부 계약](chat/INTEGRATION.md) · [Search API](product-search/INTEGRATION.md) · [Profiler 원래 가이드](profiling/README.md)
- [기존 Search 실행 가이드](product-search/README.md) · [Dylan workbench](workbench/dylan/README.md)

통합 실행의 기준은 `integration/containers/Dockerfile`과 `compose.guarded.yaml`입니다. 합성 시험은 별도 `compose.smoke.yaml`을 사용합니다. 각 서비스의 기존 단독 실행 자료는 upstream 호환을 위해 보존하며 통합 배포 경로로 사용하지 않습니다. 앱 시작 시 DDL·계정 생성·과거 migration 재실행을 하지 않습니다.

실제 BE 완전 export·프로필 grant·사전 계산 파일은 아직 연결되지 않았습니다. BE400의 하루 10턴·취향 수정 3크레딧은 BE 소유의 미구현 계획으로 AI에 추가하지 않았습니다. 이 통합 후보의 push·PR 게시·merge·배포는 실행하지 않았습니다.
