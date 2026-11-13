"""Spend and pressure are bounded server-side, not only at the edge.

The frontend proxy's limiter lives in a serverless instance's memory: it resets on
cold start, is not shared, and does not exist for anything talking to the port.
These tests cover the control that actually holds.
"""
import http.client
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "kb"))

import ratelimit  # noqa: E402


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestTokenBucket:
    def test_burst_then_denied(self):
        lim = ratelimit.TokenBucketLimiter(rate_per_min=6, clock=FakeClock())
        allowed = [lim.allow("a")[0] for _ in range(6)]
        assert all(allowed)
        ok, retry_after = lim.allow("a")
        assert ok is False
        assert retry_after > 0

    def test_refills_over_time(self):
        clock = FakeClock()
        lim = ratelimit.TokenBucketLimiter(rate_per_min=60, clock=clock)  # 1/s
        for _ in range(60):
            assert lim.allow("a")[0]
        assert lim.allow("a")[0] is False
        clock.advance(1.0)
        assert lim.allow("a")[0] is True

    def test_identities_are_independent(self):
        lim = ratelimit.TokenBucketLimiter(rate_per_min=2, clock=FakeClock())
        assert lim.allow("a")[0] and lim.allow("a")[0]
        assert lim.allow("a")[0] is False
        assert lim.allow("b")[0] is True

    def test_zero_rate_disables_the_limiter(self):
        lim = ratelimit.TokenBucketLimiter(rate_per_min=0, clock=FakeClock())
        assert lim.enabled is False
        for _ in range(1000):
            assert lim.allow("a")[0] is True

    def test_tracked_identities_stay_bounded(self):
        """An unbounded dict keyed by client identity is itself a DoS vector."""
        clock = FakeClock()
        lim = ratelimit.TokenBucketLimiter(rate_per_min=60, max_identities=50, clock=clock)
        for i in range(500):
            lim.allow("id-%d" % i)
        assert lim.tracked() <= 50, lim.tracked()

    def test_retry_after_is_bounded(self):
        lim = ratelimit.TokenBucketLimiter(rate_per_min=1, clock=FakeClock())
        lim.allow("a")
        ok, retry_after = lim.allow("a")
        assert ok is False
        assert 1.0 <= retry_after <= 3600.0


class TestConcurrencyGate:
    def test_limits_in_flight_and_sheds_the_rest(self):
        gate = ratelimit.ConcurrencyGate(limit=2, wait_s=0.05)
        entered = []
        with gate:
            entered.append("a")
            with gate:
                entered.append("b")
                with pytest.raises(ratelimit.BusyError):
                    with gate:
                        entered.append("c")
        assert entered == ["a", "b"]
        assert gate.rejected == 1

    def test_slots_are_released(self):
        gate = ratelimit.ConcurrencyGate(limit=1, wait_s=0.05)
        for _ in range(5):
            with gate:
                assert gate.in_flight == 1
        assert gate.in_flight == 0

    def test_released_even_on_exception(self):
        gate = ratelimit.ConcurrencyGate(limit=1, wait_s=0.05)
        with pytest.raises(RuntimeError):
            with gate:
                raise RuntimeError("boom")
        assert gate.in_flight == 0
        with gate:
            assert gate.in_flight == 1


def post(server, path, payload, headers=None):
    conn = http.client.HTTPConnection(server["host"], server["port"], timeout=10)
    try:
        hdrs = {"Content-Type": "application/json",
                "Authorization": "Bearer " + server["token"]}
        hdrs.update(headers or {})
        conn.request("POST", path, body=json.dumps(payload), headers=hdrs)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


class TestServerEnforcesIt:
    def test_429_with_retry_after_once_the_bucket_is_empty(self, kb, server, monkeypatch):
        monkeypatch.setattr(kb, "LIMITER", ratelimit.TokenBucketLimiter(rate_per_min=2))
        statuses = [post(server, "/feedback", {"id": "x", "vote": "up"})[0] for _ in range(3)]
        assert statuses[:2] == [200, 200], statuses
        status, headers, body = post(server, "/feedback", {"id": "x", "vote": "up"})
        assert status == 429
        assert "Retry-After" in headers
        assert "rate limited" in json.loads(body)["error"]

    def test_forwarded_header_is_ignored_without_trust(self, kb, server, monkeypatch):
        monkeypatch.setattr(kb, "TRUST_PROXY", False)
        monkeypatch.setattr(kb, "LIMITER", ratelimit.TokenBucketLimiter(rate_per_min=1))
        first = post(server, "/feedback", {"id": "x", "vote": "up"},
                     {"X-Forwarded-For": "1.2.3.4"})[0]
        second = post(server, "/feedback", {"id": "x", "vote": "up"},
                      {"X-Forwarded-For": "5.6.7.8"})[0]
        assert first == 200
        assert second == 429, "a client-supplied header must not buy a new bucket"

    def test_forwarded_header_is_honoured_with_trust(self, kb, server, monkeypatch):
        monkeypatch.setattr(kb, "TRUST_PROXY", True)
        monkeypatch.setattr(kb, "LIMITER", ratelimit.TokenBucketLimiter(rate_per_min=1))
        first = post(server, "/feedback", {"id": "x", "vote": "up"},
                     {"X-Forwarded-For": "1.2.3.4"})[0]
        second = post(server, "/feedback", {"id": "x", "vote": "up"},
                      {"X-Forwarded-For": "5.6.7.8"})[0]
        assert (first, second) == (200, 200), "separate clients need separate buckets"

    def test_auth_still_comes_first(self, kb, server, monkeypatch):
        monkeypatch.setattr(kb, "LIMITER", ratelimit.TokenBucketLimiter(rate_per_min=1))
        conn = http.client.HTTPConnection(server["host"], server["port"], timeout=10)
        try:
            conn.request("POST", "/chat", body='{"question":"hi"}',
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 401, "auth must not be bypassable by exhausting a bucket"
        finally:
            conn.close()

    def test_busy_returns_503_when_generations_are_saturated(self, kb, server, monkeypatch):
        gate = ratelimit.ConcurrencyGate(limit=1, wait_s=0.05)
        gate.__enter__()                       # occupy the only slot
        monkeypatch.setattr(kb, "GENERATIONS", gate)
        try:
            status, headers, body = post(server, "/chat", {"question": "hello"})
        finally:
            gate.__exit__(None, None, None)
        assert status == 503
        assert "Retry-After" in headers
        assert json.loads(body)["error"].startswith("busy")
