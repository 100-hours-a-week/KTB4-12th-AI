"""Modal submission/result boundaries, using synthetic credentials and HTTP only."""
import httpx
import pytest

from profiling.v3.client import CONNECT_TRIES, MAX_REDIRECTS, Deadline, V3Client, V3Unavailable

BASE = "https://model.example"
AUTH = {"Authorization": "Bearer synthetic-app-key", "Modal-Key": "synthetic-proxy-id",
        "Modal-Secret": "synthetic-proxy-secret"}


@pytest.fixture
def make_client():
    clients = []

    def make(handler, *, base_url=BASE, sleep=lambda _: None):
        client = V3Client(base_url, "synthetic-app-key", proxy_key=AUTH["Modal-Key"],
                          proxy_secret=AUTH["Modal-Secret"], transport=httpx.MockTransport(handler), sleep=sleep)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.mark.parametrize(("location", "kind"), [
    ("https://other.example/result", "unsafe_redirect"),
    ("//other.example/result", "unsafe_redirect"),
    ("https://model.example:444/result", "unsafe_redirect"),
    ("http://model.example/result", "unsafe_redirect"),
    ("https://user:synthetic-url-secret@model.example/result", "unsafe_redirect"),
    ("https://model.example:invalid/result", "connect"),  # HTTPX rejects the response before returning it.
])
def test_unsafe_redirect_never_sends_a_second_request(make_client, location, kind):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(303, headers={"Location": location})

    client = make_client(handler)
    with pytest.raises(V3Unavailable) as error:
        client.post_json("/v3/tags", {"text": "synthetic profile"}, Deadline(10))
    assert error.value.kind == kind
    assert [r.method for r in requests] == ["POST"]
    assert "synthetic-url-secret" not in str(error.value)
    assert all(value not in str(error.value) for value in AUTH.values())


@pytest.mark.parametrize("status", [307, 308])
def test_redirect_cannot_replay_a_post_even_on_same_origin(make_client, status):
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(status, headers={"Location": "/moved"})

    with pytest.raises(V3Unavailable, match="unsafe_redirect"):
        make_client(handler).post_json("/v3/tags", {}, Deadline(10))
    assert methods == ["POST"]


@pytest.mark.parametrize("location", ["/v3/tags?__modal_function_call_id=synthetic-job",
                                       "https://MODEL.example:443/v3/tags?__modal_function_call_id=synthetic-job"])
def test_same_origin_result_get_retains_auth_without_request_body(make_client, location):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(303, headers={"Location": location})
        return httpx.Response(200, json={"tags": []})

    result, _ = make_client(handler).post_json("/v3/tags", {"text": "synthetic profile"}, Deadline(10))
    assert result == {"tags": []}
    assert [r.method for r in requests] == ["POST", "GET"]
    assert requests[1].content == b""
    assert requests[1].url.params["__modal_function_call_id"] == "synthetic-job"
    assert all(requests[1].headers[k] == v for k, v in AUTH.items())


@pytest.mark.parametrize("status", [307, 308])
def test_same_origin_get_redirect_can_continue_result_lookup(make_client, status):
    methods = []

    def handler(request):
        methods.append(request.method)
        if len(methods) == 1:
            return httpx.Response(303, headers={"Location": "/result/accepted-job"})
        if len(methods) == 2:
            return httpx.Response(status, headers={"Location": "/result/current"})
        return httpx.Response(200, json={"tags": []})

    result, _ = make_client(handler).post_json("/v3/tags", {}, Deadline(10))
    assert result == {"tags": []}
    assert methods == ["POST", "GET", "GET"]


@pytest.mark.parametrize("failure", ["transport", 502, 503, 504])
def test_result_lookup_retry_never_resubmits_inference(make_client, failure):
    requests = []
    gets = 0

    def handler(request):
        nonlocal gets
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(303, headers={"Location": "/result/accepted-job"})
        gets += 1
        if gets == 1:
            if failure == "transport":
                raise httpx.ConnectError("synthetic lookup error", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={"tags": []})

    result, _ = make_client(handler).post_json("/v3/tags", {}, Deadline(10))
    assert result == {"tags": []}
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    assert requests[1].url == requests[2].url
    assert requests[2].content == b""
    assert all(requests[2].headers[k] == v for k, v in AUTH.items())


def test_repeated_result_connection_errors_remain_bounded(make_client):
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(303, headers={"Location": "/result/accepted-job"})
        raise httpx.ConnectError("synthetic lookup error", request=request)

    with pytest.raises(V3Unavailable, match="connect"):
        make_client(handler).post_json("/v3/tags", {}, Deadline(10))
    assert methods == ["POST"] + ["GET"] * CONNECT_TRIES


def test_result_retry_preserves_original_deadline(make_client):
    methods = []
    deadline = Deadline(10)

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(303, headers={"Location": "/result/accepted-job"})
        return httpx.Response(503)

    def exhaust_deadline(_):
        deadline.end = 0

    with pytest.raises(V3Unavailable, match="timeout"):
        make_client(handler, sleep=exhaust_deadline).post_json("/v3/tags", {}, deadline)
    assert methods == ["POST", "GET"]


def test_redirect_limit_survives_result_lookup_retries(make_client):
    methods = []
    failed_once = False

    def handler(request):
        nonlocal failed_once
        methods.append(request.method)
        if request.method == "GET" and not failed_once:
            failed_once = True
            return httpx.Response(503)
        return httpx.Response(303, headers={"Location": "/result/accepted-job"})

    with pytest.raises(V3Unavailable, match="timeout"):
        make_client(handler).post_json("/v3/tags", {}, Deadline(10))
    assert methods.count("POST") == 1
    assert len(methods) == MAX_REDIRECTS + 2


@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError])
def test_ambiguous_post_transport_failure_is_not_replayed(make_client, error_type):
    methods = []

    def handler(request):
        methods.append(request.method)
        raise error_type("synthetic transport failure", request=request)

    with pytest.raises(V3Unavailable, match="connect"):
        make_client(handler).post_json("/v3/tags", {}, Deadline(10))
    assert methods == ["POST"]


@pytest.mark.parametrize("url", [
    "ftp://model.example", "https:///missing-host", "https://model.example:0",
    "https://user:synthetic-url-secret@model.example", "https://model.example?key=synthetic-url-secret",
    "https://model.example#synthetic-url-secret", "http://model.example", "https://model.example:invalid",
])
def test_invalid_authenticated_endpoint_fails_without_exposing_url(make_client, url):
    with pytest.raises(V3Unavailable) as error:
        make_client(lambda _: pytest.fail("must not send"), base_url=url)
    assert error.value.kind == "invalid_url"
    assert "synthetic-url-secret" not in str(error.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"])
def test_loopback_http_remains_available_for_internal_servers(make_client, url):
    result, _ = make_client(lambda _: httpx.Response(200, json={"tags": []}), base_url=url).post_json(
        "/v3/tags", {}, Deadline(10))
    assert result == {"tags": []}


def test_absolute_initial_request_cannot_escape_configured_origin(make_client):
    client = make_client(lambda _: pytest.fail("must not send"))
    with pytest.raises(V3Unavailable, match="unsafe_redirect"):
        client.post_json("https://other.example/v3/tags", {}, Deadline(10))
