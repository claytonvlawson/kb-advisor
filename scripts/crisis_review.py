#!/usr/bin/env python3
"""Turn logged crisis hits into candidate rows for human labelling.

This is how the labelled set in eval/crisis_set.jsonl should grow: from real
phrasings, reviewed by a person. Two rules keep it honest.

1. It requires --consent. The log holds things parents typed in a private
   moment, so exporting it is a deliberate act with a written justification, not
   a default. The script refuses without the flag and says why.
2. It never labels anything. Every row is written with an empty label so a human
   has to decide whether it is a true crisis, an idiom, or ordinary distress.
   Auto-labelled data would just re-teach the detector what it already believes.

    python3 scripts/crisis_review.py \
        --log clients/example-parent-advisor/logs/crisis_log.jsonl \
        --out eval/candidates.jsonl \
        --consent "reviewed with the site owner; question text is redacted"

Rows that already exist in the labelled set are skipped, and near-duplicates are
collapsed by normalising case, punctuation and digits.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kb"))

import logio  # noqa: E402

_NORMALISE = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalise(text):
    return _SPACES.sub(" ", _NORMALISE.sub(" ", text.lower())).strip()


def load_existing(paths):
    seen = set()
    for path in paths:
        p = pathlib.Path(path)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("text"):
                seen.add(normalise(row["text"]))
    return seen


def collect(log_path, mode, min_chars, limit):
    rows, seen = [], set()
    p = pathlib.Path(log_path)
    if not p.exists():
        raise SystemExit("no log at %s" % log_path)
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = record.get("question") or record.get("text") or ""
        text = logio.redact_text(raw, mode).strip()
        if len(text) < min_chars:
            continue
        key = normalise(text)
        if key in seen:
            continue
        seen.add(key)
        rows.append(text)
        if limit and len(rows) >= limit:
            break
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True, help="crisis_log.jsonl to review")
    ap.add_argument("--out", default=str(ROOT / "eval" / "candidates.jsonl"))
    ap.add_argument("--existing", nargs="*",
                    default=[str(ROOT / "eval" / "crisis_set.jsonl")],
                    help="labelled sets whose questions should not be duplicated")
    ap.add_argument("--mode", default="redact", choices=("redact", "hash", "none"),
                    help="redaction applied before export (default: redact)")
    ap.add_argument("--min-chars", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--consent", default="",
                    help="required: who approved exporting question text and why")
    a = ap.parse_args(argv)

    if not a.consent.strip():
        print("refusing to export: pass --consent with who approved this and why.\n"
              "The log holds questions parents typed in a private moment, so this\n"
              "is a deliberate act, not a default.", file=sys.stderr)
        return 2

    if a.mode == "none":
        raise SystemExit("--mode none would export empty rows; use hash or redact")

    existing = load_existing(a.existing)
    rows = [t for t in collect(a.log, a.mode, a.min_chars, a.limit)
            if normalise(t) not in existing]

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        for text in rows:
            # label and category are deliberately blank: a human fills them in
            fh.write(json.dumps({"text": text, "label": "", "category": "",
                                 "source": "crisis_log",
                                 "consent": a.consent.strip()}) + "\n")

    print("log        %s" % a.log)
    print("mode       %s (redaction applied before export)" % a.mode)
    print("existing   %d labelled questions skipped" % len(existing))
    print("candidates %d written to %s" % (len(rows), out))
    print("\nEvery row has an empty label. Label them by hand, then append the")
    print("ones you are sure about to eval/crisis_set.jsonl and re-run")
    print("scripts/eval_crisis.py. Nothing here is training data until a person")
    print("has said so.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
