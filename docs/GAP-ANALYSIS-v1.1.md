# Gap analysis for v1.1

Reviewed: `dcf5390` on 2026-11-12, a senior-engineer read of the whole repo, not a
skim. Everything below was produced by running the thing, and each finding names
the command or the file that shows it. Nothing here is a style opinion.

## Verdict

The core is sound and unusually honest for a template: the HTTP surface behaves as
documented, auth fails closed, the index carries a tokenizer version, and the
design rules are recorded as invariants rather than vibes. It is ready to be read
by an outside engineer today.

It is not ready to be handed to a client who will put it in front of real users,
and the blockers are not in the retrieval code. They are in the three places a
template gets copied into production without review: **crisis coverage is
unmeasured**, **client-supplied regexes run unbounded against user input**, and
**nothing bounds spend, threads, or log growth on the box the README tells you to
deploy it on.**

## What holds up, with evidence

| Claim | How it was checked | Result |
|---|---|---|
| Tests are real and fast | clean clone, fresh venv, `python -m pytest` | 66 passed in 1.4 s, Python 3.9 |
| Quickstart builds an index | `./scripts/build_index.sh example-parent-advisor` | 4 files, 4 chunks, pkl written |
| Server runs and answers | `kb/kb_server.py` with `ADVISOR_ALLOW_ANON=1` | `/health` returns `{"ok": true, "chunks": 4}` |
| Body cap works | 20 KB POST to `/chat` | `400`, connection closed |
| Unknown route | `GET /nope` | `404` |
| No exception text leaks | POST `/chat` with no model configured | `502 {"error": "The advisor could not answer that from the knowledge base."}` |
| Auth fails closed | read of `_authorized`, `token_configured`, `PLACEHOLDER_TOKENS` | empty or placeholder token denies; `hmac.compare_digest` |
| Index compatibility is enforced | `retrieval.py` load path | refuses on `tokenizer_version` mismatch, `version 5` |
| No committed secrets or home paths | `git grep -n -e /home/ -e /Users/ -e /root/`, secret sweep | no hits |
| Repo is small | `git ls-files \| xargs du -h \| sort -h \| tail` | largest file 32 KB, no data in git |

That list is why the gaps below are worth fixing rather than rewriting.

---

## P0: blocks a v1.1 that a client can deploy

### 1. Crisis recall is unmeasured

**Status: measured and gated.** `eval/crisis_set.jsonl` (121 labelled rows: 61
crisis, 60 ordinary including idioms and near-misses), `scripts/eval_crisis.py`, and
a CI job that fails below 0.95 recall or 0.90 precision. Baseline on the shipped
patterns was **recall 0.344, precision 1.000**: 40 of 61 crisis questions would have
been missed silently. After rewriting the trigger set (14 to 47 patterns):
**recall 0.984, precision 1.000**. The set is hand-authored rather than harvested
from traffic, and patterned injury is a documented miss.

`clients/example-parent-advisor/crisis.json` holds 14 hand-written patterns. There
is no labelled set of real phrasings, so the false-negative rate is unknown, and a
false negative here is a safety event that fails silently. The README already says
this is the one gap that should not ship to a wide audience; it is still the top
item because nothing else in the repo has that property.

**Acceptance criteria**
- `eval/crisis_set.jsonl`: labelled questions, positive (should trigger) and
  negative (ordinary parenting questions that must not), sourced from real
  phrasings rather than invented ones.
- `scripts/eval_crisis.py` reporting recall, precision and the specific misses.
- A CI gate on recall, in the same shape as the sign-stack eval gate: the number
  is checked on every push, or it drifts.
- A documented floor, for example "recall at or above 0.95 on the labelled set",
  with the misses listed in the report so nobody assumes zero.

### 2. Client-supplied regexes run unbounded against user input

**Status: fixed.** `kb/regex_safety.py` rejects provably risky patterns at load
time (naming the pattern, exit code 2), bounds every match with a wall clock
limit, fails closed on timeout, and `ADVISOR_MAX_THREADS` caps concurrent
handlers with backpressure. Covered by `tests/test_regex_safety.py` (25 tests,
including a live six-request run against a cap of two).

