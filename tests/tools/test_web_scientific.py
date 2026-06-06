"""Scientific search backends: OpenAlex / arXiv / Crossref + auto-routing.

All HTTP is mocked — these tests assert the shape and parsing contract
against captured-style fixtures, not live API behaviour. Live integration
belongs in a separate marked test; here we keep the suite hermetic.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest import mock

import pytest

from sciagent.tools.atomic import web_scientific as ws


# ---------------------------------------------------------------------------
# Auto-routing — two regexes, no keyword sniffing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        # DOIs route to Crossref
        ("10.1038/s41586-021-03819-2", "crossref"),
        ("  10.1126/science.abc1234  ", "crossref"),
        # arXiv IDs route to arXiv (new + old format, with/without version)
        ("2401.12345", "arxiv"),
        ("2401.12345v2", "arxiv"),
        ("arxiv:2401.12345", "arxiv"),
        ("ARXIV:2401.12345v3", "arxiv"),
        ("hep-th/0303001", "arxiv"),
        # Keyword queries stay on general web — the agent uses kind= to
        # opt into scientific backends explicitly when it wants them.
        ("metasurfaces 1550nm", "web"),
        ("papers on diffusion models", "web"),
        ("Hopkins 2024 arxiv", "web"),  # "arxiv" as a word, not an ID
        ("", "web"),
        # Almost-DOIs / almost-arxiv-IDs that shouldn't trigger
        ("10.1", "web"),
        ("2401", "web"),
        ("DOI: 10.1038/s41586-021-03819-2", "web"),  # prefix, not bare DOI
    ],
)
def test_route_query(query: str, expected: str) -> None:
    assert ws.route_query(query) == expected


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


def _openalex_fixture() -> Dict[str, Any]:
    """One Work record with the fields the projector reads — inverted
    abstract, authorships, primary_location, OA URL."""
    return {
        "results": [
            {
                "id": "https://openalex.org/W1234",
                "doi": "https://doi.org/10.1038/example",
                "title": "Example Paper Title",
                "publication_year": 2023,
                "abstract_inverted_index": {
                    "We": [0],
                    "study": [1],
                    "metasurfaces": [2],
                },
                "authorships": [
                    {"author": {"display_name": "Ada Lovelace"}},
                    {"author": {"display_name": "Grace Hopper"}},
                ],
                "primary_location": {
                    "landing_page_url": "https://example.com/paper",
                    "source": {"display_name": "Nature"},
                },
                "open_access": {"oa_url": "https://example.com/paper.pdf"},
            }
        ]
    }


def test_openalex_search_projects_to_result_shape() -> None:
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get") as g:
        g.return_value = mock.Mock(
            status_code=200,
            json=lambda: _openalex_fixture(),
        )
        g.return_value.raise_for_status = lambda: None

        results = ws.search_openalex("metasurfaces", num_results=5)

    assert len(results) == 1
    r = results[0]
    assert r["index"] == 1
    assert r["title"] == "Example Paper Title"
    # OA URL preferred over landing page so the agent fetches the PDF directly
    assert r["url"] == "https://example.com/paper.pdf"
    assert r["snippet"] == "We study metasurfaces"
    assert r["authors"] == ["Ada Lovelace", "Grace Hopper"]
    assert r["year"] == 2023
    assert r["venue"] == "Nature"
    assert r["source_id"] == "https://doi.org/10.1038/example"
    assert r["backend"] == "openalex"


def test_openalex_polite_pool_email_passed_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIAGENT_POLITE_EMAIL", "researcher@example.com")
    captured: Dict[str, Any] = {}
    def fake_get(url: str, params: Dict[str, Any] = None, **kw: Any):  # type: ignore[no-untyped-def]
        captured["params"] = params
        m = mock.Mock(status_code=200, json=lambda: {"results": []})
        m.raise_for_status = lambda: None
        return m
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get", side_effect=fake_get):
        ws.search_openalex("anything")
    assert captured["params"].get("mailto") == "researcher@example.com"


def test_reconstruct_inverted_abstract_preserves_word_order() -> None:
    # OpenAlex stores ``{word: [positions]}``; reassembly is by sorted index.
    inv = {"first": [0], "second": [1, 4], "third": [2], "and": [3]}
    out = ws._reconstruct_inverted_abstract(inv)
    assert out == "first second third and second"


def test_reconstruct_inverted_abstract_empty_or_none() -> None:
    assert ws._reconstruct_inverted_abstract(None) == ""
    assert ws._reconstruct_inverted_abstract({}) == ""


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <title>Example arXiv Paper</title>
    <summary>This paper studies something interesting.</summary>
    <published>2024-01-15T12:00:00Z</published>
    <author><name>First Author</name></author>
    <author><name>Second Author</name></author>
    <link href="http://arxiv.org/pdf/2401.12345v2" rel="related" title="pdf"/>
    <link href="http://arxiv.org/abs/2401.12345v2" rel="alternate"/>
  </entry>
</feed>
"""


