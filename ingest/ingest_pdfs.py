#!/usr/bin/env python3
"""ingest_pdfs.py -- turn PDFs into vault-ready markdown notes.

The production learnwithclay.ai advisor reads a markdown-only vault; this is
the missing front door for "drop in a client's PDFs and get a knowledge
base." Each PDF becomes one Obsidian-style note the stack already knows how
to chunk, retrieve, and cite:

  - frontmatter: name, type: source, authors/year/doi/url when found
  - `## Full citation` block when a DOI or a clean title/authors/year set
    is extractable, so build_citations() links it (production invariant:
    citations come from `## Full citation` blocks or callouts).
  - body text extracted with PyMuPDF, headings guessed from font size
    (the chunker is heading-aware; headings are what make chunks focused).

Usage:
  python3 ingest/ingest_pdfs.py <pdf_dir_or_file> -o <vault_dir> [--type source]

Then rebuild the index:
  python3 -c 'import sys; sys.path.insert(0, "kb"); import retrieval; \
              retrieval.build_index("<vault_dir>", "<index.pkl>")'

Scanned/image-only PDFs have no text layer: this script reports them and
skips them rather than fabricating text (OCR is a separate, deliberate
step -- see README).
"""
import argparse
import os
import re
import sys

import fitz  # PyMuPDF

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kb"))
import injection  # noqa: E402  (the scanner lives next to the server that uses it)

MIN_CHARS_PER_PAGE = 80   # below this the page is probably scanned


def _slug(name):
    name = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] or "untitled"


def _guess_headings(doc, sample_pages=6):
    """Most common body font size -> heading threshold. Returns (body_size, threshold)."""
    sizes = {}
    for pno in range(min(sample_pages, doc.page_count)):
        for block in doc[pno].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        s = round(span.get("size", 0))
                        sizes[s] = sizes.get(s, 0) + len(span["text"])
    if not sizes:
        return 11.0, 14.0
    body = max(sizes, key=lambda s: sizes[s])
    bigger = [s for s in sizes if s > body + 1.5]
    return body, (min(bigger) if bigger else body + 2)


def extract_body(doc, body_size, heading_threshold):
    """Reconstruct markdown-ish text, promoting larger spans to headings."""
    out = []
    for pno in range(doc.page_count):
        page = doc[pno]
        lines = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not text.strip():
                    continue
                size = max((s.get("size", 0) for s in line.get("spans", [])), default=0)
                bold = any("bold" in (s.get("font", "") or "").lower()
                           for s in line.get("spans", []))
                if size >= heading_threshold:
                    level = 2 if size < heading_threshold + 3 else 1
                    text = text.strip()
                    if text and not text.startswith("#"):
                        text = "#" * level + " " + text
                elif bold and size >= body_size and len(text.strip()) < 80:
                    text = "## " + text.strip()
                lines.append(text)
        if lines:
            out.append("\n".join(lines))
    return "\n\n".join(out)


def find_doi(text):
    m = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>\]]+)", text)
    if m:
        doi = m.group(1).rstrip(".,;)")
        return doi
    return ""


