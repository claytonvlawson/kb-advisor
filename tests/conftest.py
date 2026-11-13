"""Shared fixtures: build an index once, then run a real server against it.

kb_server.py reads its configuration at import time (including loading the
BM25 pickle), so the environment has to be set and the index has to exist
before the module is imported. Every fixture here is session-scoped and the
server fixture is the only place `import kb_server` happens.
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.join(ROOT, "kb")
CLIENT_DIR = os.path.join(ROOT, "clients", "example-parent-advisor")
VAULT_DIR = os.path.join(CLIENT_DIR, "vault")
TEST_TOKEN = "test-token-41f1c8a2"

# Importing the kb/ modules directly (retrieval, citations, llm_router) is how
# the server itself imports them: kb/ is on sys.path, not a package.
sys.path.insert(0, KB_DIR)
sys.path.insert(0, CLIENT_DIR)


@pytest.fixture(scope="session")
def retrieval_mod():
    import retrieval
    return retrieval


@pytest.fixture(scope="session")
def vault_dir():
    """The markdown vault shipped with the example client."""
    assert os.path.isdir(VAULT_DIR), "example client vault is missing"
    return VAULT_DIR


@pytest.fixture(scope="session")
def index_path(tmp_path_factory, retrieval_mod):
    """A BM25 index built from the shipped example vault, in a temp dir."""
    out = tmp_path_factory.mktemp("index") / "bm25.pkl"
    built = retrieval_mod.build_index(VAULT_DIR, str(out))
    assert built > 0, "example vault produced no chunks"
    return str(out)


@pytest.fixture(scope="session")
def kb(index_path, tmp_path_factory):
    """The kb_server module, configured against the temp index and a temp log dir."""
    log_dir = tmp_path_factory.mktemp("logs")
    os.environ.update({
        "ADVISOR_CLIENT": CLIENT_DIR,
        "ADVISOR_VAULT": VAULT_DIR,
        "ADVISOR_INDEX": index_path,
        "ADVISOR_TOKEN": TEST_TOKEN,
        "ADVISOR_ALLOW_ANON": "0",
        "ADVISOR_LOG_DIR": str(log_dir),
        "OPENROUTER_API_KEY": "",
        # Keep the spend controls out of the way here; test_ratelimit.py sets
        # them per test.
        "ADVISOR_RATE_PER_MIN": "1000",
        "ADVISOR_MAX_GENERATIONS": "8",
    })
    import kb_server
    kb_server.LOG_DIR = str(log_dir)
    kb_server.QA_LOG = os.path.join(str(log_dir), "qa_log.jsonl")
    kb_server.FEEDBACK_LOG = os.path.join(str(log_dir), "feedback.jsonl")
    kb_server.CRISIS_LOG = os.path.join(str(log_dir), "crisis_log.jsonl")
    return kb_server


@pytest.fixture(scope="session")
def server(kb):
    """A live ThreadingHTTPServer on an ephemeral port, torn down after the run.

    The HTTP surface is what a reviewer or an integrator actually touches, so
    it is tested over a socket rather than by calling handler methods.
    """
    # The bounded subclass, not the stdlib one: the suite should exercise the
    # server type the deployment actually runs.
    httpd = kb.BoundedThreadingHTTPServer(("127.0.0.1", 0), kb.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": httpd.server_address[1], "token": TEST_TOKEN}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
