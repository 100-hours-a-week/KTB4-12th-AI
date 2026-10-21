"""Narrow internal read contract. Grants must be issued by an authorized BE path.

There is deliberately no grant-creation API. The current BE contract cannot prove
this scope; production auto-enrichment remains disabled until that contract exists.
"""
from datetime import datetime, timezone
import secrets
from uuid import UUID
import os
import httpx

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from profiling.settings import get_settings

router = APIRouter()


class ProfileReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grantId: UUID
    callerUserId: int = Field(strict=True, ge=1, le=2**53 - 1)
    recipientUserId: int = Field(strict=True, ge=1, le=2**53 - 1)
    sourceVersion: int = Field(strict=True, ge=1, le=2**53 - 1)
    conversationId: UUID | None = None
    turnId: UUID | None = None


def read_scoped_profile(engine, body, token):
    """BE owns mint/revoke; existing service identity proves introspection origin.

    No app role gains grant-table write permission, and no fallback to a
    different grant authority is allowed when scoped verification fails.
    """
    if os.getenv('PROFILING_BE_GRANTS_ENABLED') != 'true' or not body.conversationId or not body.turnId:
        raise HTTPException(403, 'PROFILE_SCOPE_DENIED')
    with httpx.Client(base_url=get_settings().BACKEND_BASE_URL.rstrip('/'), timeout=5,
                      trust_env=False, follow_redirects=False) as http:
        reply = http.post('/api/internal/v1/profile-grants/verify',
            headers={'Authorization': 'Bearer '+token}, json=body.model_dump(mode='json'))
    if reply.status_code in (400, 401, 403, 404, 409):
        raise HTTPException(403, 'PROFILE_SCOPE_DENIED')
    if reply.status_code != 200:
        raise HTTPException(503, 'PROFILE_AUTHORITY_UNAVAILABLE')
    expires = reply.json()['data']['expiresAt']
    stamp = datetime.fromisoformat(expires.replace('Z', '+00:00'))
    remaining = (stamp-datetime.now(timezone.utc)).total_seconds()
    if not 0 < remaining <= 121:
        raise HTTPException(403, 'PROFILE_SCOPE_DENIED')
    with engine.connect() as connection:
        row = connection.execute(sa.text('''SELECT source_version,guarded_input,successful_analysis
            FROM ai_profile.recipient_inputs WHERE recipient_user_id=:recipient'''),
            {'recipient':body.recipientUserId}).mappings().first()
    if not row: raise HTTPException(404, 'PROFILE_NOT_FOUND')
    analysis = row['successful_analysis']
    if (row['source_version'] != body.sourceVersion or not analysis
            or analysis['sourceVersion'] != body.sourceVersion):
        raise HTTPException(409, 'PROFILE_VERSION_MISMATCH')
    if analysis['tagStatus'] not in ('ok','skip','skip_no_signal'):
        raise HTTPException(503, 'PROFILE_ANALYSIS_UNAVAILABLE')
    return {'source':'profiler','recipientUserId':body.recipientUserId,'sourceVersion':body.sourceVersion,
        'giftPreference':row['guarded_input']['giftPreference'],
        'asOf':analysis['analyzedAt'],'preferredTags':analysis['preferredTags'],
        'dislikedTags':analysis['dislikedTags'],'dislikedCategories':row['guarded_input']['dislikedCategories'],
        'tagStatus':analysis['tagStatus'],'validatorVersion':analysis['validatorVersion'],
        'catalogVersion':analysis['catalogVersion'],'tagTaxonomy':analysis.get('tagTaxonomy'),
        'grantExpiresAt':expires}


def read_profile(engine, request):
    with engine.connect() as connection:
        connection = connection.execution_options(isolation_level="REPEATABLE READ")
        grant = connection.execute(sa.text("""SELECT caller_user_id,recipient_user_id,source_version,expires_at,revoked_at
            FROM ai_profile.read_grants WHERE id=:id"""), {"id": request.grantId}).mappings().first()
        if (not grant or grant["revoked_at"] is not None
                or grant["expires_at"] <= datetime.now(timezone.utc)
                or (grant["caller_user_id"], grant["recipient_user_id"], grant["source_version"])
                != (request.callerUserId, request.recipientUserId, request.sourceVersion)):
            raise HTTPException(403, "PROFILE_SCOPE_DENIED")
        row = connection.execute(sa.text("""SELECT source_version,preferred_tags,disliked_tags,
            disliked_categories,updated_at,tag_status,prompt_version,validator_version,catalog_version_id
            FROM ai_profile.recipient_profiles WHERE recipient_user_id=:recipient"""),
            {"recipient": request.recipientUserId}).mappings().first()
        if not row:
            raise HTTPException(404, "PROFILE_NOT_FOUND")
        if row["source_version"] != request.sourceVersion:
            raise HTTPException(409, "PROFILE_VERSION_MISMATCH")
        return {"source": "profiler", "recipientUserId": request.recipientUserId,
                "sourceVersion": row["source_version"], "asOf": row["updated_at"].isoformat(),
                "preferredTags": row["preferred_tags"], "dislikedTags": row["disliked_tags"],
                "dislikedCategories": [{"categoryId": c["category_id"], "categoryName": c["category_name"]}
                                       for c in row["disliked_categories"]], "tagStatus": row["tag_status"],
                "promptVersion": row["prompt_version"], "validatorVersion": row["validator_version"],
                "catalogVersion": str(row["catalog_version_id"]) if row["catalog_version_id"] else None,
                # Existing rows did not persist the vocabulary hash at derivation.
                # Do not invent it from the current runtime or mutate old profiles.
                "tagTaxonomy": None}


@router.post("/internal/v1/profiles/read")
async def profile_read(body: ProfileReadRequest, request: Request, response: Response):
    provider = getattr(request.app.state, "be_token_provider", None)
    try:
        token = (await anyio.to_thread.run_sync(provider)).value if provider else get_settings().SERVICE_TOKEN
    except ValueError:
        raise HTTPException(503, "SERVICE_TOKEN_UNAVAILABLE") from None
    if not token or not secrets.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
        raise HTTPException(401, "UNAUTHORIZED")
    response.headers["Cache-Control"] = "no-store"
    try:
        if body.conversationId is not None or body.turnId is not None:
            return await anyio.to_thread.run_sync(read_scoped_profile, request.app.state.engine, body, token)
        return await anyio.to_thread.run_sync(read_profile, request.app.state.engine, body)
    except HTTPException:
        raise
    except Exception:
        # SQL/connection errors can contain profile fields: do not log their text.
        raise HTTPException(503, "PROFILE_STORE_UNAVAILABLE") from None
