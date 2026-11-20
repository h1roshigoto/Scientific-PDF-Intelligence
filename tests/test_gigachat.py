import json
import logging
import ssl
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from spi import gigachat, gigachat_smoke
from spi.gigachat import MODEL_ID, GigaChatClient, GigaChatError, GigaChatSettings
from spi.gigachat_smoke import _complete, _control


SETTINGS = GigaChatSettings(credentials=SecretStr("dGVzdDpmaXh0dXJl"))
ACTUAL_MODEL = MODEL_ID + ":3.0.1"


def oauth_response(request, *, token="mock-access-token", expiry=None):
    assert request.method == "POST"
    assert str(request.url) == gigachat.OAUTH_URL
    assert request.headers["Authorization"] == "Basic dGVzdDpmaXh0dXJl"
    assert UUID(request.headers["RqUID"]).version == 4
    assert parse_qs(request.content.decode()) == {"scope": ["GIGACHAT_API_PERS"]}
    return httpx.Response(200, json={
        "access_token": token,
        "expires_at": expiry if expiry is not None else int(gigachat.time.time()) + 1800,
    })


def completion_response(content, *, model=ACTUAL_MODEL, finish_reason="stop"):
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
    })


def answer_for(case):
    return {
        "raw_value": case.source.target.text,
        "raw_unit": "",
        "object_or_method": case.lookup.subject,
        "metric": case.lookup.metric,
        "context": "Table 1",
        "source_block_id": case.source.target.block_id,
        "source_quote": case.source.target.text,
    }


@pytest.mark.parametrize(
    ("scenario", "json_parsed", "quote_matches", "error"),
    [
        ("valid", True, True, None),
        ("invalid_json", False, None, "invalid_json"),
        ("missing_quote", True, None, "invalid_schema"),
        ("foreign_quote", True, False, "quote_mismatch"),
        ("wrong_model", False, None, "unexpected_model"),
        ("refused", False, None, "finish_blacklist"),
    ],
)
def test_completion_with_local_schema_and_evidence(scenario, json_parsed, quote_matches, error, caplog):
    case = _control(1)
    answer = answer_for(case)
    if scenario == "missing_quote":
        del answer["source_quote"]
    if scenario == "foreign_quote":
        answer["source_quote"] = "not in the source"

    def handle(request):
        if request.url.path.endswith("oauth"):
            return oauth_response(request)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer mock-access-token"
        body = json.loads(request.content)
        assert body["model"] == MODEL_ID
        assert body["max_tokens"] == 700
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["strict"] is True
        assert "source_quote" in body["response_format"]["schema"]["required"]
        return completion_response(
            "not JSON" if scenario == "invalid_json" else json.dumps(answer),
            model="GigaChat-2-Max" if scenario == "wrong_model" else ACTUAL_MODEL,
            finish_reason="blacklist" if scenario == "refused" else "stop",
        )

    with caplog.at_level(logging.INFO, logger="spi.gigachat"):
        with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
            observation = _complete(client, case, strict=True)
    assert observation["json_parsed"] is json_parsed
    assert observation["quote_matches"] is quote_matches
    assert observation.get("error") == error
    assert observation["status"] == ("verified" if error is None else "needs_review")
    assert observation["elapsed_ms"] >= 0
    assert "dGVzdDpmaXh0dXJl" not in caplog.text
    assert "mock-access-token" not in caplog.text


