"""Real integrated Profiler + Node/DB, injected synthetic provider transport only."""

import json, os
from pathlib import Path
import httpx
import integration.profiler_runtime as runtime

MODE = Path(os.environ["EMMET_FIXTURE_MODE"])
Real = runtime.ProviderLimitedExtractor


def provider(request):
    mode = json.loads(MODE.read_text())["mode"]
    if mode == "timeout":
        raise httpx.ReadTimeout("SYNTHETIC timeout")
    if mode == "error":
        return httpx.Response(429, text="PRIVATE_SYNTHETIC_PROVIDER_RESPONSE")
    if mode == "bad_json":
        return httpx.Response(200, text="PRIVATE_SYNTHETIC_PROVIDER_RESPONSE")
    body = json.loads(request.content)
    assert isinstance(body["messages"], list) and body["messages"]
    assert body["response_format"]["type"] == "json_schema"
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": '{"tags":[]}'}, "finish_reason": "stop"}
            ],
            "usage": {}
            if mode == "missing_usage"
            else {"cost": 0.0001, "prompt_tokens": 10, "completion_tokens": 4},
        },
    )


def factory(*args, **kwargs):
    return Real(*args, **kwargs, transport=httpx.MockTransport(provider))


runtime.ProviderLimitedExtractor = factory
app = runtime.app


@app.post("/fixture/stop-encoder")
def stop_encoder():
    app.state.embedding.close()
    return {"synthetic": True, "encoderStopped": True}
