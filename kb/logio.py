#!/usr/bin/env python3
"""logio.py -- JSONL logging with rotation, retention and redaction.

The server logs user questions. On a 1 GB box an unbounded append is a
disk-full outage waiting for traffic, and for an audience of parents of Deaf and
hard of hearing children the text itself is sensitive. Three controls, all
configurable per deployment:

  rotation   size-bounded rotation with N backups (path, path.1, path.2 ...)
  retention  backups older than N days are deleted; a prune runs at most every
             5 minutes per log, so it costs nothing on the hot path
  redaction  what happens to user text before it is written:
               full    store it (opt in; needs a notice and consent)
               redact  strip direct identifiers: emails, phone numbers, card and
                       long digit runs, URLs with query strings, @handles
               hash    store sha256 prefix and length only, no text at all
               none    do not store the field at all

`redact` is the default because it keeps the shape of a question usable for
improving the crisis trigger while removing the obvious identifiers. It is not
anonymisation: "my son Adam just failed his hearing test" survives, so treat the
retention window, not the redaction, as the real limit on exposure.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

TEXT_FIELDS = ("question", "answer", "note")
MODES = ("full", "redact", "hash", "none")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL_QUERY = re.compile(r"\bhttps?://\S*[?#]\S*", re.IGNORECASE)
_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")


def redact_text(text, mode="redact"):
    """Return the text to store for one field, per `mode`."""
    text = "" if text is None else str(text)
    if mode == "full":
        return text
    if mode == "none":
        return ""
    if mode == "hash":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return "sha256:%s:len=%d" % (digest, len(text))
    if mode != "redact":
        raise ValueError("unknown log mode %r, expected one of %s" % (mode, MODES))

    out = _EMAIL.sub("[email]", text)
    out = _URL_QUERY.sub("[url]", out)
    out = _CARD.sub("[number]", out)
    out = _PHONE.sub("[phone]", out)
    out = _LONG_DIGITS.sub("[number]", out)
    out = _URL.sub("[url]", out)
    out = _HANDLE.sub("[handle]", out)
    return out


class JsonlLog:
    """One JSONL file with size rotation, age-based retention and redaction."""

    def __init__(self, path, max_bytes=5 * 1024 * 1024, backups=3,
                 retention_days=30.0, mode="redact", encode="utf-8"):
        if mode not in MODES:
            raise ValueError("unknown log mode %r, expected one of %s" % (mode, MODES))
        self.path = path
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self.retention_days = float(retention_days)
        self.mode = mode
        self.encode = encode
        self._lock = threading.Lock()
        self._last_prune = 0.0

    # -- writes ------------------------------------------------------------
    def append(self, record):
        """Redact, serialise and append one record, rotating first if needed."""
        payload = self._prepare(record)
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode(self.encode)
        with self._lock:
            try:
                self._rotate_if_needed(len(line))
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "ab") as fh:
                    fh.write(line)
                self._maybe_prune()
            except OSError:
                # Logging must never take a request down.
                return False
        return True

    def _prepare(self, record):
        out = dict(record)
        for field in TEXT_FIELDS:
            if field in out:
                out[field] = redact_text(out[field], self.mode)
        return out

    def _rotate_if_needed(self, incoming):
        if self.max_bytes <= 0:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        if self.backups <= 0:
            os.replace(self.path, self.path + ".1")
            return
        # shift path.N-1 -> path.N, dropping the oldest
        oldest = "%s.%d" % (self.path, self.backups)
        if os.path.exists(oldest):
            try:
                os.remove(oldest)
            except OSError:
                pass
        for n in range(self.backups - 1, 0, -1):
            src = "%s.%d" % (self.path, n)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (self.path, n + 1))
        os.replace(self.path, self.path + ".1")

    def _maybe_prune(self):
        now = time.time()
        if now - self._last_prune < 300:
            return
        self._last_prune = now
        self.prune()

    def prune(self):
        """Delete backups older than the retention window. Returns the count."""
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for n in range(1, self.backups + 1):
            candidate = "%s.%d" % (self.path, n)
            try:
                if os.path.getmtime(candidate) < cutoff:
                    os.remove(candidate)
                    removed += 1
            except OSError:
                continue
        return removed

    def files(self):
        """Every file this log owns, oldest last."""
        out = [self.path]
        for n in range(1, self.backups + 1):
            out.append("%s.%d" % (self.path, n))
        return out


def from_env(prefix, path, default_mode="redact"):
    """Build a JsonlLog from ADVISOR_LOG_* style environment variables."""
    return JsonlLog(
        path,
        max_bytes=int(os.environ.get(prefix + "_MAX_BYTES", str(5 * 1024 * 1024))),
        backups=int(os.environ.get(prefix + "_BACKUPS", "3")),
        retention_days=float(os.environ.get(prefix + "_RETENTION_DAYS", "30")),
        mode=os.environ.get(prefix + "_QUESTION", default_mode),
    )
