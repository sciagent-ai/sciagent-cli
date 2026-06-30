"""service_search tool: case-insensitive discovery against the live
registry.yaml plus a tiny synthetic registry for shape assertions.

Targets the failure mode caught in user testing: the agent grep'd
``OpenFOAM`` (capitalized) against lowercase YAML keys, came back with 3
hits in capability prose, decided the registry was empty, and went to
build-service. service_search must (a) be case-insensitive, (b) match
across description / packages / capabilities, (c) return a structured
output the agent can act on without re-reading the YAML.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from sciagent.tools.atomic.service_search import ServiceSearchTool


def _write_registry(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "registry.yaml"
    p.write_text(dedent(body).lstrip())
    return p


def test_no_keyword_returns_full_catalog():
    """A no-arg / empty-keyword call is a 'show me what you have' intent —
    return the lightweight name+description catalog for every service so the
    agent can see the surface without reading the YAML. Less friction than
    forcing a retry with a placeholder keyword."""
    tool = ServiceSearchTool()
    result = tool.execute()
    assert result.success is True
    assert result.output["keyword"] is None
    assert result.output["match_count"] >= 1
    # Each catalog entry must carry name + description, no packages/capabilities
    # (those are fetched only when the agent narrows with a keyword).
    for m in result.output["matches"]:
        assert "name" in m
        assert "description" in m
        assert "packages" not in m


def test_alias_kwargs_accepted(tmp_path: Path):
    """The model frequently calls service_search with `query=` or `service=`
    instead of `keyword=`. Accept the common aliases so a kwarg-name typo
    doesn't surface as an unhelpful 'unexpected keyword argument' error."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          openfoam:
            description: "OpenFOAM CFD"
            packages: []
            capabilities: []
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))

    for alias in ("query", "q", "search", "name", "service", "pattern"):
        out = tool.execute(**{alias: "openfoam"})
        assert out.success is True, f"alias '{alias}' did not bind"
        assert out.output["match_count"] == 1, f"alias '{alias}' missed the match"


def test_case_insensitive_match_on_name(tmp_path: Path):
    """The user's bug: agent searched for 'OpenFOAM' (capitalized) and got
    no hits because the YAML key is `openfoam:`. service_search must match
    regardless of case."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          openfoam:
            description: "OpenFOAM CFD"
            image: ghcr.io/x/openfoam
            packages: []
            capabilities: ["incompressible flow"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))

    out = tool.execute(keyword="OpenFOAM")
    assert out.success is True
    assert out.output["match_count"] == 1
    assert out.output["matches"][0]["name"] == "openfoam"


def test_match_across_packages_and_capabilities(tmp_path: Path):
    """A service whose name doesn't carry the keyword but whose packages or
    capabilities do must still match."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          scipy-base:
            description: "Scientific Python foundation"
            packages: [numpy, scipy, pandas]
            capabilities: ["Numerical computing"]
          gromacs:
            description: "Molecular dynamics"
            packages: [gromacs]
            capabilities: ["MD simulation"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))

    # Keyword in packages, not in name
    out = tool.execute(keyword="numpy")
    assert out.output["match_count"] == 1
    assert out.output["matches"][0]["name"] == "scipy-base"

    # Keyword in capability prose only
    out = tool.execute(keyword="molecular")
    assert out.output["match_count"] == 1
    assert out.output["matches"][0]["name"] == "gromacs"


def test_no_matches_returns_empty_list_not_error(tmp_path: Path):
    """Empty result is not an error — it's a signal to consider build-service.
    The tool must succeed with match_count=0 so the agent can branch on it."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          scipy-base:
            description: "Scientific Python"
            packages: [numpy]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="quantum-thing")
    assert out.success is True
    assert out.output["match_count"] == 0
    assert out.output["matches"] == []


def test_capabilities_truncated_to_keep_result_lean(tmp_path: Path):
    """A service with 20 capability bullets shouldn't blow up the agent's
    context. Cap at 6 — full list is one Read away."""
    p = tmp_path / "registry.yaml"
    lines = ["services:", "  rich:", '    description: "rich"', "    capabilities:"]
    lines.extend(f'      - "cap{i}"' for i in range(20))
    p.write_text("\n".join(lines) + "\n")

    tool = ServiceSearchTool(registry_path=str(p))
    out = tool.execute(keyword="rich")
    assert out.success is True
    assert len(out.output["matches"][0]["capabilities"]) == 6


def test_finds_openfoam_in_live_registry():
    """End-to-end against the installed registry. Guards the regression the
    user actually hit: openfoam* services must be discoverable by keyword."""
    tool = ServiceSearchTool()
    out = tool.execute(keyword="openfoam")
    assert out.success is True
    names = [m["name"] for m in out.output["matches"]]
    assert "openfoam" in names
    assert "openfoam-swak4foam" in names
    assert "openfoam-swak4foam-2012" in names


def test_finds_by_capability_keyword_in_live_registry():
    """The agent often searches by domain-keyword (CFD, MD, EDA, optics)
    rather than service name. The live registry's capability prose should
    surface relevant services for these queries."""
    tool = ServiceSearchTool()
    out = tool.execute(keyword="CFD")
    assert out.success is True
    assert out.output["match_count"] >= 1
    names = [m["name"] for m in out.output["matches"]]
    assert any(n.startswith("openfoam") for n in names)


def test_missing_registry_file_returns_error(tmp_path: Path):
    tool = ServiceSearchTool(registry_path=str(tmp_path / "nope.yaml"))
    out = tool.execute(keyword="anything")
    assert out.success is False
    assert "registry" in (out.error or "").lower()


# ---------------------------------------------------------------------------
# Token-fallback matching (multi-word queries + plural→singular strip)
# ---------------------------------------------------------------------------


def test_match_mode_present_on_exact_matches(tmp_path: Path):
    """Every result row carries a `match_mode` field so downstream code can
    branch on the discovery path. Exact substring hits report 'exact' and
    leave `matched_tokens` null."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          openfoam:
            description: "OpenFOAM CFD"
            packages: []
            capabilities: ["incompressible flow"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="openfoam")
    assert out.output["matches"][0]["match_mode"] == "exact"
    assert out.output["matches"][0]["matched_tokens"] is None


