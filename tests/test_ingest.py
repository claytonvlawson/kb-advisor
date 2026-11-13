"""PDF ingestion is the only code that parses untrusted third-party files.

Three fixtures, each a real failure mode from production:

  paper.pdf    a normal text PDF with metadata, a DOI and a heading
  scanned.pdf  a page with no text layer at all (an image of a page)
  broken.pdf   a truncated download

The second two must be reported and skipped, never fabricated into a note. The
distinction between them matters: a truncated file used to be reported as "needs
OCR", which sends the reader down a path that cannot work.
"""
import os
import sys

import pytest

fitz = pytest.importorskip("fitz", reason="ingest needs pymupdf (pip install -e .[ingest])")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.join(ROOT, "kb"))

import ingest_pdfs  # noqa: E402


def fixture(name):
    return os.path.join(FIXTURES, name)


def frontmatter(note):
    assert note.startswith("---\n"), note[:40]
    block = note.split("---", 2)[1]
    out = {}
    for line in block.strip().splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


class TestTextPDF:
    def test_produces_a_note_with_the_frontmatter_the_stack_reads(self):
        note, err = ingest_pdfs.pdf_to_note(fixture("paper.pdf"))
        assert err is None
        fm = frontmatter(note)
        assert fm["type"] == "source"
        assert fm["name"] == "Language Deprivation and Early Intervention"
        assert fm["authors"] == "A. Researcher and B. Scholar"
        assert fm["doi"] == "10.1093/deafed/enz012"

    def test_emits_a_full_citation_block_so_the_citation_builder_links_it(self):
        note, _ = ingest_pdfs.pdf_to_note(fixture("paper.pdf"))
        assert "## Full citation" in note
        citation = note.split("## Full citation", 1)[1]
        assert "https://doi.org/10.1093/deafed/enz012" in citation

    def test_promotes_larger_text_to_headings(self):
        """The chunker is heading-aware; a flat page produces one giant chunk."""
        note, _ = ingest_pdfs.pdf_to_note(fixture("paper.pdf"))
        heading_lines = [line for line in note.splitlines() if line.startswith("#")]
        assert any("Methods" in line for line in heading_lines), heading_lines
        assert not any(line.strip().startswith("## Methods and the title")
                        for line in heading_lines)

    def test_body_text_survives(self):
        note, _ = ingest_pdfs.pdf_to_note(fixture("paper.pdf"))
        assert "theory of mind" in note.lower()

    def test_title_is_not_duplicated_into_the_body(self):
        note, _ = ingest_pdfs.pdf_to_note(fixture("paper.pdf"))
        occurrences = note.lower().count("language deprivation and early intervention")
        assert occurrences <= 3, "title appears %d times" % occurrences


class TestSkippedFiles:
    def test_image_only_page_is_reported_as_needing_ocr(self):
        note, err = ingest_pdfs.pdf_to_note(fixture("scanned.pdf"))
        assert note is None
        assert "no text layer" in err and "OCR" in err

    def test_truncated_pdf_is_not_misreported_as_scanned(self):
        """Regression: MuPDF repairs a truncated file, so it never raised and
        used to be reported as 'needs OCR', which cannot fix a bad download."""
        note, err = ingest_pdfs.pdf_to_note(fixture("broken.pdf"))
        assert note is None
        assert "corrupt or truncated" in err
        assert "OCR will not help" in err
        assert "no text layer" not in err


class TestCommandLine:
    def _src(self, tmp_path, names):
        """A controlled input dir, so adding a fixture later cannot break this."""
        import shutil

        src = tmp_path / "pdfs"
        src.mkdir()
        for name in names:
            shutil.copy(fixture(name), src / name)
        return str(src)

    def _run(self, tmp_path, monkeypatch, src):
        out = tmp_path / "vault"
        monkeypatch.setattr(sys, "argv", ["ingest_pdfs.py", src, "-o", str(out)])
        ingest_pdfs.main()
        return out

    def test_writes_one_note_and_skips_the_other_two(self, tmp_path, monkeypatch, capsys):
        src = self._src(tmp_path, ["paper.pdf", "scanned.pdf", "broken.pdf"])
        out = self._run(tmp_path, monkeypatch, src)
        notes = sorted(p.name for p in out.iterdir())
        assert notes == ["paper.md"], notes
        printed = capsys.readouterr().out
        assert "Wrote 1 note(s), skipped 2." in printed
        assert "corrupt or truncated" in printed

    def test_never_silently_overwrites_an_existing_note(self, tmp_path, monkeypatch):
        src = self._src(tmp_path, ["paper.pdf"])
        first = self._run(tmp_path, monkeypatch, src)
        second_out = tmp_path / "vault"
        monkeypatch.setattr(sys, "argv", ["ingest_pdfs.py", src, "-o", str(second_out)])
        ingest_pdfs.main()
        notes = sorted(p.name for p in second_out.iterdir())
        assert notes == ["paper 2.md", "paper.md"], notes
        assert first.exists()


class TestCommandLineFlagsInjection:
    def test_flagged_note_is_reported_in_the_summary(self, tmp_path, monkeypatch, capsys):
        import shutil

        src = tmp_path / "pdfs"
        src.mkdir()
        shutil.copy(fixture("poisoned.pdf"), src / "poisoned.pdf")
        out = tmp_path / "vault"
        monkeypatch.setattr(sys, "argv", ["ingest_pdfs.py", str(src), "-o", str(out)])
        ingest_pdfs.main()
        printed = capsys.readouterr().out
        assert "FLAGGED 1 note(s)" in printed
        assert "poisoned.md" in printed
        assert "review the sources" in printed


class TestIngestFeedsRetrieval:
    def test_the_written_note_is_chunkable_and_indexable(self, tmp_path, monkeypatch):
        """End to end for the one contract that is easy to break: the note a
        client's PDF becomes has to be readable by the chunker."""
        import retrieval

        out = tmp_path / "vault"
        monkeypatch.setattr(sys, "argv", ["ingest_pdfs.py", FIXTURES, "-o", str(out)])
        ingest_pdfs.main()

        index = tmp_path / "bm25.pkl"
        chunks = retrieval.build_index(str(out), str(index))
        assert chunks > 0, "the ingested note produced no chunks"
        loaded_chunks, bm25 = retrieval.load_index(str(index))
        assert len(loaded_chunks) == chunks
        assert any("theory of mind" in c["text"].lower() for c in loaded_chunks)
