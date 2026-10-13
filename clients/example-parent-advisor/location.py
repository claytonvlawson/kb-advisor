"""location.py -- find the US state (and city) a parent mentions, so the parent advisor
can put that state's resource notes first.

Deterministic on purpose, like the crisis trigger in parent_advisor_server.py: a model
asked "where does this parent live?" will guess from whatever example happens to be in
context (the South Carolina incident). This module only reports a place the parent
actually wrote, and reports an ambiguous city (Columbus: Georgia, Indiana or Ohio) as
ambiguous instead of picking one.

    detect(text)                     -> {"state", "abbr", "city"} | {"ambiguous": True, ...} | None
    resolve(text, carried)           -> the location to use this turn (new mention beats carried)
    folder_prefix(state)             -> vault folder holding that state's resource notes
    same_state_folder(source, state) -> False for another state's resource note

Cities come from us_places.json (US Census Vintage 2023, see tools/build_us_places.py).
"""
import json
import os
import re

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
BY_ABBR = {a: s for s, a in STATES.items()}

# State names that are also first names or surnames ("my daughter Georgia"): only count
# them after a place cue ("in Georgia"), after "City, " ("Atlanta, Georgia"), or before
# "state" / "State".
NAMEISH = {"Georgia", "Virginia", "Montana", "Indiana", "Washington", "Nevada", "Carolina"}
# Abbreviations that are also English words: only count them right after "City, ".
WORDISH_ABBR = {"IN", "OR", "ME", "OK", "HI", "OH", "ID", "DE", "MA", "LA", "PA", "CO", "MI",
                "AL", "AR", "MO"}

CUE = r"(?i:\b(?:in|near|from|to|around|outside(?:\s+of)?|at|by|across|throughout|live\s+in|moved\s+to|move\s+to)\s+(?:the\s+)?)"
FROM_CUE = re.compile(r"(?i)\bfrom\s+(?:the\s+)?$")

VAULT_AGENT = "LanguageDeprivationAgent/"
STATE_DIR = VAULT_AGENT + "Local Resources - States/"
DMV_DIR = VAULT_AGENT + "Local Resources DMV/"
_STATE_NOTE = re.compile(re.escape(STATE_DIR) + r"([^/]+)/")

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "us_places.json")) as _fh:
    PLACES = json.load(_fh)["places"]          # city -> [state names]

_DC = re.compile(r"\bWashington,?\s*D\.?\s?C\b\.?|\bD\.C\.|\bDC\b|(?i:\bdistrict\s+of\s+columbia\b)")
_WA_STATE = re.compile(r"(?i:\bWashington\s+state\b|\bstate\s+of\s+Washington\b)")
_NAMES = sorted((s for s in STATES if s != "District of Columbia"), key=len, reverse=True)
_STATE_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in _NAMES) + r")\b", re.IGNORECASE)
_ABBR_COMMA = re.compile(r"[A-Z][A-Za-z.'-]+,\s*([A-Z]{2})\b")
_ABBR_CUE = re.compile(CUE + r"([A-Z]{2})\b")
_CITY_NAMES = sorted(PLACES, key=len, reverse=True)
_CITY_ALT = "|".join(re.escape(c) for c in _CITY_NAMES)
_CITY_CUE = re.compile(CUE + r"(" + _CITY_ALT + r")\b")
_CITY_COMMA = re.compile(r"\b(" + _CITY_ALT + r"),\s*$")


def _canon_state(name):
    for s in STATES:
        if s.lower() == name.lower():
            return s
    return None


def _mask(text, spans):
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _state_mentions(text):
    """-> [(pos, state, from_cued, end)] in text order."""
    found, spans = [], []
    for m in _DC.finditer(text):
        found.append((m.start(), "District of Columbia", bool(FROM_CUE.search(text[:m.start()])), m.end()))
        spans.append(m.span())
    for m in _WA_STATE.finditer(text):
        found.append((m.start(), "Washington", bool(FROM_CUE.search(text[:m.start()])), m.end()))
        spans.append(m.span())
    rest = _mask(text, spans)
    for m in _STATE_RE.finditer(rest):
        state = _canon_state(m.group(1))
        before, after = rest[:m.start()], rest[m.end():]
        if state in NAMEISH or m.group(1) != state:
            # name-like, or not capitalized as a proper noun: needs a place cue or "City, "
            cued = re.search(CUE + r"$", before) or re.search(r"[A-Z][A-Za-z.'-]+,\s*$", before) \
                or re.match(r"\s+[Ss]tate\b", after)
            if not cued:
                continue
        found.append((m.start(), state, bool(FROM_CUE.search(before)), m.end()))
    for m in _ABBR_COMMA.finditer(rest):
        ab = m.group(1)
        if ab in BY_ABBR and ab != "DC":
            found.append((m.start(1), BY_ABBR[ab], False, m.end(1)))
    for m in _ABBR_CUE.finditer(rest):
        ab = m.group(1)
        if ab in BY_ABBR and ab not in WORDISH_ABBR and ab != "DC":
            found.append((m.start(1), BY_ABBR[ab], bool(FROM_CUE.search(rest[:m.start(1)])), m.end(1)))
    found.sort()
    return found


