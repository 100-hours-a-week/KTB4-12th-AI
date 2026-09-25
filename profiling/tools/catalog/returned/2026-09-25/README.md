# Backend 회신 2026-09-25 — v1 확정 카테고리·상품

BE가 보낸 xlsx 두 개를 `tools/catalog/import_be_ids.py`로 바꾼 중간 파일. **이 폴더가 회신의 스냅샷**이다 — 같은 입력이면 같은 결과가 나오므로 언제든 다시 만들 수 있고, 무엇을 어떻게 이었는지 확인할 수 있다.

| 원본 (BE 전달) | 내용 |
|---|---|
| `카테고리_DB_ID_매핑_2026-09-23.xlsx` | 대분류 10 · 소분류 57 · 활성 상품 4,231 |
| `v1_상품목록_4231개_2026-09-23.xlsx` | 상품 4,231 (ID 1~4231) · 가격 · 재고 · 조회수 |

| 만든 파일 | 줄 수 | 쓰는 곳 |
|---|---|---|
| `category-id-map.jsonl` | 67 | `load_catalog --category-id-map` → `categories.backend_category_id` |
| `product-id-map.jsonl` | 4,231 | `load_catalog --id-map` → `products.backend_product_id` |
| `metrics.jsonl` | 4,231 | `load_catalog --metrics` → `products.availability` · `view_count` |

## 어떻게 이었나

회신에는 **수집처 ID(`KAKAO_GIFT:…` · `CAT-01-02`)가 없다.** BE가 발급한 번호와 이름·가격뿐이라 이름으로 이었다.

- **카테고리** — 소분류·대분류 **이름**으로. 57/57 · 10/10 일치(양쪽 차집합 0)
- **상품** — `(이름, 브랜드, 가격, 소분류 이름)`으로 4,224건 확정
- **남은 7건** — BE 표에 이름·브랜드·가격·분류가 **완전히 같은 중복 상품**이 3군 있다(상품권 3 · 프라다 타이 2 · 헤라 쿠션 2). 구분할 근거가 없어 양쪽을 정렬해 1:1로 붙였다. 같은 입력이면 항상 같은 배정이 나온다
- 한 Backend 번호가 두 상품에 붙으면 도구가 즉시 실패한다

## 값에 대해 알아둘 것

- **재고가 전건 100**이다. MVP 기본값으로 보이며 실제 재고가 아닐 수 있다. `availability = available`로 저장했다(재고 > 0). 실제 재고로 갱신되면 BE에서 다시 받아 `--metrics`로 덮으면 된다
- **조회수는 102 ~ 49,989**로 값이 퍼져 있다. v1 추천 풀의 정렬 기준이다 (정렬 기준 자체는 아직 논의 중)
- 소분류 `주류`(BE 34)는 상품 0건 — 우리 `CAT-04-07`도 0건이라 일치한다

## 다시 만들기

```bash
uv run python -m tools.catalog.import_be_ids \
  --categories ~/Downloads/카테고리_DB_ID_매핑_2026-09-23.xlsx \
  --products   ~/Downloads/v1_상품목록_4231개_2026-09-23.xlsx \
  --out        tools/catalog/returned/2026-09-25/

uv run python -m tools.catalog.load_catalog \
  --id-map          tools/catalog/returned/2026-09-25/product-id-map.jsonl \
  --category-id-map tools/catalog/returned/2026-09-25/category-id-map.jsonl \
  --metrics         tools/catalog/returned/2026-09-25/metrics.jsonl
```
