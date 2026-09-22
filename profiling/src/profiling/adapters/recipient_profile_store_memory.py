"""RecipientProfileStore (메모리) — recipient_user_id → RecipientProfile. DB adapter(ai_profile.recipient_profiles) 전까지의 임시. (목)

여기에 쓸 것 (ports.RecipientProfileStore 구현):
  - class MemoryRecipientProfileStore:
        _items: dict[int, RecipientProfile] · _lock
        upsert(profile) -> None            같은 수신자면 덮어씀. 순서 규칙(should_replace)은 호출자(pipeline)가 판단하고 여기서는 저장만
        get(recipient_user_id) -> RecipientProfile | None
        delete(recipient_user_id) -> bool   사용자 데이터 삭제 요청용 (profile_runs와 함께)
        __len__ / clear                     시험용
"""

from __future__ import annotations

import threading

from profiling.profile.recipient_profile import RecipientProfile

## 메모리 구현을 위한 임시 클래스 -> DB 연동시 변경예정
class MemoryRecipientProfileStore:
    """ports.RecipientProfileStore 구현 (메모리)."""

    def __init__(self) -> None:
        self._items: dict[int, RecipientProfile] = dict()
        self._lock = threading.Lock()

    def upsert(self, profile: RecipientProfile) -> None:



        raise NotImplementedError

    def get(self, recipient_user_id: int) -> RecipientProfile | None:


        raise NotImplementedError

    def delete(self, recipient_user_id: int) -> bool:
 
        ## 제거할려는 아이템이 없다 -> 에러 발생(항목없음)
        if recipient_user_id in self._items:
            
            return 
        ## 제거할려는 아이템이 있다. -> 제거
        else:
            self._items[recipient_user_id] = []
        raise NotImplementedError