`kb/kb_server.py` compiles every client pattern into one alternation and calls
`.search()` on the raw question, with no timeout and no complexity check. The
current 14 patterns are tame, but the mechanism is inherited per client. A pattern
a non-expert would plausibly write is catastrophic:

```
pattern  (\w+\s?)+[.!?]        (a "sentence" matcher)
  16 chars unpunctuated ->     7.4 ms
  20 chars                ->    69.2 ms
  24 chars                ->   951.3 ms
```

`ADVISOR_MAX_Q` is 500, so a single request can occupy a worker for effectively
forever, and `ThreadingHTTPServer` spawns a thread per connection with no cap.
That is a self-inflicted denial of service from one bad line of client config.

**Acceptance criteria**
- Reject or rewrite nested-quantifier patterns at load time, with a clear startup
  error naming the offending pattern.
- Bound the match: pre-screen with cheap literal/regex-free triggers, or run the
  regex in a worker with a wall-clock limit, and skip the injection path on
  timeout while logging loudly. Never block the request thread.
- Cap concurrent handlers (a semaphore around `answer_question`) so one slow
  request cannot exhaust the box.
- A regression test that feeds a pathological pattern and asserts the request
  returns inside a bounded time.

### 3. Nothing server-side bounds spend

`/chat` calls a paid model. Rate limiting exists only in `frontend/api/chat.js`,
which stores hits in a module-level `Map`: it resets on every cold start, is not
shared across instances, and does not apply at all to anything that talks to the
port directly. `deploy/Caddyfile` is nine lines and sets no body limit, no rate
limit and no security headers. The stated deployment is a 1 GB VM, which one
scripted client can saturate.

**Acceptance criteria**
- Server-side token bucket keyed by client identity (token, or IP from a trusted
  proxy header), with configurable requests/minute and a hard cap on concurrent
  generations.
- Edge defence in `Caddyfile`: request body max size, a rate limit, HSTS and the
  usual headers, and access logging.
- A test that the limiter returns 429 and that the limit is not bypassed by
  changing a header.

### 4. Logs hold user text forever and grow without bound

`_append_jsonl` appends full question, full answer and citations to
`clients/<name>/logs/qa_log.jsonl`, and crisis hits to `crisis_log.jsonl`. There is
no rotation, no retention, no redaction and no delete path. For an audience of
parents of Deaf and hard of hearing children this is sensitive text, and on a
1 GB box it is also a disk-full outage waiting for traffic.

**Acceptance criteria**
- Rotation by size and count, with the limits configurable.
- A retention window enforced (a small prune on write, or logrotate in the unit).
- Optional redaction of question text (hash or truncate) for deployments that do
  not need it, plus the id kept for feedback correlation.
- A written retention policy in the README that matches the code.

---

## P1: should be in v1.1

### 5. Deployment hardening

`deploy/kb-advisor.service` sets `User`, `NoNewPrivileges` and `PrivateTmp`, and
stops there. Missing, on a box the README calls out as 1 GB:

- `ProtectSystem=strict` with `ReadWritePaths` for the log directory, `ProtectHome`,
  `PrivateDevices`, `RestrictAddressFamilies`.
- `MemoryMax` and `MemoryHigh`, so a runaway generation kills the service and not
  the VM.
- `Restart=always` (currently `on-failure`, so a clean exit stays down), plus
  `WatchdogSec` or `ExecStartPost` polling `/health`.
- `LimitNOFILE` and `TasksMax` to match the thread cap from P0.3.

### 6. Reproducibility

`requirements.txt` and `pyproject.toml` use `>=` ranges with no lock or constraints
file, so two installs a month apart can differ. `requires-python = ">=3.9"` while
CI tests only 3.11 and 3.12; the suite does pass on 3.9 (checked here), so this is
an unverified claim rather than a false one.

**Acceptance criteria:** a `constraints.txt` (or `requirements.lock`) naming the
versions the deployment runs, and a CI job on 3.9 or a raised floor with the
matrix updated to match.

### 7. No static checks or coverage gate

CI runs pytest and nothing else. There is no ruff, no mypy, no coverage floor.
For a repo whose stated purpose is to be copied per client, a lint and type gate is
the cheapest way to keep forks from rotting.