def find_arxiv_url(text):
    m = re.search(r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/[\w.-]+", text)
    return m.group(0).rstrip(".,;)") if m else ""


def find_authors_year(doc, meta):
    """Best-effort authors/year from PDF metadata or the first page."""
    text0 = doc[0].get_text()[:3000] if doc.page_count else ""
    year = ""
    m = re.search(r"\b(19|20)\d{2}\b", meta.get("date") or "")
    if m:
        year = m.group(0)
    if not year:
        m = re.search(r"\b(19|20)\d{2}\b", text0)
        if m:
            year = m.group(0)
    authors = meta.get("author") or ""
    return authors, year


def build_citation_text(title, authors, year, doi, url):
    parts = []
    if title:
        parts.append('"%s."' % title)
    if authors:
        parts.append(authors.rstrip(",") + ("." if year else ""))
    if year:
        parts.append(year + ".")
    if doi:
        parts.append("https://doi.org/%s" % doi)
    elif url:
        parts.append(url)
    return " ".join(parts) if parts else ""


def _guess_title(doc, body_size, heading_threshold):
    """Title = first largest-font line on page 1; fall back to first line."""
    best, best_size = "", 0
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if not text:
                continue
            size = max((s.get("size", 0) for s in line.get("spans", [])), default=0)
            if size > best_size:
                best, best_size = text, size
    if best and best_size >= heading_threshold:
        return best[:160]
    return ""


def _guess_authors(doc, title):
    """Authors = first non-title line on page 1 that looks like a name list."""
    if not title:
        return ""
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if not text or text == title:
                continue
            # name-list shape: "A. Author and B. Author", "Author, A.; Author, B.",
            # "Dr. X Y, Prof. Z W", or ends with a bare year
            looks_names = (" and " in text or (";" in text and "," in text)
                           or re.search(r"\b(19|20)\d{2}\s*$", text) is not None)
            words = text.split()
            if looks_names and 2 <= len(words) <= 20 \
                    and not re.match(r"^(abstract|introduction|keywords?\b)", text, re.I):
                return text[:200]
    return ""


def _pdf_to_note_inner(pdf_path, on_injection="flag"):
    doc = fitz.open(pdf_path)
    meta = doc.metadata or {}
    body_size, heading_threshold = _guess_headings(doc)
    body = extract_body(doc, body_size, heading_threshold)

    n_pages = max(1, doc.page_count)
    if len(body.strip()) < MIN_CHARS_PER_PAGE * n_pages * 0.5:
        # A truncated download does not raise: MuPDF "repairs" it into a
        # document with no text, which used to be reported as "needs OCR" and
        # sent the reader down the wrong path. Tell the two apart.
        repaired = bool(getattr(doc, "is_repaired", False))
        doc.close()
        if repaired:
            return None, ("corrupt or truncated PDF (the parser had to repair "
                          "it) -- re-download it; OCR will not help")
        return None, "no text layer (likely scanned) -- needs OCR first"

    title = (meta.get("title") or "").strip() or _guess_title(doc, body_size, heading_threshold)
    if not title:
        # first non-empty line of the body, heading marker stripped
        for line in body.split("\n"):
            if line.strip():
                title = line.strip().lstrip("#").strip()[:120]
                break
    if not title:
        title = os.path.splitext(os.path.basename(pdf_path))[0]

    # clear the title line out of the body (frontmatter `name:` carries it)
    for line in body.split("\n", 3)[:3]:
        if line.strip().lstrip("#").strip() == title:
            body = body.replace(line, "", 1)
            break

    doi = find_doi(body[:6000])
    url = find_arxiv_url(body[:6000])
    authors = (meta.get("author") or "").strip() or _guess_authors(doc, title)
    year = find_authors_year(doc, meta)[1]
    citation = build_citation_text(title, authors, year, doi, url)
    doc.close()

    # A PDF is untrusted input and its text goes in front of the model, so it is
    # scanned before it becomes a note. `sanitize` always runs: zero-width and
    # bidi characters have no place in a knowledge base.
    findings = injection.scan(body)
    rules = injection.summary(findings)
    if findings and on_injection == "skip":
        return None, ("instruction-like content (%s) -- review the file before "
                      "ingesting it" % rules)
    body = injection.sanitize(body)

    fm_lines = ["---", "name: %s" % title, "type: source"]
    if findings:
        fm_lines.append("injection_flags: %d" % len({name for name, _ in findings}))
    if authors:
        fm_lines.append("authors: %s" % authors)
    if year:
        fm_lines.append("year: %s" % year)
    if doi:
        fm_lines.append("doi: %s" % doi)
    if url:
        fm_lines.append("url: %s" % url)
    fm_lines.append("---")

    note = "\n".join(fm_lines) + "\n\n# %s\n" % title
    if findings:
        note += ("\n> [!warning] Instruction-like text was found in this document "
                 "(%s). Retrieval screens chunks at serve time and will drop them; "
                 "review the source before trusting this note.\n" % rules)
    if citation:
        note += "\n## Full citation\n\n%s\n" % citation
    note += "\n" + body.strip() + "\n"
    return note, None


def pdf_to_note(pdf_path, on_injection="flag"):
    """(note, error). MuPDF error output is muted either way.

    A corrupt PDF makes the C library spray "format error" lines onto stderr;
    our own message is the actionable one, so the noise goes away and comes back
    in a finally block so one bad file cannot silence the next good one.
    """
    tools = getattr(fitz, "TOOLS", None)
    can_mute = tools is not None and hasattr(tools, "mupdf_display_errors")
    if can_mute:
        tools.mupdf_display_errors(False)
    try:
        return _pdf_to_note_inner(pdf_path, on_injection=on_injection)
    finally:
        if can_mute:
            tools.mupdf_display_errors(True)


def main():
    ap = argparse.ArgumentParser(description="PDF -> markdown note(s) for the KB advisor vault")
    ap.add_argument("src", help="PDF file or directory of PDFs")
    ap.add_argument("-o", "--out", required=True, help="vault directory to write notes into")
    ap.add_argument("--on-injection", choices=("flag", "skip"), default="flag",
                    help="flag (default): write the note with a warning callout and "
                         "a frontmatter marker; skip: refuse to write it at all. "
                         "Either way the serve-time screen drops the chunk.")
    args = ap.parse_args()

    if os.path.isfile(args.src):
        pdfs = [args.src]
    elif os.path.isdir(args.src):
        pdfs = sorted(
            os.path.join(args.src, f) for f in os.listdir(args.src)
            if f.lower().endswith(".pdf"))
    else:
        print("ERROR: %s not found" % args.src, file=sys.stderr)
        sys.exit(1)
    if not pdfs:
        print("No PDFs found in %s" % args.src, file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)
    written, skipped, flagged = [], [], []
    for p in pdfs:
        try:
            note, err = pdf_to_note(p, on_injection=args.on_injection)
        except Exception as exc:
            note, err = None, "extraction failed: %s" % exc
        if err:
            skipped.append((p, err))
            print("SKIP  %s -- %s" % (os.path.basename(p), err))
            continue
        stem = _slug(os.path.splitext(os.path.basename(p))[0])
        out_path = os.path.join(args.out, stem + ".md")
        i = 2
        while os.path.exists(out_path):     # never silently overwrite a note
            out_path = os.path.join(args.out, "%s %d.md" % (stem, i))
            i += 1
        if "> [!warning] Instruction-like text" in note:
            flagged.append(out_path)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(note)
        written.append(out_path)
        print("OK    %s -> %s" % (os.path.basename(p), os.path.relpath(out_path)))

    print("\nWrote %d note(s), skipped %d." % (len(written), len(skipped)))
    if flagged:
        print("FLAGGED %d note(s) for instruction-like content: %s"
              % (len(flagged), ", ".join(os.path.basename(f) for f in flagged)))
        print("  Retrieval screens these chunks at serve time; review the sources.")
    if skipped:
        print("Skipped files need OCR (scanned), a re-download (corrupt), or review.")
    print("Next: rebuild the index (see README) and restart the server.")


if __name__ == "__main__":
    main()
