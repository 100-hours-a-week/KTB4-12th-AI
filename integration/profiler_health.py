"""Health shape bridge: Emmet llm metadata, legacy fixtures and integration-owned requirements."""


def model_state(health):
    if isinstance(health.get("llm"), dict):
        value = health["llm"]
        guard = health.get("integration", {})
        return {
            "enabled": value.get("enabled") is True,
            "model": value.get("model"),
            "required": guard.get("modelRequired") is True,
            "state": value.get("state"),
        }
    return health.get("tagger", {})
