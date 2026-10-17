# 에멧 ERD·사전 생성 파일 호환성 확인

2026-10-17 PR #15가 병합된 develop `bfce5219` 및 그 커밋의 `profiling/tools/catalog/load_catalog.py`, `embedding.py`, 마이그레이션 `0009`–`0011`을 대조했다. 실제 상품 파일은 내려받거나 다시 생성하지 않았다. 검증 데이터는 합성뿐이다.

## ERD와 단일 서버 후보

[에멧 DB ERD](https://github.com/100-hours-a-week/KTB4-12th-wiki/wiki/AI-프로파일링-v2-DB-ERD)는 `0011` 기준이며 주황색은 `0009`–`0011` 추가 항목이다. 상품의 `embedding vector(768)`·`tags jsonb`, 카탈로그의 embedding/tag 지문, 프로필의 `disliked_tags`, 길이 제한이 없어진 `prompt_version`을 소스와 대조했다. 해당 마이그레이션은 수정 없이 채택했고 전용 임시 DB fresh·0010 upgrade에서 실행했다.

[10월 15일 통합 안내](https://github.com/100-hours-a-week/KTB4-12th-wiki/wiki/AI-프로파일링-v2-통합-안내-2026-10-15)의 마지막 마이그레이션 `0008`, `dislikedTags=비선호 카테고리 이름` 설명은 최신 `0011`·비선호 태그 구현보다 오래됐다. 최신 코드와 ERD를 우선한다. PR의 저자 보고 부하 수치·실 BE 성공·quiet-period 제안은 이번 후보 검증 결과 또는 BE 변경 승인으로 취급하지 않았다.

통합 후보는 기존 `ai_catalog.products/catalog_versions`를 파괴하거나 ID를 다시 매기지 않는다. 새 Search 정본은 `ai_catalog.generations`와 `generation_products` 투영이다. Profiler는 Search API를 통해 상품·태그·벡터를 읽고 개인 데이터는 `ai_profile`에 유지한다. Emmet의 개인 `catalog_version_id`에는 실제 사용한 Search 세대 UUID를 기록한다. 두 카탈로그 저장 구조를 동시에 정본으로 운용하지 않는다.

## 파일을 받았을 때의 재사용 판정

| 확인 항목 | 에멧 현행 적재 코드 | 통합 후보 재사용 조건 |
|---|---|---|
| 묶음 형식 | `ai-catalog-bundle/v1`, `packageId`, `taxonomyVersion`, `sourceFileSha256`, categories/products | package 이름만으로 활성 세대와 같다고 보지 않는다. 완전 export 증거와 실제 상품 projection hash를 연결해야 한다 |
| 상품 번호 | 새 manifest는 `source_product_ids` 존재 시 `product_ids`를 BE ID로 해석. 옛 형식은 수집처 ID를 매핑 | 양의 정수·중복 없음·현재 상품과 정확한 대응·행 순서 확인. 다른 ID 체계나 90% 부분 일치를 완전 대응으로 인정하지 않는다 |
| 카테고리 | BE ID, 대분류/소분류 부모 관계 | 같은 ID·계층·분류 판을 확인하며 이름만으로 추정 연결하지 않는다 |
| 벡터 바이트 | `manifest.json` + `vectors.f32`; vectors SHA·차원 검사 | float32, 행 수×768, 유한/비영 벡터, SHA, 정확한 행 ID 대응 |
| 모델 | model 이름·model SHA를 저장 | Jina v5 nano retrieval, revision `ac5d898c8d382b17167c33e5c8af644a3519b47d`, q4f16·768·실제 weight SHA·encoder code/tokenizer 지문 모두 확인 |
| 전처리 | 배포 코드의 Query encoder와 문서 생성 코드에 의존 | `Query: ` / `Document: `, 1024 token, 문서 구성, L2·cosine 의미 일치. 모델 이름·차원만 같으면 재사용 불가 |
| 상품 태그 | 현행 bundle builder에는 tags가 없다. `--tags`가 이미지의 `vocab`로 계산 | upstream `vocab.RULE`와 RULE+TSV hash, 카탈로그 브랜드 집합 hash, 상품 입력과 태그 내용 일치. 코드 revision은 별도 source manifest로 추적한다. 공지의 별도 태그 파일 형식은 실물 확인 전 추정하지 않는다 |
| 삭제·재고 | bundle 단독으로 새 완전 export의 삭제 증거가 되지 않음 | 삭제 tombstone 및 export receipt를 보존. 옛 bundle로 삭제를 되살리지 않는다 |

기존 manifest에 revision·전처리·문서 지문이 없으면 출처의 pinned 생성 코드와 원본 manifest로 보완해야 한다. 임의로 현재 값을 채워 호환으로 표시하지 않는다. 이 후보의 `Generation`·`encoder_contract`·`product_tags.validate`가 검증을 강제한다. 검증된 같은 세대의 문서가 같으면 기존 벡터를 재사용하고 바뀐 문서만 인코딩한다. 태그 규칙·TSV·브랜드 집합이나 검증한 태그 내용이 달라지면 태그를 무료 재계산할 수 있으며, 문서·인코더가 같다는 증거가 있으면 벡터까지 다시 만들 필요는 없다. 동결 후보의 코드+TSV 지문을 새 upstream 지문으로 이름만 바꾸어 재사용하지 않는다. 외부 묶음 자동 import는 아직 구현했다고 주장하지 않는다.

## Drive 접근 결과와 남은 입력

이전 후보 준비 때 공식 연결 Drive 읽기 검색은 성공했다. 이번 통합에서는 외부 파일을 새로 읽거나 쓰지 않았다. `catalog-bundle`, `manifest.json`, `vectors.f32`, `product-tags` 파일명 검색은 0건이었다. PR와 연결된 ERD·프로파일링·통합 안내에서 정확한 Drive 파일 링크를 찾지 못했다. 위키는 합본을 담당자에게 받도록 설명한다. 따라서 **파일 미식별·정확한 링크 대기** 상태이며 권한 거부라고 단정하지 않는다. 링크를 요청했고 인증 우회·공유 권한 변경·추정 file ID 접근은 하지 않았다.

필요한 것은 공식 Drive 링크와 파일 manifest다. 접근이 확인된 후에도 상품 본문 대신 먼저 파일명·크기·SHA·형식·모델/카탈로그 지문을 확인한다. 사전 생성 실물에 대한 호환 판정, 재사용 import 및 실제 BE export 연결은 미완료다. 그 파일을 중복 생성한 일은 없다.
