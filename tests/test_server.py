"""The HTTP surface, exercised over a real socket.

Auth is the load-bearing behaviour here: the server used to treat an empty
token as "auth off", which turns a misconfigured deployment into an open LLM
proxy. Every route that spends money or writes data is tested both ways.
"""
import http.client
import json


def request(server, method, path, body=None, token=None, raw=None):
    conn = http.client.HTTPConnection(server["host"], server["port"], timeout=10)
    headers = {}
    payload = raw
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Length"] = str(len(payload))
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        parsed = json.loads(data.decode("utf-8")) if data else None
    except ValueError:
        parsed = data
    return resp.status, parsed


def test_health_is_public_and_reports_chunks(server):
    status, body = request(server, "GET", "/health")
    assert status == 200
    assert body["ok"] is True
    assert body["chunks"] > 0


def test_unknown_route_is_404(server):
    status, body = request(server, "GET", "/nope")
    assert status == 404
    assert body["error"] == "not found"


def test_chat_requires_a_token(server):
    status, body = request(server, "POST", "/chat", body={"question": "hi"})
    assert status == 401
    assert body["error"] == "unauthorized"


def test_chat_rejects_a_wrong_token(server):
    status, _ = request(server, "POST", "/chat", body={"question": "hi"},
                        token="not-the-token")
    assert status == 401


def test_deep_health_requires_a_token(server):
    # Deep health runs a real generation, so it costs money. It must never be
    # reachable anonymously.
    status, body = request(server, "GET", "/health/deep")
    assert status == 401
    assert body["error"] == "unauthorized"


def test_feedback_requires_a_token(server):
    status, _ = request(server, "POST", "/feedback", body={"id": "x", "vote": "up"})
    assert status == 401


def test_chat_rejects_an_empty_question(server):
    status, body = request(server, "POST", "/chat", body={"question": "   "},
                           token=server["token"])
    assert status == 400
    assert body["error"] == "empty question"


def test_chat_rejects_malformed_json(server):
    status, body = request(server, "POST", "/chat", raw=b"{not json",
                           token=server["token"])
    assert status == 400
    assert body["error"] == "invalid json"


def test_chat_returns_the_documented_shape(server, kb, monkeypatch):
    monkeypatch.setattr(kb, "answer_question",
                        lambda q, loc=None, mentioned=None: ("A grounded answer.", ["Notes/First 90 Days After Diagnosis.md"]))
    status, body = request(server, "POST", "/chat", body={"question": "what now"},
                           token=server["token"])
    assert status == 200
    assert set(body) == {"id", "answer", "sources", "citations",
                         "location", "location_ambiguous"}
    assert body["answer"] == "A grounded answer."
    assert len(body["id"]) == 12
    assert isinstance(body["citations"], list)


def test_chat_writes_a_qa_log_line(server, kb, monkeypatch):
    monkeypatch.setattr(kb, "answer_question",
                        lambda q, loc=None, mentioned=None: ("Logged answer.", []))
    status, body = request(server, "POST", "/chat", body={"question": "log me"},
                           token=server["token"])
    assert status == 200
    with open(kb.QA_LOG, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert any(r["id"] == body["id"] and r["question"] == "log me" for r in rows)


def test_chat_returns_502_when_generation_produced_nothing(server, kb, monkeypatch):
    monkeypatch.setattr(kb, "answer_question",
                        lambda q, loc=None, mentioned=None: ("", []))
    status, body = request(server, "POST", "/chat", body={"question": "anything"},
                           token=server["token"])
    assert status == 502
    assert "could not answer" in body["error"]


def test_chat_survives_a_generation_exception(server, kb, monkeypatch):
    def explode(q, loc=None, mentioned=None):
        raise RuntimeError("upstream blew up with a secret token sk-abc")
    monkeypatch.setattr(kb, "answer_question", explode)
    status, body = request(server, "POST", "/chat", body={"question": "boom"},
                           token=server["token"])
    assert status == 502
    # The client must not receive internal exception text.
    assert "sk-abc" not in json.dumps(body)


def test_feedback_rejects_an_unknown_vote(server):
    status, body = request(server, "POST", "/feedback",
                           body={"id": "abc", "vote": "meh"}, token=server["token"])
    assert status == 400
    assert "vote" in body["error"]


def test_feedback_records_a_valid_vote(server, kb):
    status, body = request(server, "POST", "/feedback",
                           body={"id": "abc123", "vote": "down", "note": "wrong state"},
                           token=server["token"])
    assert status == 200
    assert body == {"ok": True}
    with open(kb.FEEDBACK_LOG, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert rows[-1]["id"] == "abc123" and rows[-1]["vote"] == "down"


def test_oversized_body_is_rejected(server):
    big = b'{"question": "' + b"x" * 20000 + b'"}'
    status, body = request(server, "POST", "/chat", raw=big, token=server["token"])
    assert status == 400
