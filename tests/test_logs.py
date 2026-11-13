"""Logs hold user text, so rotation, retention and redaction are all tested.

The failure modes: an unbounded JSONL fills a 1 GB box, a backup older than the
policy survives, and a parent's email or phone number lands in a committed log.
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "kb"))

import logio  # noqa: E402


def read_lines(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestRedaction:
    def test_redact_removes_direct_identifiers(self):
        text = "email me at jane.doe@example.com or call 555-123-4567"
        out = logio.redact_text(text, "redact")
        assert "jane.doe@example.com" not in out
        assert "555-123-4567" not in out
        assert "[email]" in out and "[phone]" in out

    def test_redact_keeps_the_question_shape(self):
        out = logio.redact_text("my son has a hearing aid and hates it", "redact")
        assert out == "my son has a hearing aid and hates it"

    def test_redact_strips_urls_handles_and_long_numbers(self):
        out = logio.redact_text(
            "see https://x.test/a?token=abc and @somebody and 123456789", "redact")
        assert "token=abc" not in out
        assert "@somebody" not in out
        assert "123456789" not in out

    def test_redact_is_not_anonymisation_and_says_so(self):
        # Documented limitation, pinned so nobody assumes names are removed.
        out = logio.redact_text("my son Adam failed his hearing test", "redact")
        assert "Adam" in out

    def test_hash_mode_stores_no_text(self):
        out = logio.redact_text("my child is in danger", "hash")
        assert out.startswith("sha256:")
        assert "danger" not in out
        assert out == logio.redact_text("my child is in danger", "hash")

    def test_none_mode_stores_nothing(self):
        assert logio.redact_text("anything", "none") == ""

    def test_full_mode_is_opt_in_and_verbatim(self):
        assert logio.redact_text("anything at all", "full") == "anything at all"

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError):
            logio.redact_text("x", "scramble")


class TestRotation:
    def test_rotates_and_bounds_the_file_count(self, tmp_path):
        path = str(tmp_path / "qa.jsonl")
        log = logio.JsonlLog(path, max_bytes=300, backups=2, retention_days=30)
        for i in range(40):
            assert log.append({"i": i, "question": "question number %d" % i})

        files = [f for f in log.files() if os.path.exists(f)]
        assert len(files) <= 3, files
        assert os.path.exists(path + ".1")
        # the newest record survives rotation
        last = read_lines(path)[-1]
        assert last["i"] == 39

    def test_each_generation_file_stays_near_the_limit(self, tmp_path):
        path = str(tmp_path / "qa.jsonl")
        log = logio.JsonlLog(path, max_bytes=300, backups=3, retention_days=30)
        for i in range(30):
            log.append({"i": i, "question": "q" * 40})
        for f in log.files():
            if os.path.exists(f):
                assert os.path.getsize(f) <= 400, (f, os.path.getsize(f))

    def test_backups_zero_keeps_one_rotated_file(self, tmp_path):
        path = str(tmp_path / "fb.jsonl")
        log = logio.JsonlLog(path, max_bytes=100, backups=0, retention_days=30)
        for i in range(10):
            log.append({"i": i, "note": "n" * 30})
        assert os.path.exists(path + ".1")


class TestRetention:
    def test_prune_removes_only_expired_backups(self, tmp_path):
        path = str(tmp_path / "qa.jsonl")
        log = logio.JsonlLog(path, max_bytes=10_000, backups=3, retention_days=2)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("")
        old = time.time() - 10 * 86400
        fresh = time.time()
        for n, mtime in ((1, old), (2, fresh)):
            with open("%s.%d" % (path, n), "w") as fh:
                fh.write("x\n")
            os.utime("%s.%d" % (path, n), (mtime, mtime))

        removed = log.prune()
        assert removed == 1
        assert not os.path.exists(path + ".1")
        assert os.path.exists(path + ".2")

    def test_zero_days_disables_pruning(self, tmp_path):
        path = str(tmp_path / "qa.jsonl")
        log = logio.JsonlLog(path, backups=2, retention_days=0)
        with open(path + ".1", "w") as fh:
            fh.write("x\n")
        old = time.time() - 999 * 86400
        os.utime(path + ".1", (old, old))
        assert log.prune() == 0
        assert os.path.exists(path + ".1")


class TestRecordsAreRedactedOnWrite:
    def test_question_and_answer_are_scrubbed_but_id_survives(self, tmp_path):
        path = str(tmp_path / "qa.jsonl")
        log = logio.JsonlLog(path, mode="redact")
        log.append({"id": "abc123", "question": "call 555-123-4567",
                    "answer": "mail jane@example.com", "sources": ["a.md"]})
        row = read_lines(path)[0]
        assert row["id"] == "abc123"
        assert "555-123-4567" not in row["question"]
        assert "jane@example.com" not in row["answer"]
        assert row["sources"] == ["a.md"]


class TestCrisisReviewExport:
    """Exporting log text requires consent, redacts, dedupes and never labels."""

    @staticmethod
    def _module():
        import importlib.util
        path = os.path.join(ROOT, "scripts", "crisis_review.py")
        spec = importlib.util.spec_from_file_location("crisis_review", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _log(self, tmp_path, rows):
        path = tmp_path / "crisis_log.jsonl"
        with open(path, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return str(path)

    def test_refuses_without_consent(self, tmp_path, capsys):
        mod = self._module()
        log = self._log(tmp_path, [{"question": "my son wants to die"}])
        rc = mod.main(["--log", log, "--out", str(tmp_path / "c.jsonl")])
        assert rc == 2
        assert not (tmp_path / "c.jsonl").exists()
        assert "refusing" in capsys.readouterr().err

    def test_writes_unlabelled_redacted_deduped_rows(self, tmp_path):
        mod = self._module()
        log = self._log(tmp_path, [
            {"question": "my son told me he wants to die"},
            {"question": "MY SON TOLD ME HE WANTS TO DIE!!"},
            {"question": "call 555-123-4567, my kid is in danger"},
            {"question": "ok"},
        ])
        out = tmp_path / "c.jsonl"
        rc = mod.main(["--log", log, "--out", str(out),
                       "--consent", "test", "--existing", str(tmp_path / "none.jsonl")])
        assert rc == 0
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        texts = [r["text"] for r in rows]
        assert len(rows) == 2, texts          # duplicate and short row dropped
        assert all(r["label"] == "" for r in rows), "the script must not label"
        assert all(r["category"] == "" for r in rows)
        assert any("[phone]" in t for t in texts)
        assert not any("555-123-4567" in t for t in texts)

    def test_skips_questions_already_in_the_labelled_set(self, tmp_path):
        mod = self._module()
        log = self._log(tmp_path, [{"question": "my son told me he wants to die"}])
        out = tmp_path / "c.jsonl"
        rc = mod.main(["--log", log, "--out", str(out), "--consent", "test",
                       "--existing", os.path.join(ROOT, "eval", "crisis_set.jsonl")])
        assert rc == 0
        assert out.read_text().strip() == ""


class TestServerUsesIt:
    def test_feedback_note_is_redacted_end_to_end(self, kb, server):
        """The one route that writes user text without needing a model."""
        import http.client

        conn = http.client.HTTPConnection(server["host"], server["port"], timeout=10)
        try:
            body = json.dumps({"id": "abc123", "vote": "down",
                               "note": "wrong number, call 555-987-6543"})
            conn.request("POST", "/feedback", body=body, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + server["token"],
            })
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200
        finally:
            conn.close()

        rows = read_lines(kb.FEEDBACK_LOG)
        assert rows, "no feedback row written"
        assert "555-987-6543" not in rows[-1]["note"]
        assert "[phone]" in rows[-1]["note"]
