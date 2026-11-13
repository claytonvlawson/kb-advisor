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

All POST routes and `/health/deep` are rate limited per client (`429` with
`Retry-After`), and generations are capped (`503` when saturated). See
[Spend and pressure](#spend-and-pressure).

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
   (safety resources plus the real answer); a false negative fails silently,
   which is why the trigger is measured against a labelled set and gated in CI
   (see [Crisis eval](#crisis-eval)).

2. **Grounding contract is absolute.** "Answer ONLY from the provided
   context." The crisis number gets INTO the context by injection, never by
   loosening the contract -- loosening it trades a safety bug for a
   hallucination surface across the whole corpus.

3. **Logs are bounded and redacted on write.** Rotation, retention and
   redaction live in `kb/logio.py`, not in a cron job someone has to remember.
   The default mode strips emails, phone numbers, long digit runs, URLs with
   query strings and @handles; `full` is an opt-in that needs a notice and
   consent, `hash` and `none` exist for stricter deployments.

4. **Formatting invariants live in code, not prompts.** Em dashes are stripped
   by regex post-processing because models drop prompt instructions under
   real traffic. Any rule that must hold 100% of the time belongs in
   post-processing.

5. **A paper quoted in-text always appears in citations.**
   `citations.build_citations_grounded` force-merges any retrieved source the
   answer names verbatim, plus vault-wide author-year/person matches, so the
   citation list is guaranteed to reference what the answer actually said.
   The prompt clause is a hint; the code-side merge is the guarantee.

6. **Corpus shape is a retrieval parameter.** Short (<=63 line), heading
   dense, alias-rich notes retrieve better than a long one; the ingest script
   promotes PDF headings so the chunker has structure to work with. The
   220-word heading-aware chunker has no overlap by design -- it relies on
   the notes being short and heading dense.

7. **Never assume the user's location/context.** Retrieved examples are
   examples. The persona clause (and the example client's hooks) keep
   another state's phone numbers out of the answer.

8. **BM25 only, no dense half.** A 1GB VM with no GPU serves this. The known
   cost is vocabulary-gap recall (a user's words may share zero tokens with
   the corpus's words); aliases and `system_prompt.md` routing pay for most
   of it.

9. **Client regexes are validated, and every match is bounded.** `crisis.json`
   is config, and `re.search` has no timeout, so a pattern like
   `(\w+\s?)+[.!?]` is a denial of service rather than a safeguard (7 ms at 16
   characters, 951 ms at 24). `kb/regex_safety.py` refuses provably risky
   shapes at startup, naming the pattern, and runs the match under a wall clock
   limit. A timeout fails closed (safety note injected) and disables the
   detector for the process with a CRITICAL log, because a thread cannot be
   killed. Concurrent handlers are capped at `ADVISOR_MAX_THREADS` (8) with
   backpressure instead of unbounded threads.

## Spend and pressure

`/chat` costs money and each in-flight generation holds prompt buffers, so both
are bounded in the server, not only at the edge:

| setting | default | meaning |
|---|---|---|
| `ADVISOR_RATE_PER_MIN` | 30 | token bucket per client; `0` disables |
| `ADVISOR_TRUST_PROXY` | off | take the client identity from `X-Forwarded-For` |
| `ADVISOR_MAX_GENERATIONS` | 2 | concurrent generations |
| `ADVISOR_GEN_WAIT_S` | 10 | how long a request waits for a slot before `503` |

Over the bucket a request gets `429` with `Retry-After`. Over the generation cap
it waits, then gets `503` with `Retry-After` rather than piling up threads and
paid requests. Auth is checked first, so exhausting a bucket never becomes a way
around the token.

**Identity is the part that matters.** `X-Forwarded-For` is client-controlled
until a proxy overwrites it, so it is only read with `ADVISOR_TRUST_PROXY=1`,
and the shipped `deploy/Caddyfile` sets `header_up X-Forwarded-For
{remote_host}` (replace, not append). Without that, a client could mint a fresh
bucket per request by changing a header, which would make the limiter decoration.

`deploy/Caddyfile` also caps request bodies at 16 KB, sets HSTS and the usual
headers, and logs access with rotation. An edge rate limit is included but
commented out, because it needs the `caddy-ratelimit` plugin; the app's limiter
is the control that always exists, including for anything talking to the port
directly.

## Logs and retention

Three JSONL files per client (`qa_log`, `feedback`, `crisis_log`) hold questions,
answers and your feedback notes. `kb/logio.py` bounds every one of them:

| setting | default | meaning |
|---|---|---|
| `ADVISOR_LOG_MAX_BYTES` | 5 MB | rotate before a file grows past this |
| `ADVISOR_LOG_BACKUPS` | 3 | keep this many rotated generations (20 MB per log at the defaults) |
| `ADVISOR_LOG_RETENTION_DAYS` | 30 | delete rotated files older than this, checked at most every 5 minutes |
| `ADVISOR_LOG_QUESTION` | `redact` | what is written for `question`, `answer` and `note` |

Modes: `redact` (default) strips emails, phone numbers, card and long digit runs,
URLs with query strings and @handles; `full` stores the text verbatim and needs a
notice plus consent; `hash` stores a `sha256:` prefix and length with no text;
`none` stores nothing.

**The retention window, not the redaction, is the real limit on exposure.** The
server keeps the id on every row so a feedback vote still correlates with an
answer after the text is gone. Set `ADVISOR_LOG_QUESTION=hash` if you do not need
to read questions back, and state your own retention period in your privacy
notice.

### Growing the crisis set from real traffic

`crisis_log.jsonl` is the useful signal: it is every question the trigger fired
on. Turning it into eval data is a deliberate, two-step act:

```sh
python3 scripts/crisis_review.py \
  --log clients/example-parent-advisor/logs/crisis_log.jsonl \
  --out eval/candidates.jsonl \
  --consent "approved by <who>, question text is redacted"
```

The script refuses without `--consent`, redacts before writing, drops
near-duplicates and questions already in the labelled set, and writes every row
with an **empty label**. A person decides crisis versus idiom versus ordinary
distress, then appends the sure ones to `eval/crisis_set.jsonl` and re-runs the
eval. Nothing is auto-labelled: that would only re-teach the detector what it
already believes.

## Crisis eval

The safety trigger is the one path where a regression is silent, so it is
measured. `eval/crisis_set.jsonl` holds 121 labelled questions: 61 crisis
phrasings across the categories that matter (ideation, self-harm, caregiver
risk, abuse, household safety, neglect, medical emergency) and 60 ordinary
parenting questions built to false-positive, including idioms ("this is killing
me, he wakes up every two hours") and near-misses ("she hurt her ankle at
soccer").

```sh
python3 scripts/eval_crisis.py
```

```
recall     0.984   (60/61 crisis questions detected, floor 0.95)
precision  1.000   (0 false positives out of 60 ordinary questions, floor 0.90)
```

CI runs the same script and fails the build below either floor
(`.github/workflows/tests.yml`, job `crisis-eval`).

**Where the set came from, and what it is not.** It is hand-authored from how
parents actually phrase this (idioms included), not a clinical instrument and
not yet harvested from traffic. The honest next step is to grow it from
`qa_log.jsonl` with consent and re-measure. One known miss is deliberate: "the
baby has a bruise in the shape of a hand" is a patterned-injury description, and
a regex is the wrong detector for it.

## PDF ingestion

`ingest/ingest_pdfs.py` converts each PDF into one note:

- frontmatter (`name`, `type: source`, `authors`, `year`, `doi`, `url`)
- a `## Full citation` block when a DOI or clean title/author/year is found
  (this is what makes the citation builder link the note)
- body text with headings guessed from font size, because the chunker is
  heading-aware

Scanned/image-only PDFs are reported and skipped, not fabricated. OCR them
separately, save the text as markdown, then rebuild. A truncated or corrupt PDF
is reported as exactly that, not as "needs OCR": MuPDF silently repairs a bad
download into a document with no text, so the two cases are told apart before
the message is written.

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
`deploy/kb-advisor.service` and `deploy/wait_for_health.sh`, and put Caddy in
front (see `deploy/Caddyfile`).

The unit is written for the 1 GB box this section describes, not for a laptop:

- `ProtectSystem=strict` with `ReadWritePaths=/var/lib/kb-advisor`, `ProtectHome`,
  `PrivateDevices`, `RestrictNamespaces`, `SystemCallArchitectures=native`, and
  `UMask=0077`. **`ReadWritePaths` must contain `ADVISOR_LOG_DIR`**, or the
  service starts and silently drops every log row.
- `MemoryMax=800M` / `MemoryHigh=600M`: a runaway generation kills the unit, not
  the VM. Resize against your own index (service RSS with 8k chunks sits near
  300 MB).
- `Restart=always`, so a clean exit is treated as a failure, with
  `StartLimitBurst=5` so a config that can never boot stays down and visible
  instead of flapping.
- `TasksMax=64` matched to `ADVISOR_MAX_THREADS`, because systemd's 4096-thread
  default is not a budget a 1 GB box can honour.
- `ExecStartPost=deploy/wait_for_health.sh` is the readiness gate: the unit is
  only "started" once `/health` answers, so a missing index fails the start
  instead of serving 502s.

Check it on the host with `systemd-analyze verify deploy/kb-advisor.service`
before restarting. `tests/test_deploy.py` asserts the hardening is still in the
file and that the unit and the app agree on the log path.

### Serving more than one client

**One process per client is the supported topology, and it is the only one.**
`deploy/kb-advisor@.service` is a template unit where the instance name is the
client id:

```sh
sudo rsync -az clients/acme/ /opt/kb-advisor/clients/acme/
sudo install -m 600 deploy/acme.env.example /etc/kb-advisor/acme.env   # ADVISOR_CLIENT + ADVISOR_PORT
sudo systemctl enable --now kb-advisor@acme
```

Each instance gets its own `ADVISOR_CLIENT`, port, `ADVISOR_TOKEN`, index in
RAM, log directory and crash domain. Isolation is the OS boundary, not a tenant
check in application code. `deploy/Caddyfile` shows the matching one-site-per-
client pattern (its own certificate, headers and access log).

**Size the box before adding a client.** Service RSS is about 300 MB per
instance with an 8k-chunk index, and the indexes are the cost. `deploy/kb-advisor.slice`
bounds the instances collectively at 750 MB, which is two clients on a 1 GB box
with ~250 MB left for the kernel and Caddy. A per-unit `MemoryMax` does not
bound the sum: three instances allowed 800 MB each can still take 2.4 GB and
OOM the machine. Add a third client by adding RAM first, and raise the slice
with it.

**What is deliberately not built:** multi-tenancy in one process. A per-client
token map would mean threading a tenant through `CLIENT_DIR`, `VAULT_ROOT`,
`INDEX_PATH`, `LOG_DIR`, `SYSTEM_PROMPT`, `CRISIS_CFG`, `HOOKS`, `CHUNKS` and
`BM25`, all currently module-level, plus one resident index per client anyway.
The memory does not improve, and isolation moves from the kernel to a code path
somebody has to audit. For a corpus of client documents that is strictly worse.

Token rotation stays per instance: issue a new `ADVISOR_TOKEN` in that client's
env file and `systemctl restart kb-advisor@<client>`. One client's rotation does
not restart anybody else.

Index load takes ~5s; poll `/health` after restart rather than curling once.
Set the environment file chmod 600. Put the front end's bearer token in the
edge function (Vercel/Netlify), never in the browser.

## Tests

157 tests, offline, no model calls, about 4 seconds. `requests.post` is faked and
the HTTP surface is exercised over a real socket on an ephemeral port.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest --cov            # 162 tests, coverage floor from pyproject
ruff check kb ingest scripts tests
mypy                              # kb/ only, configured in pyproject
```

CI runs the suite on 3.10, 3.11 and 3.12 with a **75% branch-coverage floor**
(currently 77% over `kb/`), ruff on `E4/E7/E9/W/F/B/I`, and mypy over `kb/`. The
lint set is deliberately bug-focused: no line-length or `%`-formatting churn,
because a template that rewrites 100 format strings teaches reviewers to ignore
the diff. Untyped defs are allowed; a wrong annotation is not.

| File | What it pins down |
|---|---|
| `tests/test_retrieval.py` | tokenizer stays compatible with the index, chunk round trip, ranking, k, no zero-score hits |
| `tests/test_citations.py` | uncited notes are dropped, in-text-named notes are force-merged, DOI/URL safety |
| `tests/test_crisis.py` | the safety path fires on real parent phrasing and stays quiet on ordinary questions, inject source is indexed, misconfiguration is loud |
| `tests/test_llm_router.py` | free tier serves first, paid tier only on 429/5xx, both-failed reports a machine-readable error |
| `tests/test_server.py` | auth fails closed on `/chat`, `/feedback` and `/health/deep`; request/response shapes; logging; no exception text leaks |
| `tests/test_hooks.py` | the example client's US-state routing |
| `tests/test_regex_safety.py` | risky crisis patterns rejected at startup, match bounded, detector disabled after a timeout, handler cap holds |
| `tests/test_crisis_set.py` | the labelled set is well formed and the detector still clears its recall and precision floors |
| `tests/test_logs.py` | redaction modes, rotation bounds, age-based pruning, and a live `/feedback` write whose note is scrubbed |
| `tests/test_ingest.py` | a text PDF becomes the expected note, an image-only page and a truncated file are skipped with distinct reasons, notes are never overwritten, and the note indexes cleanly |
| `tests/test_deploy.py` | unit hardening, Caddy body cap and forwarded-header handling, env documentation, and version/changelog/CI-floor agreement |
| `tests/test_ratelimit.py` | bucket refill and deny, bounded identity tracking, generation shedding, `429`/`503` shapes, and that a spoofed `X-Forwarded-For` buys nothing |
| `scripts/eval_crisis.py` | CI gate: measures recall and precision against the labelled set |

CI runs the suite on Python 3.11 and 3.12 (`.github/workflows/tests.yml`).

## Status: what works, what is not done

A full senior-engineer review with acceptance criteria per gap is in
[docs/GAP-ANALYSIS-v1.1.md](docs/GAP-ANALYSIS-v1.1.md). Short version: the
blockers for a client deployment are unmeasured crisis recall, unbounded
client-supplied crisis regexes, no server-side spend or concurrency limit, and
logs that store user text forever.

Working end to end, in use on a live client deployment: PDF ingest, heading-aware
chunking, BM25 retrieval, persona-driven generation with two-tier fallback,
deterministic crisis injection, in-text citation grounding, optional per-client
hooks, JSONL logging, systemd + Caddy deploy, Vercel proxy + React widget.

Not done, in rough priority order:

1. **Crisis recall is measured against a hand-authored set, not real traffic.**
   0.984 recall and 1.000 precision on `eval/crisis_set.jsonl` (121 rows), gated
   in CI. The set is written from how parents phrase these things, not harvested
   from logs, so treat it as a floor that catches regressions rather than a
   claim about the world. Patterned injury is a known miss.
2. **No incremental indexing.** Any vault edit means a full rebuild (~5s per
   8k chunks).
3. **BM25 only.** No embeddings, by design (a 1 GB box with no GPU serves this).
   The cost is vocabulary-gap recall.
4. **Logs are bounded and redacted, but redaction is not anonymisation.** Size
   rotation (5 MB x 4), a 30 day retention on rotated files and identifier
   stripping are in place. Names and free-text detail still survive, so the
   retention window is the real limit; set `ADVISOR_LOG_QUESTION=hash` or `none`
   if you do not need the text back.
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

