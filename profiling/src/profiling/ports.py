"""포트 — 업무 코드(pipeline.py)가 바깥에 요구하는 "모양". 구현은 catalog.py · stores.py · backend.py 에 있고, 조립은 main.py에서만 한다.

typing.Protocol 이라 상속이 필요 없다: 메서드 이름·인자·반환이 같으면 어떤 클래스든 이 자리에 꽂힌다
(catalog.py·stores.py·backend.py의 실제 구현, tests/의 가짜, 팀원의 어댑터 전부). pipeline.py는 이 파일만 import하고
구현 모듈을 import하지 않는다 — 그래야 카탈로그를 파일에서 DB로 바꿔도 pipeline이 안 바뀐다.

이름은 팀원 3단계 구현 상세 §4와 맞춘다. 확정 전이라 오늘은 여기 이름이 기준이고, 합칠 때 그쪽 이름으로 바꾼다
(후보: CatalogReader.active → Catalog.acquire()/SnapshotHandle, ProfileModel.analyze는 팀원 문서 이름 그대로).

@runtime_checkable 을 붙여 두어 isinstance(obj, CatalogReader) 로 "모양이 맞는지"를 테스트에서 확인할 수 있다
(메서드 존재만 검사하고 시그니처는 검사하지 않는다).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import NamedTuple, Protocol, runtime_checkable

from profiling.schemas import ProductRecord
from profiling.types import CallbackResult, ProfileOutcome, RecipientProfile

# ---------------------------------------------------------------------------
# 예외 — 포트가 던지고 Transport(api/)가 HTTP 상태로 바꾼다
# ---------------------------------------------------------------------------


class NoActiveCatalog(Exception):
    """활성 카탈로그가 없다(파일 없음·비어 있음·DB에 활성 버전 없음). 7.6에서 503 SERVICE_UNAVAILABLE."""


# ---------------------------------------------------------------------------
# 포트 4개 — v1에서 모두 쓴다 (v3의 ProfileModel·Embedder는 그때 여기에 추가)
# ---------------------------------------------------------------------------


@runtime_checkable
class CatalogReader(Protocol):
    """활성 카탈로그 한 벌을 준다. 구현: catalog.FileCatalogReader (오늘) → DB adapter (내일)."""

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
    """프로파일링 결과 보관. 구현: stores.DbProfileRunStore (ai_profile.profile_runs)."""

    def save(self, outcome: ProfileOutcome) -> None:
        """실행 기록 + 수신자 프로필 upsert. 같은 recipient_user_id면 덮어쓴다.

        오늘은 source_version 순서를 보지 않는다(낮은 버전이 뒤에 와도 덮어씀). 순서 역전 처리(409·SUPERSEDED)는 #23.
        """
        ...

    def get(self, recipient_user_id: int) -> ProfileOutcome | None:
        """마지막으로 저장된 결과. 없으면 None. Chat이 대화 시작 시 읽는 것이 이것(6단계 1.2)."""
        ...

    def run_lock(self, recipient_user_id: int, source_version: int) -> AbstractContextManager[bool]:
        """그 (수신자, 원본 버전)에 대한 **배타 잠금**. `with store.run_lock(rid, sv) as got:` 로 쓴다.

        `got=False` 면 다른 실행이 이미 그 키를 처리하고 있다는 뜻이므로 아무것도 하지 않는다.
        접수 판정(무엇을 할지 고르기)과 그 실행(분석·재전송)이 **한 잠금 안에서** 일어나야
        같은 요청이 동시에 두 번 와도 분석이 두 벌 돌지 않는다.

        구현은 프로세스 밖에서도 통하는 잠금이어야 한다(앱을 여러 개 띄울 수 있으므로) —
        DbProfileRunStore 는 PostgreSQL advisory lock 을 쓴다.
        """
        ...

    def get_run(self, recipient_user_id: int, source_version: int) -> ProfileOutcome | None:
        """그 (수신자, 원본 버전) 한 행. 없으면 None.

        접수 단계 중복 판정이 쓴다 — Backend가 같은 sourceVersion으로 복구 전송하면(PENDING이 maximum-window ⚙6h를 넘었을 때, v0.7 ⑤)
        이미 도는 분석을 또 돌리거나 이미 만든 결과를 다시 만들지 않기 위해. get()은 "최신 한 건"이라 키를 지정해 찾을 수 없다.
        반환값의 updated_at으로 RUNNING이 아직 살아 있는지 본다.
        """
        ...


@runtime_checkable
class BackendPort(Protocol):
    """Backend 정본으로 나가는 HTTP. 구현: backend.HttpBackendPort."""

    def send_profile_callback(self, outcome: ProfileOutcome) -> CallbackResult:
        """7.7 POST 한 번 → 실행 기록의 다음 상태 + 사유(3단계 §10.2). 예외를 밖으로 내지 않는다.

        status: DELIVERED(200) · SUPERSEDED(409, 폐기) · FAILED(4xx, 재시도 없음) · RESULT_READY(5xx·네트워크, 아직 전달 못 함 = 재시도 대상).
        code: CALLBACK_STALE(409) · CONTRACT_7_7_REJECTED(4xx) · CALLBACK_UNREACHABLE(5xx·네트워크) · DELIVERED면 None.
        오늘은 1회만 보낸다. 재시도·백오프(#23)는 RESULT_READY를 받은 쪽이 아니라 이 adapter 안에서.
        """
        ...


@runtime_checkable
class RecipientProfileStore(Protocol):
    """수신자 프로필(태그) 보관 — ai_profile.recipient_profiles. 구현: stores.DbRecipientProfileStore.
    ProfileRunStore(실행 기록·전달 상태)와 분리: 실행 기록은 시도마다 한 행, 프로필은 수신자당 한 행(최신 분석이 덮어씀).
    upsert·get·delete 는 리뷰 자식 행(recipient_reviews, 0005)을 포함한다 — 저장은 스냅샷 교체, 낮은 버전이면 둘 다 그대로."""

    def upsert(self, profile: RecipientProfile) -> None: ...

    def get(self, recipient_user_id: int) -> RecipientProfile | None: ...

    def delete(self, recipient_user_id: int) -> bool: ...


@runtime_checkable
class EmbeddingIndex(Protocol):
    """상품 벡터 + 질의 임베딩 — 풀 고르기 D 의 임베딩 단계(selection.select_pool)가 쓴다. 없으면(None) D 는 임베딩 없이 돈다.

    구현 후보: product-search(검색기)가 만든 상품 벡터 · 같은 모델의 질의 인코더. 벡터는 모두 L2 정규화(내적 = 코사인).
    `version` 은 모델 · 벡터 해시처럼 결과를 다시 만들 수 있게 하는 식별자 — 같은 입력 = 같은 결과를 지키려면 실행 기록에 남긴다.
    """

    version: str

    def product_vectors(self, product_ids: list[int]):  # -> numpy.ndarray (len, d)
        ...

    def encode(self, texts: list[str]):  # -> numpy.ndarray (len, d)
        ...


class TokenUsage(NamedTuple):
    """LLM 한 번의 사용량 — 로컬이어도 잰다(외부 API 로 바꿀 때 비용 추정 · 느려짐 원인 추적). 모르는 값은 None.

    input/output 은 토큰 수, *_ms 는 밀리초. stop 은 멈춘 이유(stop = 정상 · length = 출력 상한에 잘림)."""

    input_tokens: int | None
    output_tokens: int | None
    total_ms: float | None = None
    load_ms: float | None = None
    input_ms: float | None = None
    output_ms: float | None = None
    stop: str | None = None


class TagReply(NamedTuple):
    """TagExtractor.extract 의 반환 — 원출력(JSON 문자열) + 사용량."""

    text: str
    usage: TokenUsage | None = None


class TagExtractError(Exception):
    """태그 추출 실패를 원인별로 — kind: timeout · connect · http_<상태> · bad_response · truncated. 로그 · 집계용 분류다.
    메시지엔 모델 · 상태만 넣고 원문(프롬프트 · 출력)은 넣지 않는다."""

    def __init__(self, kind: str, detail: str = "", usage: TokenUsage | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.usage = kind, usage


@runtime_checkable
class TagExtractor(Protocol):
    """LLM 한 번 호출 — 시스템 프롬프트 + 사용자 입력 + JSON 스키마 → TagReply(원출력 JSON 문자열 + 사용량). 프롬프트 · 검증은 tagging 모듈이 한다.

    구현: llm.OllamaTagExtractor(로컬 Ollama). `model` 은 실행 기록 · 로그에 남길 모델 이름(예: gemma4:12b). 실패는 예외로(되도록
    TagExtractError — 원인 분류) — pipeline 이 잡아 태그 없이 진행한다(요청을 실패시키지 않는다).
    """

    model: str

    def extract(self, system: str, prompt: str, schema: dict) -> TagReply: ...
