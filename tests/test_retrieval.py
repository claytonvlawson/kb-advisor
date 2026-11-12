"""Retrieval: tokenizer, chunker, index round trip, ranking.

These run offline and take about a second. They exist because the tokenizer
and chunker are copied from another codebase (jetson-brain) and must stay
byte-compatible: a query tokenized differently from the index is the failure
mode that makes a whole corpus silently unsearchable.
"""
import os

import pytest


def test_tokenizer_stems_and_strips_accents(retrieval_mod):
    toks = retrieval_mod.tokenize("Notes Café RUNNING résumés")
    assert "note" in toks           # trailing -s stemmed
    assert "resume" in toks         # NFKD folds the accent, then -s stemmed
    assert "cafe" in toks
    assert all(t == t.lower() for t in toks)


def test_tokenizer_is_suffix_only_by_design(retrieval_mod):
    # The stemmer is deliberately dumb (and identical to the indexer's copy):
    # irregular plurals such as "children" are not reduced, so aliases in
    # system_prompt.md and the vault have to cover them.
    assert retrieval_mod.tokenize("children") == ["children"]


def test_tokenizer_flattens_acronyms(retrieval_mod):
    # "U.S.A." must tokenize the way the index expects, not as "u" "s" "a"
    assert "usa" in retrieval_mod.tokenize("the U.S.A. program")


def test_split_frontmatter_returns_pair(retrieval_mod):
    raw = "---\nname: note\ntype: source\n---\n# Title\n\nbody text\n"
    fm, body = retrieval_mod.split_frontmatter(raw)
    assert "name: note" in fm
    assert body.startswith("# Title")
    assert "---" not in body


def test_build_index_writes_pickle_and_counts(index_path, retrieval_mod):
    assert os.path.exists(index_path)
    chunks, bm25 = retrieval_mod.load_index(index_path)
    assert len(chunks) > 0
    assert bm25 is not None
    assert all({"text", "source", "title"} <= set(c) for c in chunks)


def test_load_index_rejects_wrong_tokenizer(index_path, retrieval_mod, tmp_path):
    import pickle

    with open(index_path, "rb") as fh:
        data = pickle.load(fh)
    data["tokenizer_version"] = "some-older-tokenizer"
    bad = tmp_path / "bad.pkl"
    with open(bad, "wb") as fh:
        pickle.dump(data, fh)
    with pytest.raises(RuntimeError, match="tokenizer mismatch"):
        retrieval_mod.load_index(str(bad))


def test_load_index_reports_missing_file(retrieval_mod, tmp_path):
    with pytest.raises(RuntimeError, match="Build it first"):
        retrieval_mod.load_index(str(tmp_path / "nope.pkl"))


def test_retrieval_finds_the_right_note(index_path, retrieval_mod):
    chunks, bm25 = retrieval_mod.load_index(index_path)
    hits = retrieval_mod.retrieve_context(
        "failed hearing test what do I do first", bm25, chunks, k=5)
    assert hits, "expected at least one hit"
    assert any("First 90 Days" in h["source"] for h in hits)


def test_retrieval_respects_k(index_path, retrieval_mod):
    chunks, bm25 = retrieval_mod.load_index(index_path)
    hits = retrieval_mod.retrieve_context(
        "language development deaf children reading outcomes", bm25, chunks, k=2)
    assert len(hits) <= 2


def test_retrieval_returns_nothing_for_empty_query(index_path, retrieval_mod):
    chunks, bm25 = retrieval_mod.load_index(index_path)
    assert retrieval_mod.retrieve_context("   ", bm25, chunks, k=5) == []


def test_retrieval_scores_are_positive_only(index_path, retrieval_mod):
    chunks, bm25 = retrieval_mod.load_index(index_path)
    # No shared vocabulary with the corpus: better to return nothing than to
    # return zero-scored chunks that look like retrieval.
    hits = retrieval_mod.retrieve_context("zzzqx unrelated token", bm25, chunks, k=5)
    assert hits == []
