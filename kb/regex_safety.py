#!/usr/bin/env python3
"""regex_safety.py -- keep client-supplied crisis regexes from becoming a DoS.

`crisis.json` is client config, so a pattern arrives from someone who is not
thinking about regex engines. Two failure modes matter here:

  1. Catastrophic backtracking. `(\\w+\\s?)+[.!?]` takes 7 ms on 16 characters,
     69 ms on 20 and 951 ms on 24. The question cap is 500 characters, so one
     bad pattern pins a worker for minutes per request.
  2. No bound at all. `re.search` has no timeout, and the server used to run it
     inline on the request thread.

So there are two guards, and they are complementary:

  * compile_patterns() refuses patterns it can prove risky, at startup, naming
    the pattern. A broken client config fails loudly on boot instead of quietly
    on traffic.
  * bounded_search() runs the match in a worker with a wall clock limit, so a
    pattern the static check did not catch still cannot block the request.

On timeout the detector fails CLOSED (treated as a crisis hit) because the
design rule in the README says a false positive degrades gracefully while a
false negative fails silently. It then logs CRITICAL once and disables the
detector for the rest of the process, so a pathological pattern costs one
bounded delay instead of leaking a thread per request.
"""
import re
import sys
import threading

# Wall clock bound for one crisis match. Generous: the example client's 14
# patterns match in ~0.03 ms on a 500-character question.
MAX_MATCH_S = float(__import__("os").environ.get("ADVISOR_REGEX_TIMEOUT_S", "0.25"))

_ALTERNATION = re.compile(r"\|")
_EMPTY_BODY = re.compile(r"\(\s*\)")


class RiskyPatternError(ValueError):
    """A client pattern that is likely to backtrack catastrophically."""

    def __init__(self, pattern, reason):
        self.pattern = pattern
        self.reason = reason
        super().__init__("risky crisis regex %r: %s" % (pattern, reason))


def _matching_paren(pattern, start):
    """Index of the ')' closing the group opened at `start`, or -1."""
    depth = 0
    i = start
    in_class = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _quantifier_at(pattern, index):
    """If an unbounded quantifier starts at `index`, return its end index."""
    if index >= len(pattern):
        return None
    ch = pattern[index]
    if ch in "*+":
        return index + 1
    if ch == "{":
        end = pattern.find("}", index)
        if end == -1:
            return None
        body = pattern[index + 1:end]
        if body.endswith(","):
            return end + 1
    return None


def _group_body(pattern, start, end):
    body = pattern[start + 1:end]
    if body.startswith("?:"):
        body = body[2:]
    elif body.startswith("?"):
        # lookarounds and named groups: keep the intent visible but treat as body
        body = body.lstrip("?")
        if body.startswith("<") and ">" in body:
            body = body.split(">", 1)[1]
    return body


def _bound_alternatives(body):
    """Split a group body on top level '|' and return the branches."""
    branches, depth, current, in_class, i = [], 0, [], False, 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            current.append(body[i:i + 2])
            i += 2
            continue
        if in_class:
            current.append(ch)
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "|" and depth == 0:
            branches.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    branches.append("".join(current))
    return branches


def _can_repeat(branch):
    """True when a branch can consume an unbounded number of characters."""
    return any(q in branch for q in ("*", "+", "{"))


def find_hazard(pattern):
    """Return a human reason if `pattern` risks catastrophic backtracking.

    Two shapes account for nearly every real ReDoS:

    * a quantified group whose body is itself quantified, `(a+)+`, `(\\w+\\s?)+`
    * a quantified group with overlapping alternatives, `(a|ab)+`, where one
      branch can be a prefix of another

    This is a heuristic, not a proof: bounded_search() is the backstop.
    """
    if _EMPTY_BODY.search(pattern):
        return "empty group ()"

    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            i += 2
            continue
        if pattern[i] != "(":
            i += 1
            continue
        end = _matching_paren(pattern, i)
        if end == -1:
            return "unbalanced parenthesis"
        body = _group_body(pattern, i, end)

        # quantify-check: is the group itself repeated?
        j = end + 1
        if j < len(pattern) and pattern[j] == "?":
            j += 1                      # lazy modifier
        repeated = _quantifier_at(pattern, j) is not None

        if repeated:
            if _ALTERNATION.search(body):
                branches = _bound_alternatives(body)
                for a in branches:
                    for b in branches:
                        if a is not b and b.startswith(a) and len(a) < len(b):
                            if _can_repeat(a) or _can_repeat(b) or len(a) < len(b):
                                return ("quantified group with overlapping "
                                        "alternatives %r and %r" % (a, b))
            else:
                for k, ch in enumerate(body):
                    if ch == "\\":
                        continue
                    if ch in "*+":
                        return ("nested quantifier: group containing %r is "
                                "itself repeated" % ch)
                    if ch == "{":
                        close = body.find("}", k)
                        if close != -1 and body[k + 1:close].endswith(","):
                            return ("nested quantifier: group containing "
                                    "{%s} is itself repeated" % body[k + 1:close])

        i = end + 1
    return None


def compile_patterns(patterns, client="<client>"):
    """Compile crisis patterns into one alternation, refusing risky ones.

    Raises RiskyPatternError naming the pattern, so a bad client config fails
    at startup with something actionable instead of on live traffic.
    """
    patterns = [p for p in (patterns or []) if str(p).strip()]
    for p in patterns:
        reason = find_hazard(p)
        if reason:
            raise RiskyPatternError(p, reason)
    if not patterns:
        return None
    try:
        return re.compile("|".join(patterns), re.IGNORECASE)
    except re.error as exc:
        raise RiskyPatternError("|".join(patterns), "does not compile: %s" % exc)


_disabled = False
_timeout_logged = False
_lock = threading.Lock()


def detector_disabled():
    return _disabled


def reset_state():
    """Test hook: clear the one-time timeout state."""
    global _disabled, _timeout_logged
    with _lock:
        _disabled = False
        _timeout_logged = False


def bounded_search(regex, text, timeout_s=MAX_MATCH_S, label="crisis"):
    """re.search with a wall clock bound.

    Returns True / False, or None when the match had to be abandoned (the
    caller decides what None means). On the first timeout the detector is
    disabled for the process: a thread cannot be killed, so repeating the call
    would leak a worker and a core per request.
    """
    global _disabled, _timeout_logged
    if regex is None or _disabled:
        return False

    result = {}

    def run():
        try:
            result["hit"] = bool(regex.search(text))
        except Exception as exc:                       # noqa: BLE001
            result["error"] = exc

    worker = threading.Thread(target=run, name="%s-regex" % label, daemon=True)
    worker.start()
    worker.join(timeout_s)

    if worker.is_alive():
        with _lock:
            first = not _timeout_logged
            _timeout_logged = True
            _disabled = True
        if first:
            print("[kb-advisor] CRITICAL: %s regex exceeded %.2fs and has been "
                  "DISABLED for this process. Fix crisis.json (nested quantifier "
                  "or overlapping alternation) and restart."
                  % (label, timeout_s), file=sys.stderr, flush=True)
        return None

    if "error" in result:
        print("[kb-advisor] crisis regex raised %r; treating as no match"
              % (result["error"],), file=sys.stderr, flush=True)
        return False
    return bool(result.get("hit"))