@pytest.mark.parametrize(("reason", "reported_reason", "code"), [
    ("length", "length", "finish_length"),
    ("blacklist", "blacklist", "finish_blacklist"),
    ("error", "error", "finish_error"),
    ("function_call", "function_call", "finish_function_call"),
    ("mock-access-token", "unknown", "unexpected_finish_reason"),
    (None, None, "unexpected_finish_reason"),
])
def test_unsuccessful_completion_reports_safe_metadata(reason, reported_reason, code, caplog):
    def handle(request):
        if request.url.path.endswith("oauth"):
            return oauth_response(request)
        assert json.loads(request.content)["max_tokens"] == 1800
        return httpx.Response(200, json={
            "model": ACTUAL_MODEL,
            "choices": [{"finish_reason": reason, "diagnostic": "private PDF text"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 1800, "mock-access-token": 4},
        })

    with caplog.at_level(logging.INFO, logger="spi.gigachat"):
        with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(GigaChatError) as caught:
                client.complete_json("private PDF text", max_tokens=1800)
    error = caught.value
    assert error.code == code
    assert gigachat.completion_diagnostics(error) == {
        "model_id": ACTUAL_MODEL, "finish_reason": reported_reason,
        "usage": {"prompt_tokens": 120, "completion_tokens": 1800},
        "completion_tokens": 1800, "requested_max_tokens": 1800, "retries": 0,
    }
    for sensitive in (SETTINGS.credentials.get_secret_value(), "mock-access-token", "private PDF text"):
        assert sensitive not in str(error) + caplog.text


@pytest.mark.parametrize("max_tokens", [0, -1, 4097, 1800.5, True])
def test_client_rejects_invalid_max_tokens_without_network(max_tokens):
    with GigaChatClient(SETTINGS, transport=httpx.MockTransport(lambda _: pytest.fail("sent request"))) as client:
        with pytest.raises(ValueError, match="max_tokens"):
            client.complete_json("prompt", max_tokens=max_tokens)


@pytest.mark.parametrize("stage", ["oauth", "models", "completion"])
@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retried(stage, status):
    calls = []

    def handle(request):
        operation = "oauth" if request.url.path.endswith("oauth") else "models"
        if request.url.path.endswith("completions"):
            operation = "completion"
        calls.append(operation)
        if operation == stage:
            return httpx.Response(status, text="sensitive response")
        return oauth_response(request)

    with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GigaChatError) as exc:
            if stage == "completion":
                client.complete_json("prompt")
            else:
                client.list_models()
    assert exc.value.status == status
    assert exc.value.stage == stage
    assert "sensitive response" not in str(exc.value)
    assert calls.count(stage) == 1


@pytest.mark.parametrize(("failures", "retry_after", "delays"), [
    (2, "1", [1, 1]),
    (3, None, [0.5, 1.0]),
    (3, "60", []),
])
def test_429_retries_are_bounded(monkeypatch, failures, retry_after, delays):
    slept = []
    calls = []
    monkeypatch.setattr(gigachat.time, "sleep", slept.append)

    def handle(request):
        if request.url.path.endswith("oauth"):
            return oauth_response(request)
        calls.append(request)
        if len(calls) <= failures:
            return httpx.Response(429, headers={"Retry-After": retry_after} if retry_after else {})
        return httpx.Response(200, json={"data": [{"id": MODEL_ID}]})

    with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
        if failures == 2:
            assert client.list_models() == [MODEL_ID]
        else:
            with pytest.raises(GigaChatError) as exc:
                client.list_models()
            assert exc.value.status == 429
    assert slept == delays
    assert len(calls) == 1 + len(delays)


def test_missing_credentials(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "")
    with pytest.raises(GigaChatError, match="missing_credentials"):
        GigaChatClient(transport=httpx.MockTransport(lambda _: pytest.fail("sent request")))


def test_tls_error_wrapped_by_transport_is_not_retried(monkeypatch):
    def handle(request):
        try:
            try:
                raise ssl.SSLCertVerificationError("untrusted certificate")
            except ssl.SSLCertVerificationError:
                raise OSError("TLS handshake failed")
        except OSError as exc:
            raise httpx.ConnectError("connection failed", request=request) from exc

    monkeypatch.setattr(gigachat.time, "sleep", lambda _: pytest.fail("retried a TLS error"))
    with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GigaChatError) as exc:
            client.list_models()
    assert exc.value.stage == "oauth"
    assert exc.value.code == "tls_error"


