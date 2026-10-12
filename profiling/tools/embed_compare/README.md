# 임베딩 비교 실험 — `tools/embed_compare/`

지금 점수식(S0)과 임베딩 네 방식(A 임베딩만 · B 임베딩 후보 + 코드 점수 · C B + MMR · **D 비선호를 빼고 검색 + 이름 중복 제거**, 2차)의 30개를 같은 사람 · 같은 카탈로그로 나란히 본다. 설계는 [docs/임베딩_추천_설계안_2026-10-12.md](../../docs/임베딩_추천_설계안_2026-10-12.md), 방법은 [docs/임베딩_비교_실험_방법_2026-10-12.md](../../docs/임베딩_비교_실험_방법_2026-10-12.md).

```bash
# 준비(한 번) — product-search 의 모델 · 상품 벡터(방법 문서 §2). 모두 gitignore 대상
cd profiling
docker start profiling-ai-db
uv run python -m tools.embed_compare.run                       # 두 모드(penalty · exclude)
uv run python -m tools.embed_compare.run --modes penalty --top 10
```

| 입력 · 출력 | |
|---|---|
| 사람 · 리뷰 · 비선호 | `tools/scenario_callback30/scenarios.xlsx`(30개 콜백 검증과 같은 파일) |
| 태그 | 같은 파일의 **'태그'** 시트(번호 · 축 · 태그 · 근거 · **추출 모델** · 방법). `--tagger` 로 세트 선택(기본 `claude-opus-5-5`) |
| 로컬 LLM 태그 | `extract_tags.py --models gemma4:12b,qwen3.5:9b` — Ollama · 고정 프롬프트 · temperature 0 · seed 42 · 생각 끔 → 시트에 그 모델 행으로 · 기록 `out/태그추출_<모델>_*.json` |
| 같은 프롬프트 · 다른 모델 원출력 | `extract_tags.py --from-json out/claude_prompt_responses.json` — Ollama 밖에서 같은 SYSTEM · 입력으로 받은 답(예: `claude-opus-5-5-prompt`, 대화 안)을 같은 검증기로 시트에 |
| 추출 모델 비교 | `compare_taggers.py` — 태그 일치(정확 · 근접) · D 30개 겹침 · 시간 → `out/태그추출_비교_*.xlsx` |
| 상품 벡터 | `product-search/data/vectors.f32` · `manifest.json`(읽기만, 해시 · 모델 일치 검사) |
| 질의 벡터 | `product-search/embedding/service.mjs`(Node)를 한 번 띄워 태그마다 |
| 결과 | 화면 + `out/임베딩_비교_<시각>.xlsx`(입력_사용자 · 입력_리뷰 · 요약 · 비교30 · C_점수내역 · D_점수내역 · D_검사 · 태그(근거) · 설정) · `.json` — `out/`은 커밋 안 함. D 검사가 하나라도 실패하면 종료 코드 1 |

매개변수는 `run.py` 위쪽 상수: `PER_TAG`(태그당 후보 50) · `RRF_K`(30) · `SIM_WEIGHT`(2.0) · `MMR_LAMBDA`(0.7) · D 전용 `D_POP_CANDS`(0) · `D_AXIS_WEIGHT`(특징 ½) · `D_LAMBDA`(0.7) · `D_CAT_CAP`(한 소분류 최대 10). 앱 · 7.7 · DB 쓰기는 없다 — 코어 함수(`category_scores` · `rank_products`)만 부른다.

모델(Jina v5 nano retrieval)은 **CC BY-NC 4.0(비상업)** — 실험용. 서비스 반영 전 확인 필요.