def test_arxiv_atom_parsing() -> None:
    results = ws._parse_arxiv_atom(_ARXIV_ATOM)
    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Example arXiv Paper"
    assert r["url"] == "http://arxiv.org/pdf/2401.12345v2"
    assert r["snippet"] == "This paper studies something interesting."
    assert r["source_id"] == "2401.12345v2"
    assert r["authors"] == ["First Author", "Second Author"]
    assert r["year"] == 2024
    assert r["venue"] == "arXiv"
    assert r["backend"] == "arxiv"
    assert r["age"] == "2024-01-15"


def test_arxiv_search_id_query_uses_id_list_endpoint() -> None:
    """A bare arXiv ID should hit ``id_list=`` rather than the keyword
    search endpoint — IDs are unique, keyword search would be fuzzy."""
    captured: Dict[str, Any] = {}
    def fake_get(url: str, params: Dict[str, Any] = None, **kw: Any):  # type: ignore[no-untyped-def]
        captured["params"] = params
        m = mock.Mock(status_code=200, text=_ARXIV_ATOM)
        m.raise_for_status = lambda: None
        return m
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get", side_effect=fake_get):
        ws.search_arxiv("2401.12345")
    assert "id_list" in captured["params"]
    assert "search_query" not in captured["params"]


def test_arxiv_search_keyword_query_uses_search_endpoint() -> None:
    captured: Dict[str, Any] = {}
    def fake_get(url: str, params: Dict[str, Any] = None, **kw: Any):  # type: ignore[no-untyped-def]
        captured["params"] = params
        m = mock.Mock(status_code=200, text=_ARXIV_ATOM)
        m.raise_for_status = lambda: None
        return m
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get", side_effect=fake_get):
        ws.search_arxiv("metasurfaces", num_results=3)
    assert captured["params"]["search_query"] == "all:metasurfaces"
    assert captured["params"]["max_results"] == 3


def test_arxiv_parse_garbage_returns_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert ws._parse_arxiv_atom("not xml at all <") == []


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


def _crossref_doi_fixture() -> Dict[str, Any]:
    return {
        "status": "ok",
        "message": {
            "DOI": "10.1038/s41586-021-03819-2",
            "URL": "http://dx.doi.org/10.1038/s41586-021-03819-2",
            "title": ["Highly accurate protein structure prediction with AlphaFold"],
            "abstract": "<jats:p>Proteins are essential.</jats:p>",
            "author": [
                {"given": "John", "family": "Jumper"},
                {"given": "Richard", "family": "Evans"},
            ],
            "issued": {"date-parts": [[2021, 8, 26]]},
            "container-title": ["Nature"],
        },
    }


def test_crossref_doi_lookup_uses_doi_endpoint() -> None:
    captured: Dict[str, Any] = {}
    def fake_get(url: str, params: Dict[str, Any] = None, headers: Dict[str, Any] = None, **kw: Any):  # type: ignore[no-untyped-def]
        captured["url"] = url
        m = mock.Mock(status_code=200, json=lambda: _crossref_doi_fixture())
        m.raise_for_status = lambda: None
        return m
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get", side_effect=fake_get):
        results = ws.search_crossref("10.1038/s41586-021-03819-2")

    assert "/works/10.1038/s41586-021-03819-2" in captured["url"]
    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Highly accurate protein structure prediction with AlphaFold"
    assert r["source_id"] == "10.1038/s41586-021-03819-2"
    assert r["authors"] == ["John Jumper", "Richard Evans"]
    assert r["year"] == 2021
    assert r["venue"] == "Nature"
    # JATS markup stripped from abstract
    assert "<jats" not in r["snippet"]
    assert r["snippet"] == "Proteins are essential."


