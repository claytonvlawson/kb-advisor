#!/usr/bin/env python3
"""citations.py -- resolve retrieved vault notes into clickable citations.

Extracted verbatim from the production Parent Advisor (parent_advisor_server.py,
learnwithclay.ai) with the vault root and cap turned into parameters instead of
module globals, so one process can serve several knowledge bases.

Two passes, same invariants as production:

  build_citations(sources)
      Dedupe, resolve each note off disk, drop notes without a citation
      block, sort URL-bearing entries first, truncate to max_citations.

  build_citations_grounded(sources, answer, vault, max_citations)
      The 2026-10-12 production invariant: any paper the ANSWER names
      in-text must appear in citations, even when its note was never
      retrieved or would be dropped/truncated.
        Tier 1: a retrieved source whose title or citation text appears
                verbatim (normalized, >=12 chars) in the answer is
                force-merged in.
        Tier 2: a startup-built catalog of every vault note matches
                author-year mentions and digit-bearing names/aliases
                (plus full names for type: person notes) against the
                answer, so an in-text echo of a paper named inside a
                retrieved chunk still gets its link.

Production lessons baked in (do not "simplify" these away):
  - citation_for reads notes off disk at answer time, so the service user
    must be able to read the vault; an unreadable tree returns empty
    citations while retrieval still works (chunk text lives in the pickle).
  - a paper named in-text can vanish from citations when its note lacks a
    `## Full citation` block -- that is what the grounded pass fixes.
  - dedupe vault notes that are copies of the same paper by DOI/URL.
"""
import functools
import os
import re


# ---------------------------------------------------------------------------
# Frontmatter / markdown helpers (identical to production).
# ---------------------------------------------------------------------------
def split_frontmatter(text):
    if not text.startswith("---"):
        return "", text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return "", text


def fm_value(fm, key):
    for line in fm.split("\n"):
        m = re.match(r"^" + re.escape(key) + r"\s*:\s*(.*)$", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def _clean_md(s):
    s = re.sub(r"!\[\[[^\]]*\]\]", "", s)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    return s.replace("*", "").strip()


def _first_h1(body):
    for line in body.split("\n"):
        m = re.match(r"^#\s+(.*)$", line)
        if m:
            return _clean_md(m.group(1))
    return ""


def _extract_full_citation(body):
    out, capturing = [], False
    for line in body.split("\n"):
        if re.match(r"^#{1,6}\s", line):
            if capturing:
                break
            if re.match(r"^#{1,6}\s+Full citation\s*$", line, re.IGNORECASE):
                capturing = True
            continue
        if capturing:
            out.append(line)
    text = _clean_md(" ".join(l.strip() for l in out if l.strip()))
    return re.sub(r"\s+", " ", text).strip()


def _extract_callout_citation(body):
    out, capturing = [], False
    for line in body.split("\n"):
        if not capturing:
            m = re.match(r"^\s*>\s*\[!\w+\]\s*Citation\b[:]?(.*)$", line, re.IGNORECASE)
            if m:
                capturing = True
                if m.group(1).strip():
                    out.append(m.group(1).strip())
            continue
        if re.match(r"^\s*>", line):
            out.append(re.sub(r"^\s*>\s?", "", line))
        else:
            break
    text = _clean_md(" ".join(l.strip() for l in out if l.strip()))
    return re.sub(r"\s+", " ", text).strip()


def _safe_url(u):
    u = (u or "").strip().strip("<>").strip().rstrip(").,;")
    return u if re.match(r"^https?://\S+$", u, re.IGNORECASE) else ""


def _safe_doi(d):
    d = (d or "").strip()
    return d if re.match(r"^10\.\S+$", d) else ""


def _first_body_url(body):
    m = re.search(r"https?://[^\s)>\]]+", body)
    return _safe_url(m.group(0)) if m else ""


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


# ---------------------------------------------------------------------------
# citation_for -- one note -> {title, citation, url, doi, has_citation}
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=2048)
def citation_for(vault_root, rel_source):
    basename = os.path.splitext(os.path.basename(str(rel_source)))[0]
    fallback = {"title": basename, "citation": basename, "url": "", "doi": ""}
    path = os.path.normpath(os.path.join(vault_root, str(rel_source)))
    if path != vault_root and not path.startswith(vault_root + os.sep):
        return fallback  # path traversal guard
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return fallback
    fm, body = split_frontmatter(raw)
    title = fm_value(fm, "name") or _first_h1(body) or basename
    doi = _safe_doi(fm_value(fm, "doi"))
    url = _safe_url(fm_value(fm, "url"))
    if not url and doi:
        url = "https://doi.org/" + doi
    full_citation = _extract_full_citation(body)
    if not url and full_citation:
        url = _first_body_url(body)
    real = full_citation or _extract_callout_citation(body)
    citation = real or title
    return {"title": title, "citation": citation, "url": url, "doi": doi,
            "has_citation": bool(real)}


