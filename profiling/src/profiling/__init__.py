"""수신자 프로파일링 — 공개 표면. 다른 모듈(Chat·Search·도구)은 여기 있는 것만 쓰면 된다.

  profile(rq, catalog=…, store=…, recipient_store=…)      7.6 한 건을 끝까지 처리한다        pipeline.py
  ProfileRequest · ProfileOutcome · RunStatus · ErrorCode  단계 사이를 오가는 값               types.py
  Settings · get_settings                                 환경변수 하나로 모은 설정           settings.py

어떤 구현을 끼울지(카탈로그 파일·PostgreSQL·Backend HTTP)는 main.py 한 곳에서만 정한다 — 여기서는 아무것도 만들지 않는다.
HTTP 경계의 DTO(7.6 요청·7.7 콜백)는 schemas.py, 바깥에 요구하는 모양(Protocol)은 ports.py.
"""

def __getattr__(name):
    # Keep the public API while allowing Search to import the pure vocabulary
    # without loading personal-profile policy, prompts, settings or DB packages.
    from importlib import import_module
    module = {"profile": "pipeline", "Settings": "settings", "get_settings": "settings",
              "ErrorCode": "types", "ProfileOutcome": "types", "ProfileRequest": "types", "RunStatus": "types"}.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module("profiling." + module), name)
    globals()[name] = value
    return value

__all__ = [
    "ErrorCode",
    "ProfileOutcome",
    "ProfileRequest",
    "RunStatus",
    "Settings",
    "get_settings",
    "profile",
]
