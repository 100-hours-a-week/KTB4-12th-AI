"""Credential checks for explicit synthetic tests, without a ledger."""
CREDENTIAL_NAMES = (
    "AI_SERVICE_TOKEN",
    "SESSION_SECRET",
    "QA_ADMIN_PASSWORD",
    "DEMO_ACCESS_TOKEN",
)

def validate_credentials(values):
    if any(
        not isinstance(values.get(key), str) or not values[key].strip()
        for key in CREDENTIAL_NAMES
    ):
        raise ValueError("Existing nonblank named credentials required")

def validate_profiler_auth(variables):
    required = variables.get("PROFILING_TAGGER_REQUIRED", "").lower() == "true"
    if required or variables.get("PROFILING_TAGGER") == "openrouter":
        token = variables.get("PROFILING_SERVICE_TOKEN")
        if not isinstance(token, str) or not token.strip():
            raise ValueError(
                "Live/required Profiler needs an explicit nonblank service token"
            )
