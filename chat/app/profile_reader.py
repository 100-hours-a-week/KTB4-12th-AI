"""Opt-in profile enrichment adapter; no inferred recipient IDs or demo fallback.

Not enabled by the existing external Turn API. Its caller must supply an exact
BE-authorized grant and the immutable conversation owner/recipient binding.
"""
import copy
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.errors import APIError
from app.models import RecipientContext


class ProfileReader:
    def __init__(self, url, token, *, transport=None):
        endpoint = urlsplit(url)
        if (not token or endpoint.scheme not in ("http", "https") or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/")):
            raise ValueError("Explicit internal ProfileReader endpoint/token required")
        self.http = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(5, connect=2), trust_env=False, follow_redirects=False, transport=transport)

    async def close(self):
        await self.http.aclose()

    async def supplement(self, explicit, *, binding, access, catalog_contract=None, now=None):
        # Absence of authority means no network request, not a best-effort lookup.
        if (not isinstance(access, dict) or set(access) != {"grantId", "callerUserId", "recipientUserId", "sourceVersion"}
                or access["callerUserId"] != binding.get("senderUserId")
                or access["recipientUserId"] != binding.get("recipientUserId")):
            raise APIError(403, "프로필 조회 권한을 확인할 수 없습니다.")
        try:
            UUID(access["grantId"])
            if any(type(access[k]) is not int or not 0 < access[k] < 2**53 for k in
                   ("callerUserId", "recipientUserId", "sourceVersion")):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise APIError(403, "프로필 조회 권한 계약이 올바르지 않습니다.") from None
        if explicit and (explicit.get("recipientUserId") != access["recipientUserId"]
                or explicit.get("sourceVersion") != access["sourceVersion"]):
            raise APIError(409, "명시된 수신자 문맥과 프로필 버전이 다릅니다.")
        try:
            response = await self.http.post("/internal/v1/profiles/read", json=access)
        except httpx.HTTPError:
            return self._unavailable(explicit, "unavailable")
        if response.status_code in (401, 403):
            raise APIError(response.status_code, "프로필 조회 권한이 거부되었습니다.")
        if response.status_code in (404, 409) or response.status_code >= 500:
            return self._unavailable(explicit, "stale_or_unavailable")
        if response.status_code != 200:
            raise APIError(503, "프로필 응답 계약을 확인할 수 없습니다.")
        try:
            data = response.json()
        except ValueError:
            raise APIError(503, "프로필 응답 계약을 확인할 수 없습니다.") from None
        if not isinstance(data, dict):
            raise APIError(503, "프로필 응답 계약을 확인할 수 없습니다.")
        if (data.get("source") != "profiler" or data.get("recipientUserId") != access["recipientUserId"]
                or data.get("sourceVersion") != access["sourceVersion"]):
            raise APIError(403, "프로필 소유권 또는 버전 검증에 실패했습니다.")
        for name in ("preferredTags", "dislikedTags", "dislikedCategories"):
            if not isinstance(data.get(name), list) or any(not isinstance(x, dict) for x in data[name]):
                raise APIError(503, "프로필 응답 필드가 올바르지 않습니다.")
        try:
            stamp = datetime.fromisoformat(data["asOf"].replace("Z", "+00:00"))
            age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise APIError(503, "프로필 시각을 확인할 수 없습니다.") from None
        if age < 0 or age > 86400:
            return self._unavailable(explicit, "stale")
        result = copy.deepcopy(explicit or {})
        compatible = self._compatible_taxonomy(data, catalog_contract)
        # A stored validator label alone cannot prove the vocabulary/code hash
        # used to derive old tags. Unknown positive inference is optional; an
        # unverified negative constraint must not silently widen recommendations.
        if data["dislikedTags"] and "dislikedTags" not in result and not compatible:
            raise APIError(503, "저장된 비선호 태그와 현재 상품 분류의 호환성을 확인할 수 없습니다.")
        # Presence matters: explicit null/[] is a decision, not missing data.
        if compatible and "giftPreference" not in result and "preferredTags" not in result:
            result["preferredTags"] = copy.deepcopy(data["preferredTags"])
        for field in ("dislikedTags", "dislikedCategories"):
            if field not in result:
                result[field] = copy.deepcopy(data[field])
        result.update(recipientUserId=access["recipientUserId"], sourceVersion=access["sourceVersion"])
        return {"data": result, "profileRead": {"source": "profiler", "sourceVersion": data["sourceVersion"], "asOf": data["asOf"],
                "tagCompatibility": "verified" if compatible else "unverified_inference_omitted"}}

    @staticmethod
    def _compatible_taxonomy(data, catalog):
        # Exact generation equality is deliberately conservative. Never label
        # old profiles with today's vocabulary merely because this code runs now.
        if not isinstance(catalog, dict) or catalog.get("protocol") != "shared-catalog/1":
            return False
        taxonomy = data.get("tagTaxonomy")
        if (not isinstance(taxonomy, dict) or not catalog.get("productTagsRule")
                or not re.fullmatch(r"[0-9a-f]{64}", str(catalog.get("productTagsSha256", "")))
                or not catalog.get("catalogVersion")
                or data.get("catalogVersion") != catalog["catalogVersion"]
                or data.get("validatorVersion") != "tag3disliked-v1"
                or taxonomy != {"productTagsRule": catalog["productTagsRule"],
                               "productTagsSha256": catalog["productTagsSha256"],
                               "validatorVersion": data["validatorVersion"]}):
            return False
        return all(t.get("axis") in {"브랜드", "IP", "카테고리"}
                   and isinstance(t.get("tag"), str) and t["tag"].strip()
                   for t in data["preferredTags"] + data["dislikedTags"])

    @staticmethod
    def _unavailable(explicit, reason):
        # Only an explicit valid BE context can support this fallback.
        try:
            context = RecipientContext.model_validate(explicit)
        except Exception:
            context = None
        if context is None or context.status not in ("AVAILABLE", "NONE"):
            raise APIError(503, "필요한 수신자 프로필을 읽을 수 없습니다.")
        return {"data": copy.deepcopy(explicit), "profileRead": {"source": "backend-explicit", "reason": reason}}
