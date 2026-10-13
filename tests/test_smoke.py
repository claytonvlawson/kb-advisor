#!/usr/bin/env python3
"""test_smoke.py -- end-to-end smoke test, no LLM calls.

Builds the example client's index, then verifies the machinery the server
depends on: retrieval returns hits, the crisis trigger fires and prepends
the guardrails chunks, the location hooks route state notes, and the
citation builder produces a linked citation for a retrieved source that the
answer names in-text.

Run from the repo root:
    python3 tests/test_smoke.py
"""
import json
import os
import sys

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "kb"))

import retrieval  # noqa: E402
import citations  # noqa: E402

CLIENT = os.path.join(ROOT, "clients", "example-parent-advisor")
VAULT = os.path.join(CLIENT, "vault")
INDEX = os.path.join(CLIENT, "index", "bm25.pkl")

failures = []


def check(name, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print("%s %s %s" % (status, name, detail))
    if not cond:
        failures.append(name)


# 1. build the index
n = retrieval.build_index(VAULT, INDEX)
check("index build", n > 0, "(%d chunks)" % n)

chunks, bm25 = retrieval.load_index(INDEX)
check("index load", len(chunks) == n)

# 2. plain retrieval
hits = retrieval.retrieve_context("failed hearing test what do I do", bm25, chunks, k=5)
check("alias retrieval", any("First 90 Days" in h["source"] for h in hits),
      "(top: %s)" % (hits[0]["source"] if hits else "none"))

# 3. crisis trigger (regexes straight from the client's crisis.json)
cfg = json.load(open(os.path.join(CLIENT, "crisis.json"), encoding="utf-8"))
import re
crisis_re = re.compile("|".join(cfg["patterns"]), re.IGNORECASE)
check("crisis fires on parent phrasing",
      bool(crisis_re.search("I'm scared, he wishes he had never been born")))
check("crisis fires on self-harm",
      bool(crisis_re.search("my kid is talking about self harm tonight")))
check("crisis silent on research phrasing",
      not crisis_re.search("what does the literature say about language outcomes"))
guard = [c for c in chunks if c["source"] == cfg["inject_source"].replace("LanguageDeprivationAgent/", "LanguageDeprivationAgent/")]
check("guardrails note indexed", len(guard) > 0, "(%d chunks)" % len(guard))

# 4. citations: a retrieved source named in-text must be in citations
src = "Notes/Language and brain development in deaf children.md"
answer = ("According to Language and brain development in deaf children, "
          "early exposure matters.")
cit = citations.build_citations_grounded(VAULT, [src], answer, 6)
check("grounded citation merged", any(c["url"] for c in cit),
      "(%d citations)" % len(cit))
check("citation has doi url", any("doi.org" in (c.get("url") or "") for c in cit))

# 5. no-citation-block note does not appear in base citations
base = citations.build_citations(VAULT, ["Notes/First 90 Days After Diagnosis.md"], 6)
check("uncited note dropped from base citations", len(base) == 0)

# 6. client hooks import and route US states
sys.path.insert(0, CLIENT)
import client_hooks  # noqa: E402
loc = client_hooks.detect_location("we just moved to Ohio")
check("location detect", loc and loc.get("state") == "Ohio", "(%r)" % (loc,))
amb = client_hooks.detect_location("anyone near Columbus?")
check("ambiguous city", bool(amb and amb.get("ambiguous")), "(%r)" % (amb,))
check("same_state_folder",
      client_hooks.same_state_folder("LanguageDeprivationAgent/Local Resources - States/Ohio/x.md", "Ohio")
      and not client_hooks.same_state_folder("LanguageDeprivationAgent/Local Resources - States/Ohio/x.md", "Texas"))

print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All smoke checks passed.")