def build_citations(vault_root, sources, max_citations=6):
    seen, resolved = set(), []
    for src in sources:
        key = str(src)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(citation_for(vault_root, key))
    resolved = [c for c in resolved if c.get("has_citation")]
    with_url = [c for c in resolved if c["url"]]
    without = [c for c in resolved if not c["url"]]
    return (with_url + without)[:max_citations]


# ---------------------------------------------------------------------------
# Grounded pass -- production invariant: a paper quoted in-text is always
# referenced at the end of the answer. Code-side merge, not a prompt clause.
# ---------------------------------------------------------------------------
def _norm_loose(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _fm_list(fm, key):
    raw = fm_value(fm, key)
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [p.strip().strip("'\"") for p in raw.split(",") if p.strip()]


def _surname(author):
    author = author.strip()
    if "," in author:
        first = author.split(",")[0].strip()
        if first and not first.endswith("."):
            return first
    toks = author.split()
    return toks[-1] if toks else ""


@functools.lru_cache(maxsize=8)
def _vault_catalog(vault_root):
    """(rel_path, surnames, year, name_needles, person_needles) per note."""
    entries = []
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            fm, body = split_frontmatter(head)
            name = fm_value(fm, "name") or _first_h1(body) or os.path.splitext(fn)[0]
            surnames = []
            for a in _fm_list(fm, "authors"):
                s = _surname(a)
                if s and len(s) >= 3 and s.lower() not in surnames:
                    surnames.append(s.lower())
            needles = []
            for cand in [name] + _fm_list(fm, "aliases"):
                n = _norm_loose(cand)
                if n and len(n) >= 10 and any(ch.isdigit() for ch in n) \
                        and n not in needles:
                    needles.append(n)
            # Person notes: full-name needles so a person NAMED in an answer
            # gets their profile link; bare surnames never match.
            person_needles = []
            if fm_value(fm, "type") == "person":
                for cand in [name] + _fm_list(fm, "aliases"):
                    n = _norm_loose(cand)
                    if n and len(n) >= 8 and n not in needles \
                            and n not in person_needles:
                        person_needles.append(n)
            entries.append({
                "rel": os.path.relpath(path, vault_root),
                "surnames": surnames,
                "year": fm_value(fm, "year") or "",
                "needles": needles,
                "person_needles": person_needles,
            })
    return tuple(entries)


def _author_year_match(entry, hay):
    year = entry.get("year") or ""
    surnames = entry.get("surnames") or []
    if not year or year not in hay:
        return False
    if not surnames:
        return False
    if len(surnames) == 1:
        # Single-author notes are surname-collision prone ("Hall 2020" must
        # not match "Caselli, Hall, and Henner 2020"): require the literal
        # "surname year" pair.
        return re.search(re.escape(surnames[0]) + r"\s*,?\s*" + year, hay) \
            is not None
    if len(surnames) <= 3:
        return all(s in hay for s in surnames)
    return surnames[0] in hay


def _vault_grounded_citations(vault_root, answer, seen_keys):
    hay = _norm(answer)
    hay_loose = _norm_loose(answer)
    out, seen_doi = [], set()
    for e in _vault_catalog(vault_root):
        matched = _author_year_match(e, hay) or \
            any(n in hay_loose for n in e["needles"]) or \
            any(n in hay_loose for n in e.get("person_needles") or [])
        if not matched:
            continue
        c = citation_for(vault_root, e["rel"])
        key = _norm(c.get("citation"))
        dupe = c.get("doi") or c.get("url") or ""
        if key in seen_keys or (dupe and dupe in seen_doi):
            continue
        seen_doi.add(dupe)
        out.append(c)
    return out


def build_citations_grounded(vault_root, sources, answer, max_citations=6):
    """build_citations plus any source the answer names in-text."""
    base = build_citations(vault_root, sources, max_citations)
    if not answer:
        return base
    hay = _norm(answer)
    all_srcs = []
    seen = set()
    for src in sources:
        key = str(src)
        if key not in seen:
            seen.add(key)
            all_srcs.append(citation_for(vault_root, key))
    grounded = []
    for c in all_srcs:
        needles = [_norm(c.get("title")), _norm(c.get("citation"))]
        if any(n and len(n) >= 12 and n in hay for n in needles):
            grounded.append(c)
    base_keys = {_norm(c.get("citation")) for c in base}
    merged = list(base)
    for c in grounded:
        if _norm(c.get("citation")) not in base_keys:
            merged.append(c)
            base_keys.add(_norm(c.get("citation")))
    for c in _vault_grounded_citations(vault_root, answer, base_keys):
        if _norm(c.get("citation")) not in base_keys:
            merged.append(c)
            base_keys.add(_norm(c.get("citation")))
    return merged