def test_multi_word_query_with_tokens_spanning_fields(tmp_path: Path):
    """The motivating case: a query like 'molecular dynamics' where one
    token lives in the description and the other in the capabilities list.
    Exact substring fails (no contiguous match); token-fallback succeeds and
    reports match_mode='token' with the singularized tokens that matched."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          amber:
            description: "Amber molecular force field"
            packages: [amber]
            capabilities: ["dynamics simulation"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="molecular dynamics")
    assert out.success is True
    assert out.output["match_count"] == 1
    m = out.output["matches"][0]
    assert m["name"] == "amber"
    assert m["match_mode"] == "token"
    assert set(m["matched_tokens"]) == {"molecular", "dynamic"}


def test_plural_query_matches_singular_haystack(tmp_path: Path):
    """A plural query ('simulations') falls through exact match when the
    haystack carries the singular ('simulation'). The guarded singularizer
    strips the 's' in the token-fallback pass."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          gromacs:
            description: "Molecular dynamics"
            packages: [gromacs]
            capabilities: ["MD simulation"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="simulations")
    assert out.output["match_count"] == 1
    m = out.output["matches"][0]
    assert m["name"] == "gromacs"
    assert m["match_mode"] == "token"
    assert m["matched_tokens"] == ["simulation"]


def test_singularizer_guards_against_overstripping(tmp_path: Path):
    """Words ending in 'ss' / 'us' / 'is' / 'os' / 'as' are NOT plurals — the
    singularizer must leave them alone. If 'analysis' got stripped to
    'analysi' the token-fallback would silently miss this service."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          scipy-base:
            description: "Numerical analysis foundation"
            packages: [numpy, scipy]
            capabilities: ["statistical analysis"]
          openfoam:
            description: "CFD solver"
            packages: []
            capabilities: ["process pipeline"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))

    # 'analysis' must match (ends in 'is' — not stripped).
    out = tool.execute(keyword="statistical analysis")
    names = [m["name"] for m in out.output["matches"]]
    assert "scipy-base" in names

    # 'process' must not be over-stripped to 'proce'.
    out = tool.execute(keyword="process")
    names = [m["name"] for m in out.output["matches"]]
    assert "openfoam" in names


def test_token_fallback_handles_ies_to_y(tmp_path: Path):
    """Plural 'frequencies' must singularize to 'frequency' so it matches a
    haystack with 'frequency'."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          spectrum-tool:
            description: "Frequency-domain analysis"
            packages: []
            capabilities: ["spectral methods"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="frequencies")
    assert out.output["match_count"] == 1
    m = out.output["matches"][0]
    assert m["name"] == "spectrum-tool"
    assert m["matched_tokens"] == ["frequency"]


def test_exact_match_takes_precedence_over_token(tmp_path: Path):
    """When an exact substring hit exists, the token-fallback pass does NOT
    run — exact-mode results are returned alone. This keeps the result set
    stable and precise when the registry literally carries the query."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          exact-hit:
            description: "Has molecular dynamics in the description verbatim"
            packages: []
            capabilities: []
          token-hit:
            description: "Amber molecular force field"
            packages: []
            capabilities: ["dynamics simulation"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="molecular dynamics")
    assert out.output["match_count"] == 1
    m = out.output["matches"][0]
    assert m["name"] == "exact-hit"
    assert m["match_mode"] == "exact"


def test_token_fallback_returns_zero_when_a_token_is_missing(tmp_path: Path):
    """Token mode requires ALL query tokens to appear in the haystack —
    partial hits do not match. Otherwise a 'cfd openfoam' query would
    surface every service that has 'cfd' but no openfoam."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          openfoam:
            description: "OpenFOAM CFD solver"
            packages: []
            capabilities: []
          paraview:
            description: "Visualization toolkit"
            packages: []
            capabilities: []
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="visualization fortran")
    assert out.output["match_count"] == 0


def test_token_fallback_across_hyphenated_service_names(tmp_path: Path):
    """Service names like 'openfoam-swak4foam' tokenize into separate words.
    A query 'swak openfoam' that has no exact contiguous form must still
    surface the service via the token path."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          openfoam-swak4foam-2012:
            description: "OpenFOAM with swak4foam utilities"
            packages: []
            capabilities: []
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="swak openfoam")
    assert out.output["match_count"] == 1
    m = out.output["matches"][0]
    assert m["name"] == "openfoam-swak4foam-2012"
    assert m["match_mode"] == "token"
