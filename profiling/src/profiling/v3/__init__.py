"""프로파일러 v3 — 오픈 모델(Gemma 4 12B, Modal GPU 컨테이너 안 vLLM) + Laya + 규칙으로 태그를 뽑는다(버전 3 하나, 11-04 버전 25 버림).

설정 TAG_ENGINE 기본이 v3(11-04)이고, V3_URL · 키 3개가 다 있어야 실제로 켜진다(하나라도 비면 v2 추출기로만 — TAG_ENGINE=v2 로 끌 수 있음). v3 를 못 쓰면(모델 서버 연결 · 5xx · 기한 넘음 · Laya 못 올림)
그 사람은 v2 추출기(TAGGER, 예 openrouter)로 넘긴다 — 이때 리뷰 정리 글은 지금 v2 처럼 외부 API 로 간다.
engine.py 가 AI 서버 쪽(Modal 한 번 + 규칙), core.py 가 Modal 안 단계 순서, client.py 가 HTTP, laya.py 가 분류기, rules.py · graph.py 가 후처리 규칙.
"""