def _city_mentions(text, states):
    """-> [(pos, city, candidate_states, from_cued)]; a city right before 'City, State' is paired."""
    out = []
    for m in _CITY_CUE.finditer(text):
        out.append((m.start(1), m.group(1), PLACES[m.group(1)], bool(FROM_CUE.search(text[:m.start(1)]))))
    for pos, state, from_cued, _ in states:
        m = _CITY_COMMA.search(text[:pos])
        if m and state in PLACES.get(m.group(1), []):
            out.append((m.start(1), m.group(1), [state], from_cued))
    seen, uniq = set(), []
    for item in sorted(out):
        if item[0] not in seen:
            seen.add(item[0])
            uniq.append(item)
    return uniq


def _result(state, city=None):
    if city and state not in PLACES.get(city, []):
        city = None
    return {"state": state, "abbr": STATES[state], "city": city}


def detect(text):
    """The place the parent wrote, or None. Prefers the last mention, skipping a
    'from X' mention when another place is also named ('we moved from Texas to Ohio')."""
    if not text:
        return None
    states = _state_mentions(text)
    # "in Delaware" is the state, not Delaware, Ohio (or Wyoming, Michigan): a city that sits
    # on the same words as a state mention is dropped.
    cities = [c for c in _city_mentions(text, states)
              if not any(p <= c[0] < end for p, _, _, end in states)]
    mentions = [(p, "state", s, f) for p, s, f, _ in states] + [(p, "city", (c, cs), f) for p, c, cs, f in cities]
    if not mentions:
        return None
    if any(not m[3] for m in mentions):
        mentions = [m for m in mentions if not m[3]]
    mentions.sort(key=lambda m: m[0])
    named_states = {m[2] for m in mentions if m[1] == "state"}
    pos, kind, val, _ = mentions[-1]
    if kind == "state":
        city = None
        for p, c, cs, f in cities:
            if val in cs and (city is None or p > city[0]):
                city = (p, c)
        return _result(val, city[1] if city else None)
    city, cands = val
    if len(cands) == 1:
        return _result(cands[0], city)
    both = [s for s in cands if s in named_states]
    if len(both) == 1:
        return _result(both[0], city)
    return {"ambiguous": True, "city": city, "candidates": list(cands)}


def clean_carried(loc):
    """Validate a location the widget sends back ({"state", "city"?}); None if unusable."""
    if not isinstance(loc, dict):
        return None
    state = _canon_state(str(loc.get("state") or "")[:40])
    if not state:
        return None
    city = str(loc.get("city") or "")[:60] or None
    return _result(state, city)


def resolve(text, carried=None):
    """Location for this turn: a clear new mention wins; an ambiguous city is settled by the
    carried state when that state is one of its candidates; otherwise keep the carried one."""
    found = detect(text)
    carried = clean_carried(carried)
    if found and not found.get("ambiguous"):
        return found
    if found and found.get("ambiguous"):
        if carried and carried["state"] in found["candidates"]:
            return _result(carried["state"], found["city"])
        return found
    return carried


def folder_prefix(state):
    if state in ("District of Columbia", "Maryland"):
        return DMV_DIR
    return STATE_DIR + state + "/"


def same_state_folder(source, state):
    """False only for a resource note that belongs to a different state (or the DMV
    notes for a parent outside DC/Maryland). National notes always pass."""
    if source.startswith(DMV_DIR):
        return state in ("District of Columbia", "Maryland")
    m = _STATE_NOTE.match(source)
    if m:
        return m.group(1) == state
    return True


def label(loc):
    if not loc or loc.get("ambiguous"):
        return None
    name = "Washington, DC" if loc["state"] == "District of Columbia" else loc["state"]
    return "%s, %s" % (loc["city"], name) if loc.get("city") and loc["state"] != "District of Columbia" else name
