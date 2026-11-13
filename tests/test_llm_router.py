"""LLM router: the two-tier fallback, tested without a network call.

`requests.post` is replaced with a fake, so these run offline and instant.
The behaviours worth locking down are the ones that decide whether a user gets
an answer at all: fall through to the paid tier on 429/5xx, do not fall
through on success, and report a machine-readable error when both tiers die.
"""
import llm_router
import pytest


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok_body(text="an answer", finish_reason="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish_reason}]}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(llm_router, "OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(llm_router, "MODEL_FREE", "free/model")
    monkeypatch.setattr(llm_router, "MODEL_PAID", "paid/model")


def test_chat_returns_text_and_finish_reason(monkeypatch):
    monkeypatch.setattr(llm_router.requests, "post",
                        lambda *a, **k: FakeResponse(200, ok_body("hello")))
    text, err, finish = llm_router.chat([{"role": "user", "content": "hi"}], "m")
    assert (text, err, finish) == ("hello", None, "stop")


def test_chat_reports_missing_api_key(monkeypatch):
    monkeypatch.setattr(llm_router, "OPENROUTER_API_KEY", "")
    text, err, _ = llm_router.chat([], "m")
    assert text == "" and "API_KEY" in err


def test_chat_reports_timeout(monkeypatch):
    def boom(*a, **k):
        raise llm_router.requests.exceptions.Timeout()
    monkeypatch.setattr(llm_router.requests, "post", boom)
    text, err, _ = llm_router.chat([], "m")
    assert text == "" and err == "timeout"


def test_chat_reports_rate_limit(monkeypatch):
    monkeypatch.setattr(llm_router.requests, "post",
                        lambda *a, **k: FakeResponse(429, None, "slow down"))
    text, err, _ = llm_router.chat([], "m")
    assert text == "" and err == "429"


def test_chat_flags_a_truncated_answer(monkeypatch):
    monkeypatch.setattr(llm_router.requests, "post",
                        lambda *a, **k: FakeResponse(200, ok_body("", "length")))
    text, err, finish = llm_router.chat([], "m")
    assert text == "" and err == "empty response" and finish == "length"


def test_retry_after_header_is_parsed():
    assert llm_router._retry_after_s(FakeResponse(headers={"retry-after": "3"})) == 3.0
    assert llm_router._retry_after_s(FakeResponse(headers={})) == 2.0
    assert llm_router._retry_after_s(FakeResponse(headers={"retry-after": "soon"})) == 2.0


def test_fallback_serves_from_the_paid_tier_on_429(monkeypatch):
    calls = []

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        calls.append(model)
        if model == "free/model":
            return FakeResponse(429, None, "rate limited")
        return FakeResponse(200, ok_body("paid answer"))

    monkeypatch.setattr(llm_router.requests, "post", post)
    result = llm_router.generate_with_fallback([{"role": "user", "content": "hi"}])
    assert calls == ["free/model", "paid/model"]
    assert result["text"] == "paid answer"
    assert result["served_by"] == "paid"
    assert result["error"] is None


def test_fallback_does_not_call_the_paid_tier_on_success(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["model"])
        return FakeResponse(200, ok_body("free answer"))

    monkeypatch.setattr(llm_router.requests, "post", post)
    result = llm_router.generate_with_fallback([])
    assert calls == ["free/model"]
    assert result["served_by"] == "free"


def test_fallback_reports_both_failures(monkeypatch):
    monkeypatch.setattr(llm_router.requests, "post",
                        lambda *a, **k: FakeResponse(503, None, "upstream down"))
    result = llm_router.generate_with_fallback([])
    assert result["served_by"] == "error"
    assert result["text"] == ""
    assert result["error"].startswith("free:") and "paid:" in result["error"]
