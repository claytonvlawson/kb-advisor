"""Crisis regexes are client config, so they get both a static and a runtime guard.

Measured motivation: `(\\w+\\s?)+[.!?]` takes 7 ms on 16 characters, 69 ms on 20
and 951 ms on 24, against a 500-character question cap and a server that used to
run the match inline on the request thread.
"""
import json
import os
import re
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "kb"))

import regex_safety  # noqa: E402


def example_patterns():
    path = os.path.join(ROOT, "clients", "example-parent-advisor", "crisis.json")
    return json.load(open(path))["patterns"]


@pytest.fixture(autouse=True)
def clean_state():
    regex_safety.reset_state()
    yield
    regex_safety.reset_state()


class TestStaticGuard:
    def test_rejects_nested_quantifier(self):
        reason = regex_safety.find_hazard(r"(\w+\s?)+[.!?]")
        assert reason and "nested quantifier" in reason

    @pytest.mark.parametrize("pattern", [
        r"(a+)+b",
        r"([a-z]+)*$",
        r"(\w+)+\b",
        r"(?:x+\d?)+y",
    ])
    def test_rejects_variants(self, pattern):
        assert regex_safety.find_hazard(pattern) is not None

    def test_rejects_overlapping_alternatives(self):
        reason = regex_safety.find_hazard(r"(?:ab|a)+c")
        assert reason and "overlapping alternatives" in reason

    @pytest.mark.parametrize("pattern", [
        r"\bsuicid(e|al)\b",
        r"self[\s-]?harm",
        r"(?:kill|hurt)\s+myself",
        r"\bwant(s|ed)?\s+to\s+die\b",
        r"\bwish(es|ed)?\s+(he|she|they|i)\s+(had\s+)?never\s+(been\s+)?born\b",
    ])
    def test_accepts_ordinary_patterns(self, pattern):
        assert regex_safety.find_hazard(pattern) is None

    def test_accepts_the_shipped_client(self):
        """The example client must keep loading; a false reject breaks a client."""
        assert regex_safety.compile_patterns(example_patterns()) is not None

    def test_error_names_the_offending_pattern(self):
        bad = r"\bok\b.*(\w+\s?)+[.!?]"
        with pytest.raises(regex_safety.RiskyPatternError) as excinfo:
            regex_safety.compile_patterns([r"\bok\b", bad])
        # .pattern carries it unescaped; the message uses %r, so compare there
        assert excinfo.value.pattern == bad
        assert "nested quantifier" in str(excinfo.value)

    def test_empty_set_compiles_to_none(self):
        assert regex_safety.compile_patterns([]) is None
        assert regex_safety.compile_patterns(["", "  "]) is None

    def test_unbalanced_paren_is_caught_before_compiling(self):
        with pytest.raises(regex_safety.RiskyPatternError) as excinfo:
            regex_safety.compile_patterns([r"unclosed("])
        assert "unbalanced" in str(excinfo.value)

    def test_invalid_syntax_reports_compile_error(self):
        # [z-a] is a bad range: structurally fine, rejected by re.compile
        with pytest.raises(regex_safety.RiskyPatternError) as excinfo:
            regex_safety.compile_patterns([r"[z-a]"])
        assert "does not compile" in str(excinfo.value)


class SlowRegex:
    """A stand-in for a pattern the static check could not prove risky."""

    def __init__(self, delay):
        self.delay = delay

    def search(self, text):
        time.sleep(self.delay)
        return None


class TestRuntimeBound:
    def test_benign_match_is_fast_and_correct(self):
        rx = regex_safety.compile_patterns(example_patterns())
        started = time.perf_counter()
        assert regex_safety.bounded_search(rx, "my son said the word suicide yesterday") is True
        assert regex_safety.bounded_search(rx, "how do I teach him to read") is False
        assert time.perf_counter() - started < 0.2

    def test_timeout_returns_none_inside_the_bound(self):
        started = time.perf_counter()
        hit = regex_safety.bounded_search(SlowRegex(5.0), "anything", timeout_s=0.1)
        elapsed = time.perf_counter() - started
        assert hit is None
        assert elapsed < 0.5, "bounded_search blocked for %.3fs" % elapsed

    def test_one_timeout_disables_the_detector(self):
        assert regex_safety.detector_disabled() is False
        assert regex_safety.bounded_search(SlowRegex(5.0), "x", timeout_s=0.05) is None
        assert regex_safety.detector_disabled() is True
        # further calls return immediately instead of leaking another thread
        started = time.perf_counter()
        assert regex_safety.bounded_search(SlowRegex(5.0), "x", timeout_s=5.0) is False
        assert time.perf_counter() - started < 0.1

    def test_none_regex_is_not_a_match(self):
        assert regex_safety.bounded_search(None, "anything") is False

    def test_raising_regex_is_not_a_match(self):
        class Boom:
            def search(self, text):
                raise re.error("boom")

        assert regex_safety.bounded_search(Boom(), "x") is False


class TestServerUsesTheGuards:
    def test_is_crisis_fails_closed_on_timeout(self, kb, monkeypatch):
        monkeypatch.setattr(kb.regex_safety, "bounded_search", lambda *a, **k: None)
        assert kb.is_crisis("anything at all") is True

    def test_is_crisis_false_when_no_patterns(self, kb, monkeypatch):
        monkeypatch.setattr(kb, "_CRISIS_RE", None)
        assert kb.is_crisis("anything at all") is False

    def test_server_is_the_bounded_class(self, kb):
        from http.server import ThreadingHTTPServer
        assert issubclass(kb.BoundedThreadingHTTPServer, ThreadingHTTPServer)
        assert callable(kb.main)

    def test_concurrent_handlers_are_capped(self, kb):
        """Six simultaneous slow requests, a cap of two, all six still served."""
        import http.client
        from http.server import BaseHTTPRequestHandler

        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                with lock:
                    active["now"] += 1
                    active["peak"] = max(active["peak"], active["now"])
                time.sleep(0.15)
                with lock:
                    active["now"] -= 1
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        httpd = kb.BoundedThreadingHTTPServer(("127.0.0.1", 0), Slow, max_workers=2)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        statuses = []

        def hit():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("GET", "/")
                resp = conn.getresponse()
                resp.read()
                statuses.append(resp.status)
            finally:
                conn.close()

        workers = [threading.Thread(target=hit) for _ in range(6)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(15)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

        assert statuses == [200] * 6, statuses
        assert active["peak"] <= 2, "cap of 2 exceeded: %d concurrent" % active["peak"]
        assert active["peak"] >= 1
