"""Scientific search backends: OpenAlex, arXiv, Crossref.

Three free, keyless REST APIs that return structured metadata no general
web search (Brave / DDG / native provider grounding) can produce: DOI
resolution, citation graph, author IDs, exact venue, OA links. The agent
reaches for these when general-web ranking is the wrong tool — looking up
a specific paper, navigating "who cited this", or resolving a DOI to clean
metadata for a citation.

Result-dict shape matches what ``web.WebTool._search`` already builds for
Brave/DDG (``index/title/url/snippet/age``) plus structured fields
(``source_id/authors/year/venue/backend``) the formatter prints when
present and ignores when ``None``. Keeps the downstream display + source
classification code untouched.

Polite pool: OpenAlex and Crossref upgrade rate limits for requests that
identify a contact email. Set ``SCIAGENT_POLITE_EMAIL`` to opt in;
unauthenticated calls still work, just at the shared low-priority tier.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests


# ---------------------------------------------------------------------------
# Auto-routing
# ---------------------------------------------------------------------------

# Two unambiguous patterns. Anything else stays on general web — we don't
# keyword-sniff "papers on..." into a scientific backend because the agent
# can call ``kind="openalex"`` explicitly when it wants a literature
# search.
_DOI_RE = re.compile(r"^\s*10\.\d{4,}/\S+\s*$")
_ARXIV_RE = re.compile(
    r"^\s*(?:arxiv:)?(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})(v\d+)?\s*$",
    re.IGNORECASE,
)


def route_query(query: str) -> str:
    """Return ``"crossref"`` / ``"arxiv"`` / ``"web"`` based on query shape."""
    if _DOI_RE.match(query):
        return "crossref"
    if _ARXIV_RE.match(query):
        return "arxiv"
    return "web"


def _polite_email() -> Optional[str]:
    email = os.getenv("SCIAGENT_POLITE_EMAIL") or os.getenv("OPENALEX_EMAIL")
    return email or None


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

_OPENALEX_BASE = "https://api.openalex.org"


def search_openalex(query: str, num_results: int = 5, timeout: int = 15) -> List[Dict[str, Any]]:
    """Search OpenAlex Works index.

    Returns up to ``num_results`` hits with reconstructed abstracts (OpenAlex
    stores abstracts as a ``{word: [positions]}`` inverted index to avoid
    redistribution issues; we reassemble in-process).
    """
    params: Dict[str, Any] = {
        "search": query,
        "per-page": min(num_results, 25),
    }
    email = _polite_email()
    if email:
        params["mailto"] = email

    try:
        resp = requests.get(f"{_OPENALEX_BASE}/works", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ OpenAlex error: {e}")
        return []

    results: List[Dict[str, Any]] = []
    for i, work in enumerate(data.get("results", []), 1):
        results.append(_openalex_work_to_result(work, i))
    return results


def _openalex_work_to_result(work: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Project an OpenAlex Work record onto sciagent's result-dict shape."""
    abstract = _reconstruct_inverted_abstract(work.get("abstract_inverted_index"))
    authors = [
        a.get("author", {}).get("display_name", "")
        for a in work.get("authorships", [])
    ]
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    venue = source.get("display_name") or ""
    # Prefer the open-access URL when available — sends the agent straight
    # to a fetchable PDF instead of a paywall landing page.
    oa_url = (work.get("open_access") or {}).get("oa_url")
    landing_url = primary.get("landing_page_url") or work.get("id", "")
    return {
        "index": index,
        "title": work.get("title") or "",
        "url": oa_url or landing_url,
        "snippet": abstract[:400],
        "age": "",
        "source_id": work.get("doi") or work.get("id"),
        "authors": authors,
        "year": work.get("publication_year"),
        "venue": venue,
        "backend": "openalex",
    }


def _reconstruct_inverted_abstract(inv: Optional[Dict[str, List[int]]]) -> str:
    """Reassemble OpenAlex's inverted-index abstract into normal prose."""
    if not inv:
        return ""
    positions: Dict[int, str] = {}
    for word, idx_list in inv.items():
        for idx in idx_list:
            positions[idx] = word
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

