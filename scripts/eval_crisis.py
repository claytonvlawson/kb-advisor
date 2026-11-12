#!/usr/bin/env python3
"""Measure the crisis detector against a labelled set.

    python3 scripts/eval_crisis.py \
        --set eval/crisis_set.jsonl \
        --client clients/example-parent-advisor

Reports recall on the crisis questions, precision on the ordinary ones, and the
specific misses and false positives, then exits non-zero if either floor is
missed. The floors exist so this can gate CI: an unmeasured safety path is the
gap this script was written to close.

Why recall is the headline: injecting the safety note early degrades gracefully
(the parent gets resources plus the answer). Missing a crisis fails silently, so
recall is the number that matters and precision only has to stay high enough
that the safety path does not fire on ordinary parenting questions.

Patterns are compiled through kb/regex_safety.py, so a risky config fails here
the same way it fails at server startup.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kb"))

import regex_safety  # noqa: E402


def load_set(path):
    rows = []
    with open(path) as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("label") not in ("crisis", "ok"):
                raise SystemExit("%s:%d has label %r, expected 'crisis' or 'ok'"
                                 % (path, lineno, row.get("label")))
            if not row.get("text"):
                raise SystemExit("%s:%d has no text" % (path, lineno))
            rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default=str(ROOT / "eval" / "crisis_set.jsonl"))
    ap.add_argument("--client", default=str(ROOT / "clients" / "example-parent-advisor"),
                    help="client config dir holding crisis.json")
    ap.add_argument("--min-recall", type=float, default=0.95)
    ap.add_argument("--min-precision", type=float, default=0.90)
    ap.add_argument("--show", type=int, default=0,
                    help="print up to N examples per outcome (default: only failures)")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args(argv)

    crisis_path = pathlib.Path(a.client) / "crisis.json"
    if not crisis_path.exists():
        raise SystemExit("no crisis.json in %s" % a.client)
    cfg = json.loads(crisis_path.read_text())
    patterns = cfg.get("patterns") or []
    try:
        regex = regex_safety.compile_patterns(patterns, client=str(crisis_path))
    except regex_safety.RiskyPatternError as exc:
        raise SystemExit("pattern guard rejected the config: %s" % exc)

    rows = load_set(a.set)
    if not rows:
        raise SystemExit("empty eval set")
    if regex is None:
        raise SystemExit("crisis.json has no patterns, nothing to measure")

    misses, false_alarms, hits, quiet = [], [], 0, 0
    per_category = {}
    for row in rows:
        fired = bool(regex.search(row["text"]))
        want = row["label"] == "crisis"
        cat = row.get("category", "uncategorised")
        bucket = per_category.setdefault(cat, {"n": 0, "correct": 0})
        bucket["n"] += 1
        if fired == want:
            bucket["correct"] += 1
        if want and fired:
            hits += 1
        elif want:
            misses.append(row)
        elif fired:
            false_alarms.append(row)
        else:
            quiet += 1

    n_pos = hits + len(misses)
    n_neg = quiet + len(false_alarms)
    recall = hits / n_pos if n_pos else 0.0
    precision = (hits / (hits + len(false_alarms))) if (hits + len(false_alarms)) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    print("set        %s (%d rows: %d crisis, %d ordinary)"
          % (pathlib.Path(a.set).name, len(rows), n_pos, n_neg))
    print("patterns   %d from %s" % (len(patterns), crisis_path))
    print()
    print("recall     %.3f   (%d/%d crisis questions detected, floor %.2f)"
          % (recall, hits, n_pos, a.min_recall))
    print("precision  %.3f   (%d false positives out of %d ordinary questions, floor %.2f)"
          % (precision, len(false_alarms), n_neg, a.min_precision))
    print("f1         %.3f" % f1)

    print("\nby category")
    for cat in sorted(per_category):
        b = per_category[cat]
        print("  %-18s %3d/%3d" % (cat, b["correct"], b["n"]))

    if misses:
        print("\nMISSES (%d): the safety note would not have been injected" % len(misses))
        for row in (misses if a.show <= 0 else misses[:a.show]) or misses:
            print("  [%s] %s" % (row.get("category", "?"), row["text"]))
            if a.show > 0 and len(misses) > a.show:
                break
        if a.show > 0 and len(misses) > a.show:
            print("  ... %d more" % (len(misses) - a.show))

    if false_alarms:
        print("\nFALSE POSITIVES (%d): safety resources shown on an ordinary question"
              % len(false_alarms))
        for row in (false_alarms if a.show <= 0 else false_alarms[:a.show]):
            print("  [%s] %s" % (row.get("category", "?"), row["text"]))

    if a.json_out:
        pathlib.Path(a.json_out).write_text(json.dumps({
            "set": str(a.set),
            "client": str(a.client),
            "patterns": len(patterns),
            "n_crisis": n_pos,
            "n_ordinary": n_neg,
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "f1": round(f1, 4),
            "misses": [r["text"] for r in misses],
            "false_positives": [r["text"] for r in false_alarms],
        }, indent=2) + "\n")

    failures = []
    if recall < a.min_recall:
        failures.append("recall %.3f is below the floor %.2f" % (recall, a.min_recall))
    if precision < a.min_precision:
        failures.append("precision %.3f is below the floor %.2f" % (precision, a.min_precision))
    if failures:
        print("\nFAIL")
        for f in failures:
            print(" - " + f)
        return 1
    print("\nOK: crisis detector meets its floors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
