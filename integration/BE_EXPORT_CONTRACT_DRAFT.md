# BE 상품 export — 로컬 계약 초안

**BE export는 아직 구현되지 않았고 이 문서는 BE와 합의되지 않았다. 실 BE 연결·호출·변경은 하지 않았다.** 아래는 현재 Search adapter의 동작을 명시한 검토 자료이며 운영 계약을 대신하지 않는다. [합성 fixture](contracts/be-export-draft.fixture.json)만 검증에 사용한다.

| 필드/상황 | 현재 후보의 처리 | 합의할 내용 |
| --- | --- | --- |
| productId/categoryId | 양의 JS-safe 정수, 문자열·bool 거부. 상품 ID 오름차순·유일 | 실제 번호·정렬·전체 export의 범위 |
| name/brand/categoryName | 문자열, categoryName은 taxonomy 소분류 이름과 같아야 함 | 명칭 정본과 taxonomy 원자성 |
| description | 문자열 또는 null → 빈 문서 설명 | null과 빈 문자열 의미 |
| price/available | 비음수 정수·엄격 bool. 판매 불가는 행을 보존하고 unavailable로 표시 | 품절·삭제·노출 중단의 구분 |
| viewCount | 명시 0은 0. 누락/null은 기존 값 보존, 신규 상품은 0 | 누적/기간 조회수, null 의미, 신규 0의 적절성 |
| updatedAt/generatedAt | UTC 문자열. 상품 시각 ≤ export 시각. 이전 export보다 오래된 발행 거부 | 일관된 스냅샷 시각·동일 시각 재발행 |
| data.snapshot | UUID id, complete=true, productCount, SHA256 필수. products와 categories의 canonical digest 일치 | BE에서 보증할 원자적 전체 snapshot 식별자와 완전성 증거 |
| taxonomy | 중복 없는 상·하위 분류, 상품은 존재하는 소분류를 참조 | 부모 변경·분류 삭제·이름 변경 순서 |
| 전체 export에서 사라진 ID | 완전성 증거가 확인된 경우만 tombstone에 기록. 과거 세대·콜백에서도 제외하며 rollback으로 해제하지 않음 | 누락=삭제 범위, ID 재사용 금지, 삭제 복원 계약 |
| 상품 종류·이미지·source ID | 기존 출처 enrichment를 유지. 신규 상품은 임의 추론 없이 공란/null | BE 제공 여부·보완 데이터의 정본 |

개인 취향·리뷰·비선호 태그는 export에 포함하지 않는다. Search가 상품 투영·임베딩·발행을 맡고, Profiler가 인기·비선호·MMR 정책과 개인 프로필을 소유한다. 순수 벡터 API는 같은 모델·전처리·코사인 계약을 사용한다.

동기화는 `python -m integration.search_sync`의 별도 실행 작업이다. 요청 서버에서 BE export를 기다리지 않는다. PG에 동결 payload와 pending을 준비하고 실제 Search가 검증한 뒤 활성 포인터를 commit한다. 실패하면 기존 활성 세대를 유지하되 이미 확인한 삭제 tombstone은 유지한다. 파일 CURRENT는 재생성 가능한 캐시 표식이다. 큐/RabbitMQ는 추가하지 않는다. 조회수·가격·판매 상태만 바뀌면 문서 임베딩을 재사용하는 기존 의미를 유지한다. 상세 상태·장애 복구·실행 방법은 [현재 가이드](SINGLE_SERVER_GUIDE.md)를 따른다.

검증: `product-search/tests/test_export_contract.py`의 합성 fixture 테스트와 기존 sync 회귀. 실제 BE가 준비되면 위 의미를 합의한 비식별 fixture로 추가 검증해야 한다. 이 후보의 임시 서버 결과를 실 연동 완료로 해석하면 안 된다.
