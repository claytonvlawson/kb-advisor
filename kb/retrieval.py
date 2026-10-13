#!/usr/bin/env python3
"""retrieval.py -- BM25-only vault retrieval for the cloud advisor.

Ported from jetson-brain's build_index.py (chunking) and brain.py (tokenizer +
BM25 ranking). Dense/hybrid retrieval is deliberately NOT ported: it depends on
Ollama's nomic-embed-text embedder at query time, which this box does not run
(no GPU, no dependency on the Jetson staying up). This is BM25 only.

The chunk-text pipeline and tokenizer are kept byte-identical to jetson-brain's
originals so a chunk built here reads exactly like a chunk built there -- the
only thing dropped is the "dense" pickle key and everything downstream of it.

Usage:
    python3 -c 'import retrieval; retrieval.build_index("/opt/advisor/vault", "/opt/advisor/index/bm25.pkl")'
"""
import os
import pickle
import re
import sys
import unicodedata

TOKENIZER_VERSION = "tok-nfkd-acr-sstem"  # brain.py's EXPECTED_TOKENIZER_VERSION

CHUNK_WORDS = 220   # flush an accumulating chunk at ~this size
FLOOR_WORDS = 90    # sections smaller than this get merged with neighbors
CEIL_WORDS = 320    # sections larger than this get split (sentence-safe)

_JUNK_DIR_NAMES = {"node_modules", "site-packages", ".venv", "Templates",
                    "_WebCache", "_Research", "_History", "_Advisor",
                    "_PendingReview", "_Fulltext"}

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------
# Tokenizer -- byte-identical to brain.py / build_index.py's copy. MUST stay
# that way or a query and the index it searches disagree on what a token is.
# ---------------------------------------------------------------------------
def _s_stem(w):
    if len(w) < 4 or not w.isalpha():
        return w
    if w.endswith("ies") and not w.endswith(("eies", "aies")):
        return w[:-3] + "y"
    if w.endswith("es") and not w.endswith(("aes", "ees", "oes")):
        return w[:-1]
    if w.endswith("s") and not w.endswith(("us", "ss", "is")):
        return w[:-1]
    return w


def tokenize(s):
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group(0).replace(".", ""), s)
    return [_s_stem(t) for t in re.findall(r"[a-z0-9]+", s)]


# ---------------------------------------------------------------------------
# Chunking -- ported from build_index.py.
# ---------------------------------------------------------------------------
def split_frontmatter(text):
    if not text.startswith("---"):
        return "", text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return "", text


def clean_obsidian(text):
    text = re.sub(r"!\[\[[^\]]*\]\]", "", text)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[!\w+\][-+]?", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    return text


def strip_heading_hashes(line):
    m = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
    if m:
        return m.group(1).strip()
    return line


def extract_title(raw_text, fallback):
    for line in raw_text.split("\n"):
        m = re.match(r"^\s{0,3}#{1,6}\s+(.+)$", line)
        if m:
            title = m.group(1).strip()
            if title:
                return title
    return fallback


def normalize_body(text):
    text = clean_obsidian(text)
    return "\n".join(strip_heading_hashes(line) for line in text.split("\n"))


def split_into_sections(body_no_fm):
    sections = []
    stack = []
    cur_lines = []
    cur_crumb = []

    def flush():
        text = "\n".join(cur_lines).strip()
        if text:
            sections.append({"crumb": list(cur_crumb), "text": text})

    for line in body_no_fm.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            flush()
            cur_lines[:] = []
            level = len(m.group(1))
            title = m.group(2).strip()
            stack[:] = stack[: level - 1]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
            cur_crumb = [t for t in stack if t]
        else:
            cur_lines.append(line)
    flush()
    return sections


def _sentences(text):
    units = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for s in _SENTENCE_SPLIT_RE.split(line):
            if s.strip():
                units.append(s.strip())
    return units


def split_oversized(text):
    sents = _sentences(text)
    if not sents:
        return []
    pieces = []
    cur = []
    cur_w = 0
    for s in sents:
        w = len(s.split())
        if w > CEIL_WORDS:
            if cur:
                pieces.append(" ".join(cur))
                cur = []
                cur_w = 0
            words = s.split()
            for i in range(0, len(words), CEIL_WORDS):
                pieces.append(" ".join(words[i:i + CEIL_WORDS]))
            continue
        if cur and cur_w + w > CEIL_WORDS:
            pieces.append(" ".join(cur))
            cur = [cur[-1]]
            cur_w = len(cur[-1].split())
        cur.append(s)
        cur_w += w
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def _crumb_prefix(stem, note_title, crumb):
    head = [p for p in (stem, note_title) if p]
    parts = head + [c for c in crumb if c and c != note_title and c != stem]
    trail = []
    for p in parts:
        if not trail or trail[-1] != p:
            trail.append(p)
    return "[" + " > ".join(trail) + "]"


