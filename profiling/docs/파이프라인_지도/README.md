# 파이프라인 지도 — 갱신 기록

프로파일링 서비스가 **지금 어디까지 어떻게 동작하는지**를 그림 한 장과 문서 한 편으로 남기는 곳. 담당자가 자리를 비워도 다른 팀원이 이 폴더만 읽고 이어받을 수 있게 쓴다.

## 색인 (최신이 위)

| 날짜 | 문서 | 한 줄 요약 | 다음 할 일 1순위 |
|---|---|---|---|
| 2026-09-23 | [2026-09-23.md](2026-09-23.md) | 패키지 평탄화(6폴더 13모듈 → 1폴더 11모듈) + 저장소를 PostgreSQL 전용으로 (메모리 구현·`PROFILING_STORE` 제거) | Backend 미팅 결과 반영 → 상품 ID를 Backend 기준으로 재적재 |
| 2026-09-22 | [2026-09-22.md](2026-09-22.md) | v1 끝까지 동작 + 저장소를 PostgreSQL 두 테이블로 전환 | Backend 미팅 결과(7.7 태그 · 7.9 조회수 필드명 · 제외 vs 감점 · 디바운스) 코드 반영 |

## 규칙

**언제 갱신하나** — 다음 중 하나면 새 문서를 만든다. 그 밖에는 주 1회(타운홀 전날).

- 단계(접수·슬롯·처리·콜백)의 함수가 늘거나 바뀜
- 포트·어댑터가 늘거나 구현체가 바뀜 (파일 → DB, 메모리 → DB 같은 교체)
- 바깥 계약(7.6·7.7·7.9)이나 DB 테이블이 바뀜
- 범위가 바뀜 (v1 → v3 단계 추가)

**무엇을 남기나** — 문서마다 반드시 다섯 가지: ① 그림(그 날짜 스냅샷) ② 단계별로 지금 부르는 함수 ③ 지난 판에서 바뀐 것 ④ 돌려 보는 법과 확인 포인트 ⑤ **다음 해야 할 일**(인수인계용 — 무엇·왜·어디·완료 기준).

**어떻게 만드나**

```bash
cd profiling
python3 docs/assets/build_pipeline_map.py                                   # 코드에 맞게 스크립트 먼저 고친다
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --window-size=3400,2152 --screenshot="$PWD/docs/assets/pipeline-map.png" "file://$PWD/docs/assets/pipeline-map.svg"
cp docs/assets/pipeline-map.png "docs/파이프라인_지도/assets/pipeline-map_$(date +%F).png"   # 날짜 스냅샷 (덮어쓰지 않음)
cp "docs/파이프라인_지도/_템플릿.md" "docs/파이프라인_지도/$(date +%F).md"                # 템플릿에서 시작
```

그 다음 새 문서를 채우고, 이 README의 색인 맨 위에 한 줄 추가한다. `docs/assets/pipeline-map.png`는 항상 최신본, 이 폴더의 `assets/`는 날짜별 보관본이다.

**쓰는 법** — 문장은 팀원이 코드를 안 열어 봐도 이해되게. 함수 이름은 그대로 쓰되 무엇을 하는지 한 줄을 붙인다. "다음 해야 할 일"은 우선순위 순서로, 각 항목에 파일·함수 위치와 "끝났다고 판단하는 기준"(테스트·확인 명령)을 적는다.

## 파일

```
docs/파이프라인_지도/
├─ README.md          이 파일 — 색인·규칙
├─ _템플릿.md          새 문서의 틀
├─ 2026-09-23.md      구조 평탄화 · 저장소 DB 전용화
├─ 2026-09-22.md      첫 판 (v1 + DB 전환)
└─ assets/
   ├─ pipeline-map_2026-09-23.png
   └─ pipeline-map_2026-09-22.png
```
