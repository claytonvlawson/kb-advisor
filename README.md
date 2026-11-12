# kb-advisor

[![tests](https://github.com/claytonvlawson/kb-advisor/actions/workflows/tests.yml/badge.svg)](https://github.com/claytonvlawson/kb-advisor/actions/workflows/tests.yml)

A template RAG stack for spinning up grounded, cited, safety-configurable
knowledge-base advisors from PDFs. Ported from the production learnwithclay.ai
Parent Advisor, which turns an Obsidian research vault (~1300 notes) into a
public RAG agent with deterministic crisis routing and linkable citations.


The goal of this template: **drop in a client's PDFs, write a persona, get a
cited advisor API** (and optionally a front-end widget).

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
        |
        |  [9] frontend/ (optional)     Vercel proxy + React widget
        v                                     see frontend/README.md
```

The optional `frontend/` directory holds the same-origin Vercel proxy pair
(`api/chat.js`, `api/feedback.js`, with the bearer token server-side and
per-IP rate limits), a drop-in React `AdvisorWidget.jsx` (persona as props),
and `vercel.json` with `functions.maxDuration` set -- a production gap that
surfaced as intermittent "spotty" failures. See frontend/README.md.

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

Set `ADVISOR_TOKEN` to require an Authorization header carrying the bearer token on
the POST routes and on `/health/deep`. Auth fails closed: an empty token or the
shipped example placeholder rejects every request with 401 and prints a startup
warning. For local development only, set `ADVISOR_ALLOW_ANON=1` to run without a
token.

## HTTP API

Four routes, all JSON. `GET /health` is public; everything that spends money or
writes data requires `Authorization: Bearer $ADVISOR_TOKEN`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | no | liveness + chunk count, no generation |
| GET | `/health/deep` | yes | runs one real retrieval + generation |
| POST | `/chat` | yes | ask a question, get a cited answer |
| POST | `/feedback` | yes | record an up/down vote on an answer id |

`GET /health`

```json
{"ok": true, "chunks": 2431}
```

`GET /health/deep` -- costs one generation. Use it after a deploy, not as a
liveness probe.

```json
{"ok": true, "chunks": 2431, "generation_ok": true, "ms": 1840, "sources": 5}
```

`POST /chat` -- body limit 10 KB, question truncated at `ADVISOR_MAX_Q` (500).

```sh
curl -X POST http://127.0.0.1:8001/chat \
  -H "Authorization: Bearer $ADVISOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question": "my 2 year old just failed a hearing test, what now"}'
```

```json
{
  "id": "9f3c1a7b20de",
  "answer": "The first 90 days are mostly about audiology and early intervention...",
  "sources": ["Notes/First 90 Days After Diagnosis.md", "..."],
  "citations": [
    {"title": "Language and brain development in deaf children",
     "authors": "Hall, M.", "year": "2019", "url": "https://doi.org/10.1093/deafed/enz012"}
  ],
  "location": null,
  "location_ambiguous": null
}
```

`id` is a 12-char hex handle for the answer; pass it to `/feedback`. `sources`
are vault-relative paths. `citations` always includes any retrieved note the
answer named in-text (see design rule 4).

`POST /feedback`

```sh
curl -X POST http://127.0.0.1:8001/feedback \
  -H "Authorization: Bearer $ADVISOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"id": "9f3c1a7b20de", "vote": "down", "note": "wrong state, gave me a Texas number"}'
```

```json
{"ok": true}
```

`vote` must be `up` or `down`; an optional `note` is truncated at
`ADVISOR_MAX_NOTE` (1000 chars). Rows are appended to
`clients/<name>/logs/{qa_log,feedback}.jsonl`.

Errors are `{"error": "..."}` with a matching status: `400` for an empty
question, malformed JSON or an oversized body; `401` for a missing, wrong or
unconfigured token; `404` for an unknown route; `502` when retrieval or
generation produced nothing. A 502 body never contains internal exception text
or a model key.


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

66 tests, offline, no model calls, about 2 seconds. `requests.post` is faked and
the HTTP surface is exercised over a real socket on an ephemeral port.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

| File | What it pins down |
|---|---|
| `tests/test_retrieval.py` | tokenizer stays compatible with the index, chunk round trip, ranking, k, no zero-score hits |
| `tests/test_citations.py` | uncited notes are dropped, in-text-named notes are force-merged, DOI/URL safety |
| `tests/test_crisis.py` | the safety path fires on real parent phrasing and stays quiet on ordinary questions, inject source is indexed, misconfiguration is loud |
| `tests/test_llm_router.py` | free tier serves first, paid tier only on 429/5xx, both-failed reports a machine-readable error |
| `tests/test_server.py` | auth fails closed on `/chat`, `/feedback` and `/health/deep`; request/response shapes; logging; no exception text leaks |
| `tests/test_hooks.py` | the example client's US-state routing |

CI runs the suite on Python 3.11 and 3.12 (`.github/workflows/tests.yml`).

## Status: what works, what is not done

Working end to end, in use on a live client deployment: PDF ingest, heading-aware
chunking, BM25 retrieval, persona-driven generation with two-tier fallback,
deterministic crisis injection, in-text citation grounding, optional per-client
hooks, JSONL logging, systemd + Caddy deploy, Vercel proxy + React widget.

Not done, in rough priority order:

1. **Crisis recall is unmeasured.** The regexes are hand-written; there is no
   eval set of real phrasings, so false negatives are unknown. This is the one
   gap I would not ship to a wide public audience without fixing.
2. **No incremental indexing.** Any vault edit means a full rebuild (~5s per
   8k chunks).
3. **BM25 only.** No embeddings, by design (a 1 GB box with no GPU serves this).
   The cost is vocabulary-gap recall.
4. **No log retention policy.** `qa_log.jsonl` stores full question text
   indefinitely; decide retention before pointing this at real users.
5. **No streaming.** Answers arrive whole; long generations block until done.
6. **One deployment topology.** No container image, no Terraform; the deploy
   section is rsync plus systemd.

## Known limits (inherited from production, deliberate)

- `MAX_TOKENS=1000` shared budget: reasoning-trace models emit empty answers
  (`finish_reason=length`); use non-reasoning models or a separate reasoning
  budget.
- The fallback retries only on hard errors; a slow-but-successful free tier
  never falls through to the paid one.
- `requests` timeout bounds bytes-between-reads, not total latency.

## Lineage

The retrieval core (`kb/retrieval.py`), the LLM router (`kb/llm_router.py`),
the citation machinery (`kb/citations.py`), and `kb/kb_server.py` are ports of
the live learnwithclay.ai Parent Advisor, which serves hearing parents of Deaf
and hard of hearing children. The client-specific parts of that stack (persona,
crisis regexes, location routing) were lifted out into the per-client config
directory you see under `clients/`. The chunker and tokenizer are kept
byte-identical to the originals on purpose: an index built by one copy has to be
searchable by the other.

## License

MIT. See [LICENSE](LICENSE). Documents you ingest keep their own terms; this
license covers the code only.

