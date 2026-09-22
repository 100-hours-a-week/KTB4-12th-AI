"""포트 — 업무 모듈(profile)이 바깥에 요구하는 "모양". 구현은 adapters/에 있고, 조립은 main.py에서만 한다.

typing.Protocol 이라 상속이 필요 없다: 메서드 이름·인자·반환이 같으면 어떤 클래스든 이 자리에 꽂힌다
(adapters의 실제 구현, tests/의 가짜, 팀원의 adapter 전부). 업무 코드(pipeline.py)는 이 파일만 import하고
adapters를 import하지 않는다 — 그래야 파일→DB, 메모리→PostgreSQL로 바꿔도 pipeline이 안 바뀐다.

이름은 팀원 3단계 구현 상세 §4와 맞춘다. 확정 전이라 오늘은 여기 이름이 기준이고, 합칠 때 그쪽 이름으로 바꾼다
(후보: CatalogReader.active → Catalog.acquire()/SnapshotHandle, ProfileModel.analyze는 팀원 문서 이름 그대로).

@runtime_checkable 을 붙여 두어 isinstance(obj, CatalogReader) 로 "모양이 맞는지"를 테스트에서 확인할 수 있다
(메서드 존재만 검사하고 시그니처는 검사하지 않는다).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from profiling.profile.recipient_profile import RecipientProfile
from profiling.profile.types import ProfileOutcome, RunStatus
from profiling.transport.schemas import ProductRecord

# ---------------------------------------------------------------------------
# 예외 — 포트가 던지고 Transport(api/)가 HTTP 상태로 바꾼다
# ---------------------------------------------------------------------------


class NoActiveCatalog(Exception):
    """활성 카탈로그가 없다(파일 없음·비어 있음·DB에 활성 버전 없음). 7.6에서 503 SERVICE_UNAVAILABLE."""


# ---------------------------------------------------------------------------
# 오늘(v1) 쓰는 포트 3개
# ---------------------------------------------------------------------------


@runtime_checkable
class CatalogReader(Protocol):
    """활성 카탈로그 한 벌을 준다. 구현: adapters/catalog_reader_file.FileCatalogReader (오늘) → DB adapter (내일)."""

    def active(self) -> tuple[int, list[ProductRecord]]:
        """(catalog_version_id, 활성 버전의 상품 전체). 활성 버전이 없으면 NoActiveCatalog.

        한 요청 안에서는 한 번만 부르고 결과를 들고 다닌다 — 조인·검색·저장이 같은 버전을 보게(4단계 snapshot 원칙).
        """
        ...

    def by_id(self, product_id: int) -> ProductRecord | None:
        """상품 번호 하나 → 상품. 없으면 None. v3 리뷰 조인용(v1은 쓰지 않음)."""
        ...


@runtime_checkable
class ProfileRunStore(Protocol):
    """프로파일링 결과 보관. 구현: adapters/profile_run_store_memory.MemoryProfileRunStore (오늘) → DB adapter profile_runs·recipient_profiles (내일)."""

    def save(self, outcome: ProfileOutcome) -> None:
        """실행 기록 + 수신자 프로필 upsert. 같은 recipient_user_id면 덮어쓴다.

        오늘은 source_version 순서를 보지 않는다(낮은 버전이 뒤에 와도 덮어씀). 순서 역전 처리(409·SUPERSEDED)는 #23.
        """
        ...

    def get(self, recipient_user_id: int) -> ProfileOutcome | None:
        """마지막으로 저장된 결과. 없으면 None. Chat이 대화 시작 시 읽는 것이 이것(6단계 1.2)."""
        ...


@runtime_checkable
class BackendPort(Protocol):
    """Backend 정본으로 나가는 HTTP. 구현: adapters/backend_port_http.HttpBackendPort."""

    def send_profile_callback(self, outcome: ProfileOutcome) -> RunStatus:
        """7.7 POST 한 번 → 실행 기록의 다음 상태(3단계 §10.2). 예외를 밖으로 내지 않는다.

        DELIVERED(200) · SUPERSEDED(409, 폐기) · FAILED(4xx, 재시도 없음) · RESULT_READY(5xx·네트워크, 아직 전달 못 함 = 재시도 대상).
        오늘은 1회만 보낸다. 재시도·백오프(#23)는 RESULT_READY를 받은 쪽이 아니라 이 adapter 안에서.
        """
        ...


@runtime_checkable
class RecipientProfileStore(Protocol):
    """수신자 프로필(태그) 보관 — ai_profile.recipient_profiles. 구현: adapters/recipient_profile_store_memory (목) → DB adapter.
    ProfileRunStore(실행 기록·전달 상태)와 분리: 실행 기록은 시도마다 한 행, 프로필은 수신자당 한 행(최신 분석이 덮어씀)."""

    def upsert(self, profile: RecipientProfile) -> None: ...

    def get(self, recipient_user_id: int) -> RecipientProfile | None: ...

    def delete(self, recipient_user_id: int) -> bool: ...


# ---------------------------------------------------------------------------
# v3에서 쓰는 포트 — 지금은 자리만. pipeline.needs_model()이 True인 분기에서 호출된다.
# ---------------------------------------------------------------------------


@runtime_checkable
class ProfileModel(Protocol):
    """태그 추출 모델. 구현 후보: 규칙 추출기(실험 13, 모델 없음) · Ollama(gemma) · (Jev, 09-25 만료).
    이름은 팀원 Model adapter의 `ProfileModel.analyze`와 같게 둔다."""

    def analyze(self, prompt: str, schema: dict, seed: int) -> dict:
        """모델 1회 호출 → JSON 초안(dict). 파싱 실패·타임아웃은 여기서 예외로 올리고 pipeline이 재시도 예산을 관리한다.
        반환을 dict로 둔 이유: 3축(likes/dislikes/key_features)·평면(flat) 등 스키마가 실험에 따라 달라서 types.ExtractDraft로의
        변환은 pipeline 쪽에서 한다."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """문서·질의 임베딩(BGE-m3-ko, 1024차원). Search도 같은 것을 쓴다(개발 이슈 #8)."""

    def embed_docs(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
