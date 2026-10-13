# kb-advisor

A template RAG stack for spinning up grounded, cited, safety-configurable
knowledge-base advisors from PDFs. Ported from the production learnwithclay.ai
Parent Advisor, which turns an Obsidian research vault (~1300 notes) into a
public RAG agent with deterministic crisis routing and linkable citations.

The goal of this template: **drop in a client's PDFs, write a persona, get a
cited advisor API.**

```
  [1] PDFs / markdown        documents on your machine
        |
        |  [2] ingest/ingest_pdfs.py          PDF -> markdown notes
        |     (or bring your own markdown)
        v
  [3] clients/<name>/vault/             one knowledge base per client
        |
        |  [4] scripts/build_index.sh    heading-aware chunker
        |                                 -> clients/<name>/index/bm25.pkl
        v
  [5] BM25Okapi in memory              rebuilt every boot, ~5s / 8k chunks
        |
        v
  [6] kb/kb_server.py                  persona + crisis inject + citations
        |     clients/<name>/system_prompt.md, crisis.json, client_hooks.py
        v
  [7] kb/llm_router.py                 OpenRouter free tier -> paid fallback
        |
        v
  [8] Caddy -> your front end          {id, answer, sources, citations}
```

## What a client is

Each directory under `clients/` is one knowledge base:

```
clients/<name>/
  system_prompt.md    persona, tone, terminology, referral rules (REQUIRED)
  crisis.json         safety trigger regexes + the vault note to inject (optional)
  client_hooks.py     optional behavior plugs (e.g. US-state location routing)
  vault/              the markdown knowledge base
  index/bm25.pkl      built, gitignored
  logs/               qa/feedback/crisis JSONL
```

The server core never changes between clients. A new client is a config
directory, not a fork of the server.

## Quickstart: a new knowledge base in 4 commands

```sh
# 1. scaffold
./scripts/new_client.sh acme "You are an advisor for Acme customers."

# 2. ingest PDFs (each PDF -> one note with frontmatter + full citation)
python3 ingest/ingest_pdfs.py ~/Documents/acme-pdfs -o clients/acme/vault/Notes

# 3. build the index
./scripts/build_index.sh acme

# 4. run (needs OPENROUTER_API_KEY for generation)
export OPENROUTER_API_KEY=sk-or-...
./scripts/run_client.sh acme 8001
```

Then:

```sh
curl http://127.0.0.1:8001/health
curl -X POST http://127.0.0.1:8001/chat \
  -H 'Content-Type: application/json' \
  -d '{"question": "what is this knowledge base about"}'
```

Set `ADVISOR_TOKEN` to require `Authorization: Bearer <token>` on POST routes.

## The design rules it inherits from production

These are not stylistic; each one is a production incident turned into an
invariant. If you change them, know what you are trading away.

1. **Crisis handling is injection, not ranking.** `crisis.json` regexes match
   the *user's* phrasing, and the safety note's chunks are prepended to
   context regardless of BM25 score. Boosting was tested in production and
   lost to the literature every time (16-24 vs 64-78). A regex trigger is
   deterministic and offline testable. A false positive degrades gracefully
   (safety resources plus the real answer); a false negative fails silently.

2. **Grounding contract is absolute.** "Answer ONLY from the provided
   context." The crisis number gets INTO the context by injection, never by
   loosening the contract -- loosening it trades a safety bug for a
   hallucination surface across the whole corpus.

3. **Formatting invariants live in code, not prompts.** Em dashes are stripped
   by regex post-processing because models drop prompt instructions under
   real traffic. Any rule that must hold 100% of the time belongs in
   post-processing.

4. **A paper quoted in-text always appears in citations.**
   `citations.build_citations_grounded` force-merges any retrieved source the
   answer names verbatim, plus vault-wide author-year/person matches, so the
   citation list is guaranteed to reference what the answer actually said.
   The prompt clause is a hint; the code-side merge is the guarantee.

5. **Corpus shape is a retrieval parameter.** Short (<=63 line), heading
   dense, alias-rich notes retrieve better than a long one; the ingest script
   promotes PDF headings so the chunker has structure to work with. The
   220-word heading-aware chunker has no overlap by design -- it relies on
   the notes being short and heading dense.

6. **Never assume the user's location/context.** Retrieved examples are
   examples. The persona clause (and the example client's hooks) keep
   another state's phone numbers out of the answer.

7. **BM25 only, no dense half.** A 1GB VM with no GPU serves this. The known
   cost is vocabulary-gap recall (a user's words may share zero tokens with
   the corpus's words); aliases and `system_prompt.md` routing pay for most
   of it.

## PDF ingestion

`ingest/ingest_pdfs.py` converts each PDF into one note:

- frontmatter (`name`, `type: source`, `authors`, `year`, `doi`, `url`)
- a `## Full citation` block when a DOI or clean title/author/year is found
  (this is what makes the citation builder link the note)
- body text with headings guessed from font size, because the chunker is
  heading-aware

Scanned/image-only PDFs are reported and skipped, not fabricated. OCR them
separately, save the text as markdown, then rebuild.

The note format targets the stack's conventions, but any markdown works:
Obsidian exports, your own notes, AI-drafted summaries. Rebuild the index
after any vault change; there is no file watcher.

## Deploying

```sh
ssh vm 'mkdir -p /opt/kb-advisor'
rsync -az --exclude .git --exclude 'clients/*/index' --exclude 'clients/*/logs' \
  ./ vm:/opt/kb-advisor/
```

Then on the VM: `pip install rank_bm25 requests pymupdf` (ingest runs on your
machine, not the VM), copy `deploy/kb.env.example` to `/etc/kb-advisor.env`,
fill in `ADVISOR_TOKEN`, `OPENROUTER_API_KEY`, and `ADVISOR_CLIENT`, install
`deploy/kb-advisor.service`, and put Caddy in front (see `deploy/Caddyfile`).

Index load takes ~5s; poll `/health` after restart rather than curling once.
Set the environment file chmod 600. Put the front end's bearer token in the
edge function (Vercel/Netlify), never in the browser.

## Tests

```sh
pip install rank_bm25 requests
python3 tests/test_smoke.py          # end to end: index -> retrieve -> cite
python3 ingest/ingest_pdfs.py --help
```

## Known limits (inherited from production, deliberate)

- No incremental indexing: rebuild the whole index after vault edits.
- `MAX_TOKENS=1000` shared budget: reasoning-trace models emit empty answers
  (`finish_reason=length`); use non-reasoning models or a separate reasoning
  budget.
- The fallback retries only on hard errors; a slow-but-successful free tier
  never falls through to the paid one.
- `requests` timeout bounds bytes-between-reads, not total latency.
- qa_log stores full question text with no rotation/retention policy; make a
  retention decision before pointing this at real users.
- Crisis regex false negatives are unmeasured; build an eval before wide
  public exposure (see production ARCHITECTURE.md, roadmap item 2).

## Lineage

The retrieval core (`kb/retrieval.py`), the LLM router (`kb/llm_router.py`),
the citation machinery (`kb/citations.py`), and `kb/kb_server.py` are ports
of the live learnwithclay.ai Parent Advisor (`~/vault-advisor-cloud`, on the
`advisor-vm` droplet). That stack's full architecture, decisions, and paid
for traps list lives in its ARCHITECTURE.md.
