"""Credential boundary regressions without a gateway or machine-specific resume state."""
import pytest
from integration.credentials import CREDENTIAL_NAMES, validate_credentials, validate_profiler_auth


@pytest.mark.parametrize("token", [None, "", "   ", "\t"])
def test_required_profiler_needs_explicit_nonblank_token(token):
    for provider, required in [("openrouter", "false"), ("none", "true")]:
        with pytest.raises(ValueError):
            validate_profiler_auth(
                {
                    "PROFILING_TAGGER": provider,
                    "PROFILING_TAGGER_REQUIRED": required,
                    "PROFILING_SERVICE_TOKEN": token,
                }
            )


def test_nonblank_live_credentials_and_free_development_are_distinct():
    validate_credentials({key: "synthetic-value" for key in CREDENTIAL_NAMES})
    with pytest.raises(ValueError):
        validate_credentials(
            {
                key: "   " if key == "AI_SERVICE_TOKEN" else "synthetic"
                for key in CREDENTIAL_NAMES
            }
        )
    validate_profiler_auth(
        {
            "PROFILING_TAGGER": "openrouter",
            "PROFILING_TAGGER_REQUIRED": "true",
            "PROFILING_SERVICE_TOKEN": "synthetic",
        }
    )
    validate_profiler_auth(
        {"PROFILING_TAGGER": "none", "PROFILING_TAGGER_REQUIRED": "false"}
    )