def test_crossref_keyword_search_uses_works_endpoint() -> None:
    captured: Dict[str, Any] = {}
    def fake_get(url: str, params: Dict[str, Any] = None, headers: Dict[str, Any] = None, **kw: Any):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["params"] = params
        m = mock.Mock(
            status_code=200,
            json=lambda: {"message": {"items": [_crossref_doi_fixture()["message"]]}},
        )
        m.raise_for_status = lambda: None
        return m
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get", side_effect=fake_get):
        results = ws.search_crossref("protein structure prediction")

    assert captured["url"].endswith("/works")
    assert captured["params"]["query"] == "protein structure prediction"
    assert len(results) == 1


def test_crossref_polite_pool_uses_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crossref's polite pool is identified via ``User-Agent``, not a query param."""
    monkeypatch.setenv("SCIAGENT_POLITE_EMAIL", "researcher@example.com")
    captured: Dict[str, Any] = {}
    def fake_get(url: str, params: Dict[str, Any] = None, headers: Dict[str, Any] = None, **kw: Any):  # type: ignore[no-untyped-def]
        captured["headers"] = headers
        m = mock.Mock(status_code=200, json=lambda: {"message": {"items": []}})
        m.raise_for_status = lambda: None
        return m
    with mock.patch("sciagent.tools.atomic.web_scientific.requests.get", side_effect=fake_get):
        ws.search_crossref("anything")
    assert "researcher@example.com" in captured["headers"]["User-Agent"]


# ---------------------------------------------------------------------------
# End-to-end: web.WebTool dispatches on kind=
# ---------------------------------------------------------------------------


def test_web_tool_kind_openalex_calls_openalex_backend() -> None:
    """``kind="openalex"`` should bypass auto-routing and hit OpenAlex even
    for a keyword query — explicit override is how the agent says
    "literature search, please." """
    from sciagent.tools.atomic.web import WebTool
    tool = WebTool()

    with mock.patch(
        "sciagent.tools.atomic.web.search_openalex",
        return_value=[{
            "index": 1, "title": "T", "url": "http://x", "snippet": "s",
            "age": "", "source_id": "10.x/y", "authors": ["A"],
            "year": 2023, "venue": "V", "backend": "openalex",
        }],
    ) as mocked:
        result = tool.execute(command="search", query="anything", kind="openalex")

    mocked.assert_called_once()
    assert result.success
    # The structured Cite line appears in the formatted output
    assert "Cite:" in result.output
    assert "10.x/y" in result.output
    assert "OpenAlex" in result.output


def test_web_tool_kind_auto_routes_doi_to_crossref() -> None:
    from sciagent.tools.atomic.web import WebTool
    tool = WebTool()
    with mock.patch(
        "sciagent.tools.atomic.web.search_crossref",
        return_value=[],
    ) as mocked_cr, mock.patch.object(WebTool, "_search_brave", return_value=[]) as mocked_brave:
        tool.execute(command="search", query="10.1038/s41586-021-03819-2", kind="auto")
    mocked_cr.assert_called_once()
    mocked_brave.assert_not_called()


def test_web_tool_kind_auto_keyword_stays_on_web() -> None:
    from sciagent.tools.atomic.web import WebTool
    tool = WebTool()
    with mock.patch(
        "sciagent.tools.atomic.web.search_openalex",
    ) as mocked_oa, mock.patch.object(
        WebTool, "_search_brave", return_value=[],
    ) as mocked_brave, mock.patch.object(
        WebTool, "_search_duckduckgo", return_value=[],
    ):
        tool.execute(command="search", query="metasurfaces 1550nm", kind="auto")
    # Keyword query does NOT auto-divert to OpenAlex — stays on general web
    mocked_oa.assert_not_called()
    mocked_brave.assert_called_once()
