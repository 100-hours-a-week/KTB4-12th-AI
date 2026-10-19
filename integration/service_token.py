"""One BE credential shared by Chat and Profiler; Search/ledger auth stays separate.

Read once at the start of each HTTP authentication or outgoing request. An
accepted request finishes normally; later requests (including callbacks) read
the current atomic record. No old token is retained as a fallback.
"""
import hmac
import os
import re

from integration.provider_key import ProviderKey, KeyUnavailable, environment


KINDS = {"chat": "AI_SERVICE_TOKEN", "profiler": "PROFILING_SERVICE_TOKEN"}


class ServiceToken(ProviderKey):
    record_format = "be-service-token/2"
    legacy_files = ("be-service-token.json", "be-chat-token.json", "be-profiler-token.json")
    legacy_record_file = "be-service-token.json"
    legacy_record_format = "be-service-token/1"
    separated_files = ("be-shared-service-token.json", "be-chat-token.json", "be-profiler-token.json")

    def __init__(self, kind, **kwargs):
        if kind not in KINDS:
            raise KeyUnavailable("SERVICE_TOKEN_KIND_INVALID")
        super().__init__(kind=kind, **kwargs)
        self.record_file = "be-shared-service-token.json"

    @property
    def purpose(self):
        return "be-shared"

    @classmethod
    def from_environment(cls, kind, environ=None):
        env = os.environ if environ is None else environ
        legacy = KINDS[kind]
        directory = env.get("BE_SERVICE_TOKEN_DIR")
        shared = shared_token_configured(env)
        path = env.get("BE_SERVICE_TOKEN_FILE") if shared else env.get(kind.upper() + "_BE_SERVICE_TOKEN_FILE")
        value = env.get("BE_SERVICE_TOKEN") if shared else env.get(legacy)
        backend = env.get("AI_CREDENTIAL_BACKEND", "files")
        if backend not in {"files", "aws-secrets-manager"}:
            raise KeyUnavailable("CREDENTIAL_BACKEND_INVALID")
        aws_id = env.get("BE_SERVICE_TOKEN_SECRET_ARN") if backend == "aws-secrets-manager" else None
        if backend == "aws-secrets-manager" and (not aws_id or environment(env) == "local"):
            raise KeyUnavailable("AWS_CREDENTIAL_CONFIG_REQUIRED")
        old = any(env.get(name) or env.get(service.upper() + "_BE_SERVICE_TOKEN_FILE")
                  for service, name in KINDS.items()) if shared else False
        if aws_id and (directory or value or path) or shared and old:
            raise KeyUnavailable("SERVICE_TOKEN_SOURCE_CONFLICT")
        source = cls(kind, managed_directory=directory, external_file=path,
                     value=None if path else value, env=environment(env), aws_secret_id=aws_id,
                     layout=env.get("AI_CREDENTIAL_LAYOUT", "separate"))
        source.shared = shared
        return source

    def status(self):
        state = super().status()
        state["appliesTo"] = ["chat", "profiler"] if getattr(self, "shared", self.mode in {"managed", "aws"}) else [self.kind]
        return state

    def save(self, value, expected_revision, *, confirm_shared=False):
        return super().save(value, expected_revision, confirm_migration=confirm_shared)

    @staticmethod
    def valid_managed_key(value):
        # Existing Bearer contract accepts opaque tokens. Managed input is
        # bounded, printable ASCII without whitespace/control/header injection.
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{20,512}={0,2}", value):
            raise KeyUnavailable("SERVICE_TOKEN_INVALID")
        return value


def expected_token(kind):
    return ServiceToken.from_environment(kind).snapshot().value


def matches(kind, authorization):
    # Unavailable/corrupt/conflicting configuration always rejects the request.
    try:
        expected = expected_token(kind)
    except KeyUnavailable:
        return False
    return hmac.compare_digest(authorization.encode(), ("Bearer " + expected).encode())


def shared_token_configured(environ=None):
    env = os.environ if environ is None else environ
    return bool(env.get("BE_SERVICE_TOKEN_DIR") or env.get("BE_SERVICE_TOKEN")
                or env.get("BE_SERVICE_TOKEN_FILE") or env.get("AI_CREDENTIAL_BACKEND") == "aws-secrets-manager")


def internal_token(legacy):
    # Only the managed deployment separates internal refresh/read from BE APIs.
    return os.getenv("AI_INTERNAL_SERVICE_TOKEN", "") if shared_token_configured() else legacy
