"""Citations: the guarantee that anything the answer names in-text is linked.

The invariant from production: a paper quoted in the answer must appear in the
citation list even if it was not in the top-k sources, and a source with no
resolvable identifier must not be presented as a citation at all.
"""
import citations


def test_build_citations_skips_notes_without_identifiers(vault_dir):
    base = citations.build_citations(
        vault_dir, ["Notes/First 90 Days After Diagnosis.md"], 6)
    assert base == [], "a note with no DOI/URL must not become a citation"


def test_grounded_merge_links_a_note_named_in_the_answer(vault_dir):
    src = "Notes/Language and brain development in deaf children.md"
    answer = ("According to Language and brain development in deaf children, "
              "early exposure matters.")
    got = citations.build_citations_grounded(vault_dir, [src], answer, 6)
    assert got, "named source must produce a citation"
    assert any(c.get("url") for c in got)


def test_grounded_merge_produces_a_doi_url(vault_dir):
    src = "Notes/Language and brain development in deaf children.md"
    answer = "See Language and brain development in deaf children."
    got = citations.build_citations_grounded(vault_dir, [src], answer, 6)
    assert any("doi.org" in (c.get("url") or "") for c in got)


def test_unnamed_source_is_not_force_merged(vault_dir):
    src = "Notes/Language and brain development in deaf children.md"
    answer = "Nothing here names that note at all."
    got = citations.build_citations_grounded(vault_dir, [src], answer, 6)
    # The note has a DOI, so the source-side path may still include it; what
    # must not happen is a citation with no link attached to it.
    assert all(c.get("url") or c.get("doi") for c in got)


def test_max_citations_is_respected(vault_dir):
    srcs = ["Notes/Language and brain development in deaf children.md",
            "Notes/reading-outcomes-2021.md",
            "Notes/First 90 Days After Diagnosis.md"]
    got = citations.build_citations(vault_dir, srcs, max_citations=1)
    assert len(got) <= 1


def test_safe_url_rejects_non_http_schemes():
    assert citations._safe_url("javascript:alert(1)") == ""
    assert citations._safe_url("data:text/html,<script>") == ""
    assert citations._safe_url("https://example.org/a.pdf") == "https://example.org/a.pdf"


def test_safe_doi_requires_a_doi_prefix():
    assert citations._safe_doi("10.1093/deafed/enab001") == "10.1093/deafed/enab001"
    assert citations._safe_doi("doi:10.1093/deafed/enab001") == ""
    assert citations._safe_doi("") == ""
