"""Crisis routing: the safety path must fire on the parent's words.

Design rule under test: crisis handling is a deterministic injection, not a
ranking boost. A regex decides, and the safety note's chunks are prepended to
the context regardless of BM25 score. A false positive still answers the real
question; a false negative is silent, which is why the positive triggers are
tested against real parent phrasing rather than keyword lists.
"""
import pytest

CRISIS_TRIGGERS = [
    "I'm scared, he wishes he had never been born",
    "my kid is talking about self harm tonight",
    "sometimes I think about suicide",
    "she said she wants to die",
    "he has been cutting himself again",
    "there is no reason to live for her",
    "my child is not safe at home",
]


NON_TRIGGERS = [
    "what does the literature say about language outcomes",
    "how do I get an IEP evaluation started",
    "which audiologist do you recommend in Ohio",
    "my son got a cochlear implant last month, what now",
]


@pytest.mark.parametrize("question", CRISIS_TRIGGERS)
def test_crisis_fires_on_parent_phrasing(kb, question):
    assert kb.is_crisis(question) is True


@pytest.mark.parametrize("question", NON_TRIGGERS)
def test_crisis_stays_silent_on_ordinary_questions(kb, question):
    assert kb.is_crisis(question) is False


def test_crisis_config_loaded_from_client(kb):
    assert kb.CRISIS_CFG is not None
    assert kb.CRISIS_CFG["inject_source"].endswith("01 Agent Scope and Guardrails.md")
    assert kb.CRISIS_CFG["system_addendum"].strip()


def test_inject_source_is_present_in_the_index(kb):
    # If the inject source is missing, the crisis path has nothing to prepend
    # and fails open. The server warns at boot; this makes it a test failure.
    assert kb._crisis_chunks(), "crisis inject source was not indexed"


def test_crisis_chunks_are_prepended_before_normal_hits(kb):
    hits, crisis, _local, _ask = kb.gather_context(
        "I'm scared, he wishes he had never been born")
    assert crisis is True
    assert hits
    assert hits[0]["source"] == kb.CRISIS_CFG["inject_source"]


def test_ordinary_question_does_not_lead_with_the_safety_note(kb):
    hits, crisis, _local, _ask = kb.gather_context(
        "what does the literature say about reading outcomes")
    assert crisis is False
    assert hits
    assert hits[0]["source"] != kb.CRISIS_CFG["inject_source"]


def test_empty_patterns_disable_the_crisis_path(kb, tmp_path):
    client = tmp_path / "empty-client"
    client.mkdir()
    (client / "crisis.json").write_text(
        '{"patterns": [], "inject_source": "", "system_addendum": ""}',
        encoding="utf-8")
    assert kb.load_crisis_config(str(client)) is None


def test_patterns_without_inject_source_are_rejected(kb, tmp_path):
    client = tmp_path / "bad-client"
    client.mkdir()
    (client / "crisis.json").write_text(
        '{"patterns": ["suicide"], "inject_source": ""}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="no inject_source"):
        kb.load_crisis_config(str(client))


def test_client_without_system_prompt_is_rejected(kb, tmp_path):
    client = tmp_path / "bare-client"
    client.mkdir()
    with pytest.raises(RuntimeError, match="no system_prompt.md"):
        kb.load_system_prompt(str(client))