_ARXIV_BASE = "http://export.arxiv.org/api/query"
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def search_arxiv(query: str, num_results: int = 5, timeout: int = 20) -> List[Dict[str, Any]]:
    """Search arXiv. Auto-detects ID-style queries and does an ID lookup
    instead of a keyword search — IDs are unique, search-by-ID would be
    fuzzier than necessary."""
    arxiv_id = _extract_arxiv_id(query)
    if arxiv_id:
        params: Dict[str, Any] = {"id_list": arxiv_id}
    else:
        params = {
            "search_query": f"all:{query}",
            "max_results": min(num_results, 25),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

    try:
        resp = requests.get(_ARXIV_BASE, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ arXiv error: {e}")
        return []

    return _parse_arxiv_atom(resp.text)


def _extract_arxiv_id(query: str) -> Optional[str]:
    m = _ARXIV_RE.match(query)
    if not m:
        return None
    # Strip leading ``arxiv:`` if present; arXiv's id_list parameter wants
    # the bare ID.
    return m.group(0).strip().split(":", 1)[-1].strip()


def _parse_arxiv_atom(xml_text: str) -> List[Dict[str, Any]]:
    """Parse arXiv's Atom feed into result dicts. The schema is stable
    enough that stdlib ``ElementTree`` (no extra dep) is the right tool."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"❌ arXiv XML parse failed: {e}")
        return []

    results: List[Dict[str, Any]] = []
    for i, entry in enumerate(root.findall("a:entry", _ATOM_NS), 1):
        title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
        # arXiv entries always have at least ``<id>`` (the abs URL); PDF
        # link lives under ``<link title="pdf">`` when available.
        entry_id = (entry.findtext("a:id", default="", namespaces=_ATOM_NS) or "").strip()
        pdf_url = ""
        for link in entry.findall("a:link", _ATOM_NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
                break
        summary = (
            entry.findtext("a:summary", default="", namespaces=_ATOM_NS) or ""
        ).strip().replace("\n", " ")
        published = entry.findtext("a:published", default="", namespaces=_ATOM_NS) or ""
        year = None
        if len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])
        authors = [
            (a.findtext("a:name", default="", namespaces=_ATOM_NS) or "").strip()
            for a in entry.findall("a:author", _ATOM_NS)
        ]
        # arXiv ID is the trailing path segment of the id URL, e.g.
        # ``http://arxiv.org/abs/2401.12345v1`` → ``2401.12345v1``.
        arxiv_id = entry_id.rsplit("/", 1)[-1] if entry_id else ""

        results.append({
            "index": i,
            "title": title,
            "url": pdf_url or entry_id,
            "snippet": summary[:400],
            "age": published[:10] if published else "",
            "source_id": arxiv_id,
            "authors": authors,
            "year": year,
            "venue": "arXiv",
            "backend": "arxiv",
        })
    return results


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------

_CROSSREF_BASE = "https://api.crossref.org"


def search_crossref(query: str, num_results: int = 5, timeout: int = 15) -> List[Dict[str, Any]]:
    """Search Crossref or, when the query is a bare DOI, resolve it.

    Crossref is the authoritative DOI resolver — for a DOI lookup it returns
    the canonical metadata (exact title, journal, authors, year) that the
    publisher registered, which is what you want for citation strings.
    """
    doi = _extract_doi(query)
    if doi:
        return _crossref_doi_lookup(doi, timeout=timeout)
    return _crossref_keyword_search(query, num_results=num_results, timeout=timeout)


def _extract_doi(query: str) -> Optional[str]:
    m = _DOI_RE.match(query)
    return m.group(0).strip() if m else None


def _crossref_headers() -> Dict[str, str]:
    # Crossref's polite pool is identified via User-Agent, not a query param.
    email = _polite_email()
    if email:
        return {"User-Agent": f"sciagent/1.0 (mailto:{email})"}
    return {"User-Agent": "sciagent/1.0"}


def _crossref_doi_lookup(doi: str, timeout: int = 15) -> List[Dict[str, Any]]:
    try:
        resp = requests.get(
            f"{_CROSSREF_BASE}/works/{doi}",
            headers=_crossref_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        message = resp.json().get("message", {})
    except requests.exceptions.RequestException as e:
        print(f"❌ Crossref DOI lookup error: {e}")
        return []
    return [_crossref_work_to_result(message, 1)]


def _crossref_keyword_search(query: str, num_results: int = 5, timeout: int = 15) -> List[Dict[str, Any]]:
    params = {"query": query, "rows": min(num_results, 25)}
    try:
        resp = requests.get(
            f"{_CROSSREF_BASE}/works",
            params=params,
            headers=_crossref_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except requests.exceptions.RequestException as e:
        print(f"❌ Crossref search error: {e}")
        return []
    return [_crossref_work_to_result(item, i) for i, item in enumerate(items, 1)]


def _crossref_work_to_result(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    title = (item.get("title") or [""])[0]
    doi = item.get("DOI", "")
    url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")
    abstract = (item.get("abstract") or "").strip()
    # Crossref abstracts often arrive wrapped in JATS XML — strip the
    # surface markup so the snippet is readable.
    abstract = re.sub(r"<[^>]+>", " ", abstract)
    abstract = re.sub(r"\s+", " ", abstract).strip()
    authors = [
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in item.get("author", [])
    ]
    year = None
    issued = item.get("issued", {}).get("date-parts") or item.get("published", {}).get("date-parts")
    if issued and isinstance(issued, list) and issued[0]:
        first = issued[0][0]
        if isinstance(first, int):
            year = first
    venue = ""
    container = item.get("container-title")
    if isinstance(container, list) and container:
        venue = container[0]
    elif isinstance(container, str):
        venue = container
    return {
        "index": index,
        "title": title,
        "url": url,
        "snippet": abstract[:400],
        "age": "",
        "source_id": doi or None,
        "authors": authors,
        "year": year,
        "venue": venue,
        "backend": "crossref",
    }
