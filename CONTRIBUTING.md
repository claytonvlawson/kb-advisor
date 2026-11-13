# Contributing

This repo is a template: people copy it per client. That makes two things more
important than usual. A change that breaks an existing `clients/<name>/` config
is a breaking change, and a change nobody can find in the diff is worse than no
change at all.

## Add a client

```sh
./scripts/new_client.sh acme "You are an advisor for Acme customers."
python3 ingest/ingest_pdfs.py ~/Documents/acme-pdfs -o clients/acme/vault/Notes
./scripts/build_index.sh acme
python3 scripts/eval_crisis.py --client clients/acme      # after writing crisis.json
```

A client is a directory, never a fork: `system_prompt.md` (required),
`crisis.json` (optional but strongly recommended), `client_hooks.py`, `vault/`,
and the built `index/`. The server core does not change per client.

If you serve more than one client, use one process per client
(`deploy/kb-advisor@.service`), not multi-tenancy in one process. The README
explains why, and `deploy/kb-advisor.slice` is what keeps the box honest.

## The invariants

The design rules in the README are not stylistic preferences. Each one is a
production incident turned into an invariant, and a change that violates one
needs to say which trade it is making:

1. Crisis handling is injection, not ranking.
2. The grounding contract is absolute.
3. Logs are bounded and redacted on write.
4. Formatting rules live in code, not prompts (including: no em dashes in
   anything a user reads; `_strip_em_dashes` is the backstop, not the plan).
5. A paper quoted in-text always appears in citations.
6. Corpus shape is a retrieval parameter.
7. Never assume the user's location.
8. BM25 only, no dense half.
9. Client regexes are validated and every match is bounded.
10. The knowledge base is data. Context is wrapped in `<source>` tags with a
    fixed rule, ingested text is scanned and flagged, and retrieved chunks that
    address the model are dropped and logged.

## What CI enforces

| Gate | Command | What it means for you |
|---|---|---|
| unit | `python -m pytest --cov` | 162 tests, 75% branch-coverage floor over `kb/` |
| quality | `ruff check kb ingest scripts tests`, `mypy` | bug-focused lint and types; formatting rules are deliberately off |
| crisis-eval | `python scripts/eval_crisis.py` | recall >= 0.95 and precision >= 0.90 on the labelled set |

Run all three before opening a PR. They are fast (a few seconds each) and they
are the same commands CI runs.

## Changing the crisis trigger

This is the one area where "it looks fine" is not enough.

- Add patterns to `clients/<name>/crisis.json`, never inline in code.
- The pattern guard rejects quantified groups containing quantifiers
  (`(a+)+`) and quantified groups with overlapping alternatives (`(a|ab)+`).
  Rewrite the pattern rather than removing the guard.
- Every change needs a labelled example. Add the phrasing to
  `eval/crisis_set.jsonl` with a `label` and a `category`, then re-run
  `scripts/eval_crisis.py`. A pattern without a test example is a guess.
- Growing the set from real traffic goes through `scripts/crisis_review.py`,
  which requires `--consent` and writes **unlabelled** candidates. Label them by
  hand. Never auto-label: that only re-teaches the detector what it already
  believes.

## Changing the API

`/chat`, `/feedback`, `/health` and `/health/deep` are the contract. A new
response field is a minor bump; removing or renaming one is a major concern for
any client that integrated, so say so in the PR and in `CHANGELOG.md`.

Fields that are always `null` (`location`, `location_ambiguous` today) are worse
than no field: a client will build on them. Populate or delete.

## Commits and versions

- `CHANGELOG.md` gets an entry per tag, and CI fails if the top entry disagrees
  with `pyproject.toml`.
- A config-incompatible change (a rejected pattern, a retired env var, a changed
  field) bumps the minor version.
- Tag releases on `main` after CI is green. Do not move a published tag; ship a
  patch release instead.