### 8. The PDF ingestion path is untested

`ingest/ingest_pdfs.py` is the only component that parses untrusted third-party
files, and it has no test file at all; the README's test map does not mention it.
It does skip failures rather than fabricate text, which is the right behaviour and
worth pinning.

**Acceptance criteria:** a tiny checked-in PDF fixture (one text PDF, one
image-only, one malformed), tests for the frontmatter contract, the heading
promotion, and the skip-with-reason path.

### 9. No versioned release

`git tag` is empty and there are no releases. pyproject says 1.0.0 but nothing is
pinned to a tag, so "the version you reviewed" is unfalsifiable and every client
fork starts from `main`.

**Acceptance criteria:** a `v1.0.0` tag on the reviewed commit, a `CHANGELOG.md`
with an entry per tag, and a rule that config-incompatible changes bump the minor.

### 10. No security or contribution policy

No `SECURITY.md`, `CONTRIBUTING.md`, or issue templates. Anyone who finds a hole
(auth bypass, injection, ReDoS) has no disclosed channel.

### 11. Multi-client operations are undefined

One process serves one client, selected by `ADVISOR_CLIENT`, and auth is a single
global `ADVISOR_TOKEN`. Serving five clients means five units and five tokens that
must not be confused, and any deployment that reuses a token exposes every client
behind it. Worth stating explicitly, or supporting a per-client token map.

---

## P2: worth doing, not release-blocking

12. **Dead response fields.** `_handle_chat` always returns `location: null` and
    `location_ambiguous: null`, even when a hook resolved a location. Either
    populate them or drop them from the contract before a client builds on them.
13. **No machine-readable API spec.** The README table is the contract. A generated
    `openapi.json` would let clients typecheck their integration.
14. **Observability is `print()`.** No version in `/health`, no request id in the
    process log, no latency, model or fallback counters. The qa log has an id; the
    log line does not.
15. **Prompt injection via ingested PDFs.** Retrieved chunks are inserted into the
    prompt with no instruction-stripping or provenance marking. In scope for a
    template whose selling point is "drop in a client's PDFs".
16. **The index is a pickle.** `pickle.load` on a file means code execution if the
    file is untrusted. It is trusted in this design, but say so in the README, or
    move to `npz` plus a JSON manifest.
17. **Frontend has no tests and no accessibility checks.** The widget carries 17
    aria attributes and `role="dialog"`, which is a good start; focus trapping,
    keyboard dismissal and an `aria-live` announcement for a finished answer are
    unverified.

## Deliberate limits, restated so they are not "found" later

BM25 with no dense half, no streaming, one deployment topology, a shared 1000-token
generation budget, and a fallback that triggers only on hard errors are all
documented tradeoffs. They are fine. The difference between these and the items
above is that these are decisions, and the P0 list is unmeasured risk.

## Proposed v1.1 scope

Ship these seven, in this order, and the template is defensible in front of a
client's own engineer:

1. Crisis eval set plus a CI recall gate (P0.1)
2. Regex safety: load-time validation, bounded match, thread cap (P0.2)
3. Server-side rate limiting plus Caddy body limit and headers (P0.3)
4. Log rotation, retention, redaction (P0.4)
5. systemd hardening: ProtectSystem, MemoryMax, Restart=always (P1.5)
6. Ingest tests with fixtures (P1.8)
7. Tag `v1.0.0` on the reviewed commit plus CHANGELOG, then cut `v1.1.0` with the
   above (P1.9)

## Reproducing this review

```sh
git clone https://github.com/claytonvlawson/kb-advisor && cd kb-advisor
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                                  # 66 passed, ~1.4 s
./scripts/build_index.sh example-parent-advisor    # 4 files, 4 chunks
ADVISOR_CLIENT=clients/example-parent-advisor ADVISOR_ALLOW_ANON=1 \
  ADVISOR_PORT=8099 python3 kb/kb_server.py &
curl -s http://127.0.0.1:8099/health              # {"ok": true, "chunks": 4}
# body cap, 404, and the generic 502 all behave as the README says
```
