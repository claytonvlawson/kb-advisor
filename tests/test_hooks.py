"""Client hooks: the optional per-client behaviour plugs.

The example client routes US states so a parent in Ohio is not handed a
Texas phone number. Hooks are optional by design (the server calls only what
exists), but when a client ships them they must behave.
"""
import client_hooks


def test_detect_location_reads_a_state():
    loc = client_hooks.detect_location("we just moved to Ohio")
    assert loc and loc["state"] == "Ohio" and loc["abbr"] == "OH"


def test_detect_location_flags_an_ambiguous_city():
    loc = client_hooks.detect_location("anyone near Columbus?")
    assert loc and loc.get("ambiguous") is True
    assert "Ohio" in loc["candidates"]


def test_detect_location_returns_none_without_a_place():
    assert client_hooks.detect_location("how do I start an IEP") is None


def test_carried_location_wins_over_a_previous_turn():
    carried = {"state": "Ohio", "abbr": "OH", "city": None}
    assert client_hooks.resolve_location("and what about speech therapy", carried) == carried


def test_same_state_folder_matches_only_that_state():
    ohio = "LanguageDeprivationAgent/Local Resources - States/Ohio/x.md"
    assert client_hooks.same_state_folder(ohio, "Ohio") is True
    assert client_hooks.same_state_folder(ohio, "Texas") is False


def test_location_note_names_the_state():
    note = client_hooks.location_note({"state": "Ohio", "abbr": "OH"}, False, False)
    assert isinstance(note, str)
    assert "Ohio" in note
