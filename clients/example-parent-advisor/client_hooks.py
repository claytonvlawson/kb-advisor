"""client_hooks.py -- optional per-client behavior plugs for kb_server.py.

The parent-advisor example: deterministic US-state location routing, ported
from the production location.py. The server only calls hooks that exist, so
a client without this file gets plain BM25 retrieval with no location
logic. Every hook takes only its own arguments plus the module-level
location module, so swapping in another client's hooks is a file copy.

Implemented hooks (all optional):
  detect_location(question)      -> loc dict | ambiguous dict | None
  resolve_location(q, carried)   -> loc to use this turn
  location_note(loc, local, ask_where) -> user-message prefix, or ""
  folder_prefix(state)           -> vault folder holding that state's notes
  same_state_folder(src, state) -> False only for another state's resource note
  is_local_resource(source)     -> True for any state resource note
"""

import location  # the client's own location.py, next to this file


def detect_location(question):
    return location.detect(question)


def resolve_location(question, carried):
    return location.resolve(question, carried)


def detect_state(state):
    return None  # not needed by kb_server; kept for symmetry


def folder_prefix(state):
    return location.folder_prefix(state)


def same_state_folder(source, state):
    return location.same_state_folder(source, state)


def is_local_resource(source):
    return location.STATE_DIR in source or location.DMV_DIR in source


def location_note(loc, local, ask_where=False):
    """Plain-language instruction for this turn's user message, or ''. Ported
    verbatim from the production server."""
    if ask_where:
        return ("Where the parent is: they have not said. If state or local resources would help "
                "with this question, ask which state or city they live in, and meanwhile describe "
                "the kinds of resources to look for without naming any specific state's programs.\n\n")
    if loc and loc.get("ambiguous"):
        return ("Where the parent is: they mentioned %s, which is a city in more than one state "
                "(%s). Ask which state they mean before naming any state's specific office, phone "
                "number, or program, and meanwhile give the general answer.\n\n"
                % (loc["city"], ", ".join(loc["candidates"])))
    if not loc:
        return ""
    where = location.label(loc)
    state = "Washington, DC" if loc["state"] == "District of Columbia" else loc["state"]
    if not local:
        return "Where the parent is: %s (they told us).\n\n" % where
    note = ("Where the parent is: %s (they told us). The context includes %s's resource notes, so "
            "you may name those specific programs, offices, and contacts for them. "
            % (where, "Maryland and DC" if loc["state"] in ("Maryland", "District of Columbia") else state))
    if loc.get("city"):
        note += ("These notes are statewide, not specific to %s: say so, and tell them how to find the "
                 "nearest local or county office through the statewide program. " % loc["city"])
    note += "Do not name resources from any other state.\n\n"
    return note
