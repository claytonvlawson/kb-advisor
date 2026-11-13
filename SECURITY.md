# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(Security tab → "Report a vulnerability"). It is enabled, and it keeps the
report private until there is a fix. If you cannot use it, open a normal issue
that says only "security issue, please contact me" and no details.

Do not open a public issue with a working exploit, a leaked token, or a
screenshot of someone's question. The logs and the corpus are client documents.

Expect an acknowledgement within a few days. This is a small project without a
security team, so please include reproduction steps and the version tag.

## Supported versions

Only the latest tag on `main` is supported. `v1.0.0` is tagged for history and
is explicitly unsafe to deploy (it carries four P0 issues, including a crisis
detector with 0.344 measured recall). Anything older than the current tag
should be upgraded rather than patched.

## What counts as a vulnerability here

This is a retrieval system that puts client documents in front of a language
model, so the interesting classes are:

- **Auth bypass.** `ADVISOR_TOKEN` is required on every POST route and on
  `/health/deep`; an unset or placeholder token must fail closed (`401`).
  Bypassing it, or reaching a generation without it, is a vulnerability.
- **Crisis path failure.** A phrasing that should inject the safety note and
  does not, or a timeout that turns the detector off without the CRITICAL log.
- **Denial of service.** A crisis regex that backtracks catastrophically, a
  request that bypasses the body cap, the rate limiter, or the generation cap.
- **Cross-client exposure.** With one process per client, this means anything
  that lets an instance read another client's vault, index, or logs.
- **Data exposure in logs.** Question or answer text reaching disk unredacted
  when `ADVISOR_LOG_QUESTION=redact`, or a log line containing a token.
- **Prompt injection with impact.** Retrieved text from an ingested PDF that
  makes the model emit a crisis number, a URL, or an instruction that the
  persona forbids. Report it with the PDF or the text that does it. The
  defences (ingest scan, serve-time drop, `<source>` wrapping plus a fixed
  rule) are described under "Prompt injection" in the README; a bypass of any
  of them is in scope.

## What is out of scope

- Model output quality, hallucinations within the grounding contract, and the
  measured accuracy of the crisis trigger. Those are tracked as issues, with the
  labelled set in `eval/crisis_set.jsonl`, not handled as security reports.
- The known limits in the README: BM25-only recall, no streaming, one process
  per client, no in-process multi-tenancy.
- Vulnerabilities in OpenRouter, Caddy, systemd, or PyMuPDF itself. Report those
  upstream; tell us if the shipped configuration makes one exploitable.

## If a token leaks

Rotate it on that instance only: change `ADVISOR_TOKEN` in
`/etc/kb-advisor/<client>.env`, then `systemctl restart kb-advisor@<client>`.
Nothing else restarts. Do not commit the new value; the env file is chmod 600
and gitignored.