def chunk_note(body_no_fm, note_title, stem, floor_words=None):
    if floor_words is None:
        floor_words = FLOOR_WORDS
    sections = split_into_sections(body_no_fm)
    if not sections:
        return []

    prepared = []
    for sec in sections:
        text = normalize_body(sec["text"]).strip()
        if not text:
            continue
        prepared.append({"crumb": sec["crumb"], "text": text,
                          "words": len(text.split())})
    if not prepared:
        return []

    chunks = []
    buf_text = []
    buf_words = 0
    buf_crumb = None

    def flush_buffer():
        nonlocal buf_text, buf_words, buf_crumb
        if buf_text:
            body = " ".join(buf_text).strip()
            if body:
                prefix = _crumb_prefix(stem, note_title, buf_crumb or [])
                chunks.append(prefix + "\n" + body)
        buf_text = []
        buf_words = 0
        buf_crumb = None

    for sec in prepared:
        words = sec["words"]
        if words >= floor_words:
            if buf_words and buf_words + words <= CEIL_WORDS:
                sec_text = " ".join(buf_text) + " " + sec["text"]
                sec_crumb = buf_crumb or sec["crumb"]
                buf_text = []
                buf_words = 0
                buf_crumb = None
            else:
                flush_buffer()
                sec_text = sec["text"]
                sec_crumb = sec["crumb"]

            if len(sec_text.split()) <= CEIL_WORDS:
                prefix = _crumb_prefix(stem, note_title, sec_crumb)
                chunks.append(prefix + "\n" + sec_text.strip())
            else:
                for piece in split_oversized(sec_text):
                    prefix = _crumb_prefix(stem, note_title, sec_crumb)
                    chunks.append(prefix + "\n" + piece.strip())
        else:
            if buf_crumb is None:
                buf_crumb = sec["crumb"]
            leaf = sec["crumb"][-1] if sec["crumb"] else ""
            if leaf and leaf != note_title and (not buf_crumb or leaf != buf_crumb[-1]):
                buf_text.append(leaf + ": " + sec["text"])
            else:
                buf_text.append(sec["text"])
            buf_words += words
            if buf_words >= CHUNK_WORDS:
                flush_buffer()

    if buf_text:
        body = " ".join(buf_text).strip()
        if body and buf_words < floor_words and chunks and \
                len(chunks[-1].split()) + buf_words <= CEIL_WORDS:
            chunks[-1] = chunks[-1].rstrip() + " " + body
        else:
            flush_buffer()
    return chunks


# ---------------------------------------------------------------------------
# Index build / load / query -- BM25 only, no dense block.
# ---------------------------------------------------------------------------
def build_index(vault, out):
    """Walk `vault` for *.md, chunk each note, and pickle a BM25-only index at
    `out`. Mirrors build_index.py's pickle shape minus the "dense" key."""
    if not os.path.isdir(vault):
        raise RuntimeError("vault directory not found: %s" % vault)

    md_files = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in _JUNK_DIR_NAMES]
        for name in files:
            if name.lower().endswith(".md"):
                md_files.append(os.path.join(root, name))
    md_files.sort()

    chunks = []
    by_source = {}
    next_id = 0

    for path in md_files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
        except OSError as exc:
            print("WARNING: could not read %s: %s" % (path, exc), flush=True)
            continue

        rel_source = os.path.relpath(path, vault)
        fallback_title = os.path.splitext(os.path.basename(path))[0]
        stem = fallback_title
        frontmatter, body_no_fm = split_frontmatter(raw)
        title = extract_title(body_no_fm, fallback_title)

        by_source.setdefault(rel_source, [])
        for chunk_text in chunk_note(body_no_fm, title, stem):
            if not tokenize(chunk_text):
                continue
            chunks.append({
                "id": next_id,
                "text": chunk_text,
                "source": rel_source,
                "title": title,
            })
            by_source[rel_source].append(next_id)
            next_id += 1

    index = {
        "version": 5,
        "tokenizer_version": TOKENIZER_VERSION,
        "chunks": chunks,
    }
    out_dir = os.path.dirname(out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)

    print("Files scanned: %d" % len(md_files))
    print("Chunks built:  %d" % len(chunks))
    print("Output path:   %s" % out)
    return len(chunks)


def load_index(index_path):
    """Load the pickle and rebuild a BM25Okapi over it. Returns (chunks, bm25)."""
    if not os.path.exists(index_path):
        raise RuntimeError(
            "No index found at %s. Build it first: "
            'python3 -c \'import retrieval; retrieval.build_index(VAULT, INDEX)\'' % index_path)

    with open(index_path, "rb") as fh:
        data = pickle.load(fh)

    chunks = data.get("chunks") or []
    tok_ver = data.get("tokenizer_version")
    if tok_ver != TOKENIZER_VERSION:
        raise RuntimeError(
            "Index tokenizer mismatch: index=%r, expected=%r. Rebuild the index."
            % (tok_ver, TOKENIZER_VERSION))
    if not chunks:
        raise RuntimeError("The index at %s is empty." % index_path)

    tokenized = [tokenize(c.get("text") or "") for c in chunks]

    from rank_bm25 import BM25Okapi
    bm25 = BM25Okapi(tokenized)
    return chunks, bm25


def retrieve_context(question, bm25, chunks, k=8):
    """Rank chunks against `question` with plain BM25 and return the top-k as
    {"text", "source", "title"} dicts, highest score first."""
    qtok = tokenize(question)
    if not qtok:
        return []
    scores = bm25.get_scores(qtok)
    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    out = []
    for i in ranked:
        if scores[i] <= 0:
            break
        c = chunks[i]
        out.append({"text": c["text"], "source": c["source"], "title": c["title"]})
        if len(out) >= k:
            break
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python3 retrieval.py <vault_dir> <out_index.pkl>")
        sys.exit(1)
    build_index(sys.argv[1], sys.argv[2])
