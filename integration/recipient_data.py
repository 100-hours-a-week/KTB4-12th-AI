"""Latest authenticated Profiler input, separate from derived profile analysis.

Only the shared integration runtime installs this adapter. Raw review bodies are
not copied: their existing normalized request hash still protects version identity.
The immutable version hashes detect conflicting retries without retaining a second
history of preference text. Applications never call ``migrate`` themselves.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import psycopg


RECIPIENT_INPUTS_DDL = """
CREATE TABLE IF NOT EXISTS ai_profile.recipient_inputs (
    recipient_user_id bigint PRIMARY KEY CHECK (recipient_user_id > 0),
    source_version bigint NOT NULL CHECK (source_version >= 0),
    input jsonb NOT NULL CHECK (jsonb_typeof(input) = 'object'),
    guarded_input jsonb NOT NULL CHECK (jsonb_typeof(guarded_input) = 'object'),
    guard_version text NOT NULL,
    input_hash text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    successful_analysis jsonb
);
ALTER TABLE ai_profile.recipient_inputs ADD COLUMN IF NOT EXISTS successful_analysis jsonb;
ALTER TABLE ai_profile.recipient_inputs ADD COLUMN IF NOT EXISTS guarded_input jsonb NOT NULL;
ALTER TABLE ai_profile.recipient_inputs ADD COLUMN IF NOT EXISTS guard_version text NOT NULL;
CREATE TABLE IF NOT EXISTS ai_profile.recipient_input_versions (
    recipient_user_id bigint NOT NULL CHECK (recipient_user_id > 0),
    source_version bigint NOT NULL CHECK (source_version >= 0),
    input_hash text NOT NULL,
    PRIMARY KEY (recipient_user_id, source_version)
);
"""


def migrate(db) -> None:
    """Add input storage; preserve only known identity hashes of legacy requests.

    Legacy cleaned analysis text is deliberately not turned into an original
    request. A recipient has no raw snapshot until that exact input arrives.
    """
    db.execute(RECIPIENT_INPUTS_DDL)
    db.execute("""INSERT INTO ai_profile.recipient_input_versions
            (recipient_user_id,source_version,input_hash)
        SELECT recipient_user_id,source_version,input_hash
        FROM ai_profile.profile_runs WHERE input_hash IS NOT NULL
        ON CONFLICT (recipient_user_id,source_version) DO NOTHING""")


class RecipientInputConflict(ValueError):
    """One source version was already associated with a different request."""


class RecipientInputInvalid(ValueError):
    """A validated DTO still contains text PostgreSQL JSONB cannot preserve."""


def _lock_key(recipient_user_id: int) -> int:
    digest = hashlib.sha256(f"recipient-input:{recipient_user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _recover_analysis(db, recipient_user_id: int, source_version: int) -> None:
    """Attach a proven existing analysis only after its exact raw input arrives.

    A legacy delivered retry only resends its callback, so it does not pass the
    new profile-write wrapper. Match the run hash and version here; do not infer
    an original input from cleaned analysis or attach an in-progress/failed run.
    """
    db.execute("""UPDATE ai_profile.recipient_inputs i SET successful_analysis=jsonb_build_object(
            'sourceVersion', p.source_version, 'preferredTags', p.preferred_tags,
            'dislikedTags', p.disliked_tags, 'tagStatus', p.tag_status,
            'catalogVersion', p.catalog_version_id, 'promptVersion', p.prompt_version,
            'validatorVersion', p.validator_version, 'analyzedAt', p.updated_at)
        FROM ai_profile.recipient_profiles p JOIN ai_profile.profile_runs r
            ON r.recipient_user_id=p.recipient_user_id AND r.source_version=p.source_version
        WHERE i.recipient_user_id=%s AND i.source_version=%s
            AND p.recipient_user_id=i.recipient_user_id AND p.source_version=i.source_version
            AND r.input_hash=i.input_hash AND r.tag_status=p.tag_status
            AND r.status IN ('RESULT_READY','DELIVERED','SUPERSEDED')
            AND p.tag_status IN ('ok','skip','skip_no_signal')
            AND (i.successful_analysis IS NULL OR
                (i.successful_analysis->>'sourceVersion')::bigint < i.source_version)""",
        (recipient_user_id, source_version))


class RecipientInputStore:
    """Write one user's latest received input under a short database lock.

    ``record`` runs before analysis admission. It says only that a valid,
    authenticated input was received, never that analysis or callback succeeded.
    The return value is ``stored``, ``duplicate`` or ``stale``. Failed analysis
    retries of an identical request continue through the existing worker policy.
    """

    def __init__(self, dsn: str, *, connect_timeout_s: int = 5,
                 statement_timeout_ms: int = 5000, lock_timeout_ms: int = 2000,
                 idle_in_tx_timeout_ms: int = 60000):
        self.dsn = ("postgresql://" + dsn.removeprefix("postgresql+psycopg://")
                    if dsn.startswith("postgresql+psycopg://") else dsn)
        if min(connect_timeout_s, statement_timeout_ms, lock_timeout_ms, idle_in_tx_timeout_ms) <= 0:
            raise ValueError("Recipient input storage requires bounded database timeouts")
        self.connect_timeout_s = connect_timeout_s
        self.options = " ".join(f"-c {name}={value}" for name, value in (
            ("statement_timeout", statement_timeout_ms),
            ("lock_timeout", lock_timeout_ms),
            ("idle_in_transaction_session_timeout", idle_in_tx_timeout_ms)))

    def _connect(self):
        return psycopg.connect(self.dsn, connect_timeout=self.connect_timeout_s, options=self.options)

    def record(self, recipient_user_id: int, source_version: int,
               payload: dict, input_hash: str) -> str:
        if recipient_user_id <= 0 or source_version < 0:
            raise ValueError("Invalid recipient input identity")
        if set(payload) != {"giftPreference", "dislikedCategories"}:
            raise ValueError("Unexpected recipient input fields")
        texts = [payload["giftPreference"]] + [item["categoryName"] for item in payload["dislikedCategories"]]
        if any(isinstance(text, str) and "\x00" in text for text in texts):
            raise RecipientInputInvalid("Recipient input contains unsupported text")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            encoded.encode("utf-8")
        except UnicodeError as e:
            raise RecipientInputInvalid("Recipient input contains unsupported text") from e
        # Keep original text for its explicitly authorized storage purpose, but
        # Chat must consume the same deterministic guard used by Emmet's LLM.
        from profiling import guard
        guarded = {"giftPreference": guard.clean(payload["giftPreference"]).text,
                   "dislikedCategories": [{"categoryId": item["categoryId"],
                       "categoryName": guard.clean(item["categoryName"]).text or ""}
                       for item in payload["dislikedCategories"]]}
        with self._connect() as db:
            # Serialize versions for this recipient across processes, including
            # the first insert when there is no latest row to lock yet.
            db.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(recipient_user_id),))
            known = db.execute("""SELECT input_hash
                FROM ai_profile.recipient_input_versions
                WHERE recipient_user_id=%s AND source_version=%s""",
                (recipient_user_id, source_version)).fetchone()
            if known is not None and known[0] != input_hash:
                raise RecipientInputConflict("sourceVersion already has a different input")
            # Existing analyses can predate this additive migration. Their
            # hashes prevent an older first webhook from becoming the latest.
            latest_version = db.execute("""SELECT max(source_version)
                FROM ai_profile.recipient_input_versions WHERE recipient_user_id=%s""",
                (recipient_user_id,)).fetchone()[0]
            if known is None:
                db.execute("""INSERT INTO ai_profile.recipient_input_versions
                    (recipient_user_id,source_version,input_hash) VALUES (%s,%s,%s)""",
                    (recipient_user_id, source_version, input_hash))
            if latest_version is not None and source_version < latest_version:
                return "stale"
            latest = db.execute("""SELECT source_version,input_hash
                FROM ai_profile.recipient_inputs WHERE recipient_user_id=%s""",
                (recipient_user_id,)).fetchone()
            if latest is not None and latest == (source_version, input_hash):
                _recover_analysis(db, recipient_user_id, source_version)
                return "duplicate"
            db.execute("""INSERT INTO ai_profile.recipient_inputs
                    (recipient_user_id,source_version,input,input_hash,guarded_input,guard_version)
                VALUES (%s,%s,%s::jsonb,%s,%s::jsonb,%s)
                ON CONFLICT (recipient_user_id) DO UPDATE SET
                    source_version=excluded.source_version, input=excluded.input,
                    input_hash=excluded.input_hash, received_at=clock_timestamp(),
                    guarded_input=excluded.guarded_input, guard_version=excluded.guard_version
                WHERE ai_profile.recipient_inputs.source_version < excluded.source_version""",
                (recipient_user_id, source_version, encoded, input_hash,
                 json.dumps(guarded, ensure_ascii=False, allow_nan=False), guard.GUARD_VERSION))
            _recover_analysis(db, recipient_user_id, source_version)
            return "stored"

    def is_current(self, recipient_user_id: int, source_version: int, input_hash: str) -> bool:
        """Skip queued work superseded after its admission, without reading text."""
        with self._connect() as db:
            row = db.execute("""SELECT source_version,input_hash
                FROM ai_profile.recipient_inputs WHERE recipient_user_id=%s""",
                (recipient_user_id,)).fetchone()
        return row == (source_version, input_hash)

    def record_analysis(self, profile, tag_taxonomy=None) -> bool:
        """Retain the last successful analysis without upgrading its source version.

        Emmet's latest profile row also records failed/disabled tag extraction.
        Keeping success separately lets a reader distinguish a new raw input
        from an older successful analysis. A reader must require version equality
        before combining their contents. This method never creates raw input.
        """
        if profile.tag_status not in {"ok", "skip", "skip_no_signal"}:
            return False
        analysis = {
            "tagTaxonomy": tag_taxonomy,
            "sourceVersion": profile.source_version,
            "preferredTags": [tag.model_dump(mode="json") for tag in profile.preferred_tags],
            "dislikedTags": [tag.model_dump(mode="json") for tag in profile.disliked_tags],
            "tagStatus": profile.tag_status,
            "catalogVersion": str(profile.catalog_version_id) if profile.catalog_version_id is not None else None,
            "promptVersion": profile.prompt_version,
            "validatorVersion": profile.validator_version,
            "analyzedAt": (profile.updated_at or datetime.now(UTC)).isoformat(),
        }
        with self._connect() as db:
            result = db.execute("""UPDATE ai_profile.recipient_inputs
                SET successful_analysis=%s::jsonb
                WHERE recipient_user_id=%s AND source_version >= %s
                    AND (successful_analysis IS NULL OR
                        (successful_analysis->>'sourceVersion')::bigint < %s)
                    AND EXISTS (SELECT 1 FROM ai_profile.recipient_input_versions v
                        WHERE v.recipient_user_id=ai_profile.recipient_inputs.recipient_user_id
                            AND v.source_version=%s)""",
                (json.dumps(analysis, ensure_ascii=False, allow_nan=False),
                 profile.recipient_user_id, profile.source_version, profile.source_version, profile.source_version))
            return result.rowcount == 1


class RecordingRecipientStore:
    """Keep Emmet's profile writes and add the integration success receipt."""

    def __init__(self, store, recipient_data: RecipientInputStore, taxonomy=None):
        self.store = store
        self.recipient_data = recipient_data
        self.taxonomy = taxonomy

    def get(self, recipient_user_id: int):
        return self.store.get(recipient_user_id)

    def upsert(self, profile) -> None:
        self.store.upsert(profile)
        self.recipient_data.record_analysis(profile, self.taxonomy(profile) if self.taxonomy else None)
