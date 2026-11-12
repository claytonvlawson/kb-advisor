"""The crisis detector is measured, and the floors are enforced here too.

`scripts/eval_crisis.py` is the human-facing report and the CI gate; this keeps
the same floors enforced in the unit suite, so a pattern edit that quietly
loses recall fails on the fastest signal available.

Recall is the number that matters. Missing a crisis fails silently; a false
positive shows safety resources alongside the real answer, which the design
rules accept on purpose.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "kb"))

import regex_safety  # noqa: E402

SET_PATH = os.path.join(ROOT, "eval", "crisis_set.jsonl")
CLIENT_DIR = os.path.join(ROOT, "clients", "example-parent-advisor")

MIN_RECALL = 0.95
MIN_PRECISION = 0.90


def load_rows():
    rows = []
    with open(SET_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


@pytest.fixture(scope="module")
def rows():
    return load_rows()


@pytest.fixture(scope="module")
def crisis_regex():
    cfg = json.load(open(os.path.join(CLIENT_DIR, "crisis.json")))
    return regex_safety.compile_patterns(cfg["patterns"])


class TestTheSetItself:
    def test_has_both_labels_and_enough_of_each(self, rows):
        crisis = [r for r in rows if r["label"] == "crisis"]
        ok = [r for r in rows if r["label"] == "ok"]
        assert len(crisis) >= 40, "too few crisis examples to measure recall"
        assert len(ok) >= 40, "too few ordinary examples to measure precision"

    def test_every_row_is_labelled_and_categorised(self, rows):
        for row in rows:
            assert row["label"] in ("crisis", "ok"), row
            assert row.get("text"), row
            assert row.get("category"), row

    def test_no_duplicate_questions(self, rows):
        texts = [r["text"].strip().lower() for r in rows]
        dupes = {t for t in texts if texts.count(t) > 1}
        assert not dupes, "duplicate questions: %s" % sorted(dupes)

    def test_hard_negatives_are_present(self, rows):
        """Idioms are the false-positive risk; the set has to contain them."""
        categories = {r["category"] for r in rows if r["label"] == "ok"}
        assert "idiom" in categories
        assert "cut_hit_hurt" in categories


class TestTheDetector:
    def test_config_compiles_through_the_guard(self, crisis_regex):
        assert crisis_regex is not None

    def test_recall_floor(self, rows, crisis_regex):
        crisis = [r for r in rows if r["label"] == "crisis"]
        hits = [r for r in crisis if crisis_regex.search(r["text"])]
        recall = len(hits) / len(crisis)
        misses = [r["text"] for r in crisis if r not in hits]
        assert recall >= MIN_RECALL, (
            "recall %.3f below the %.2f floor; missed: %s"
            % (recall, MIN_RECALL, misses))

    def test_precision_floor(self, rows, crisis_regex):
        ordinary = [r for r in rows if r["label"] == "ok"]
        fired = [r for r in ordinary if crisis_regex.search(r["text"])]
        precision = 1.0 - (len(fired) / len(ordinary))
        assert precision >= MIN_PRECISION, (
            "precision %.3f below the %.2f floor; false positives: %s"
            % (precision, MIN_PRECISION, [r["text"] for r in fired]))

    def test_known_miss_is_documented_not_hidden(self, rows, crisis_regex):
        """A patterned-injury description is not a regex target; this pins the gap."""
        row = next(r for r in rows if "bruise in the shape" in r["text"])
        assert not crisis_regex.search(row["text"]), (
            "if this now matches, drop this test and the README note about it")