def test_connect_timeout_has_a_distinct_diagnostic_and_bounded_retries(monkeypatch):
    calls = []
    delays = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectTimeout("handshake failed", request=request)

    monkeypatch.setattr(gigachat.time, "sleep", delays.append)
    with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GigaChatError) as exc:
            client.list_models()
    assert exc.value.stage == "oauth"
    assert exc.value.code == "connect_timeout"
    assert "handshake failed" not in str(exc.value)
    assert len(calls) == 3
    assert delays == [0.5, 1.0]


@pytest.mark.parametrize("multiplier", [1, 1000])
def test_token_is_reused_and_refreshed_before_expiry(monkeypatch, multiplier):
    now = [1_800_000_000]
    monkeypatch.setattr(gigachat.time, "time", lambda: now[0])
    tokens = []
    used = []

    def handle(request):
        if request.url.path.endswith("oauth"):
            tokens.append(f"token-{len(tokens)}")
            return oauth_response(request, token=tokens[-1], expiry=(now[0] + 100) * multiplier)
        used.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": [{"id": MODEL_ID}]})

    with GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)) as client:
        client.list_models()
        now[0] += 69
        client.list_models()
        now[0] += 1
        client.list_models()
    assert used == ["Bearer token-0", "Bearer token-0", "Bearer token-1"]


def test_network_calls_are_serialized_across_instances():
    first_entered = Event()
    second_ready = Event()
    second_entered = Event()
    release = Event()

    def handle_first(request):
        first_entered.set()
        assert release.wait(5)
        return oauth_response(request) if request.url.path.endswith("oauth") else httpx.Response(
            200, json={"data": [{"id": MODEL_ID}]},
        )

    def handle_second(request):
        second_entered.set()
        return oauth_response(request) if request.url.path.endswith("oauth") else httpx.Response(
            200, json={"data": [{"id": MODEL_ID}]},
        )

    with (
        GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle_first)) as first,
        GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle_second)) as second,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        def call_second():
            second_ready.set()
            return second.list_models()

        one = pool.submit(first.list_models)
        try:
            assert first_entered.wait(5)
            two = pool.submit(call_second)
            assert second_ready.wait(5)
            assert not second_entered.wait(0.15)
        finally:
            release.set()
        assert one.result(timeout=5) == two.result(timeout=5) == [MODEL_ID]


def test_live_runner_falls_back_for_explicit_strict_rejection(monkeypatch):
    cases = [_control(index) for index in range(1, 4)]
    payloads = []

    def handle(request):
        if request.url.path.endswith("oauth"):
            return oauth_response(request)
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"data": [{"id": MODEL_ID}]})
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(422, json={"message": "Unsupported response_format"})
        return completion_response(json.dumps(answer_for(cases[len(payloads) - 1])))

    monkeypatch.setattr(
        gigachat_smoke, "GigaChatClient",
        lambda: GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)),
    )
    report = {"requests": [], "strict_schema": "not_checked"}
    gigachat_smoke._run_live(report, cases, json_only=False)
    assert report["oauth"] == "authorized"
    assert report["ultra_completion_succeeded"] is True
    assert report["strict_schema"] == "rejected_using_prompted_json"
    assert [item["status"] for item in report["requests"]] == ["needs_review", "verified", "verified"]
    assert ["response_format" in request for request in payloads] == [True, False, False]


def test_smoke_stops_after_a_non_stop_completion(monkeypatch):
    calls = []

    def handle(request):
        if request.url.path.endswith("oauth"):
            return oauth_response(request)
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"data": [{"id": MODEL_ID}]})
        calls.append(request)
        return completion_response("incomplete", finish_reason="length")

    monkeypatch.setattr(
        gigachat_smoke, "GigaChatClient",
        lambda: GigaChatClient(SETTINGS, transport=httpx.MockTransport(handle)),
    )
    report = {"requests": [], "strict_schema": "not_checked", "ultra_completion_succeeded": False}
    gigachat_smoke._run_live(report, [_control(1), _control(2)], json_only=False)
    assert len(calls) == 1
    assert report["ultra_completion_succeeded"] is False
    assert report["requests"][0]["finish_reason"] == "length"
    assert report["requests"][0]["error"] == "finish_length"
