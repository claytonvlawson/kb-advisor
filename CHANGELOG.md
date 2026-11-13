# Changelog

Versions are tags on `main`. The rule from here on: a change that breaks an
existing `clients/<name>/` config (a crisis pattern the guard now rejects, a
retired env var, a changed response field) bumps the minor version, because a
deployment that upgrades in place is the normal case for a template.

## 1.1.0 (2026-11-12)

The release that closes every P0 from the [gap analysis](docs/GAP-ANALYSIS-v1.1.md).
1.0.0 is the reviewed baseline and is not safe to deploy; see the note there.

**Safety**
- Crisis detection is measured and gated. `eval/crisis_set.jsonl` (121 labelled
  questions, 61 crisis / 60 ordinary including idioms) with `scripts/eval_crisis.py`
  and a CI job. The shipped 1.0.0 patterns scored **0.344 recall**; the rewritten
  set scores **0.984 recall, 1.000 precision**. (#1)
- Client-supplied crisis regexes are validated at startup, every match is bounded
  by a wall clock limit, and a timeout fails closed and disables the detector with
  a CRITICAL log. A plausible pattern measured 951 ms on 24 characters. (#2)
- Concurrent handlers are capped (`ADVISOR_MAX_THREADS`, 8) with backpressure.

**Spend and pressure**
- Per-client token bucket (`ADVISOR_RATE_PER_MIN`, 30) returning `429` with
  `Retry-After`; concurrent generations capped (`ADVISOR_MAX_GENERATIONS`, 2)
  returning `503` after a bounded wait. Identity comes from `X-Forwarded-For` only
  behind a trusted proxy. (#3)

**Privacy**
- JSONL logs rotate at 5 MB with 3 backups, delete rotated files past 30 days, and
  redact `question`, `answer` and `note` on write (default mode `redact`; also
  `full`, `hash`, `none`). The row id survives so feedback still correlates. (#4)
- `scripts/crisis_review.py` exports `crisis_log.jsonl` into unlabelled candidates
  and refuses to run without `--consent`.

**Deployment**
- systemd unit hardened: `ProtectSystem=strict` with `ReadWritePaths`, `ProtectHome`,
  `PrivateDevices`, `Restart=always`, `MemoryMax=800M`/`MemoryHigh=600M`,
  `TasksMax=64`, and an `ExecStartPost` health gate. (#5)
- Caddyfile: 16 KB body cap, HSTS and baseline headers, access log with rotation,
  and `header_up X-Forwarded-For {remote_host}` so the app's limiter cannot be
  spoofed.

**Tests**
- 66 to 147 tests: crisis set, regex guard, logs, rate limiting, and deploy config
  assertions.
- Python floor raised to 3.10 and CI now tests it. `requires-python` claimed 3.9
  while the matrix only ran 3.11 and 3.12, so the floor was a claim nothing
  verified; a test now fails if the two disagree.

## 1.0.0 (2026-11-12)

The reviewed baseline: `docs/GAP-ANALYSIS-v1.1.md` was written against this commit
(`dcf5390`). It serves a cited RAG advisor end to end and has 66 passing tests, but
it carries the four P0 issues fixed in 1.1.0, including a crisis detector that
misses 40 of 61 labelled crisis phrasings.

**Do not deploy this tag.** It exists so the review has a fixed point to point at.
