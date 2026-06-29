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

from sciagent.tools.atomic.service_search import ServiceDetailTool, ServiceSearchTool
from sciagent.tools.registry import ToolRegistry


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


def test_token_fallback_handles_reversed_phrase(tmp_path: Path):
    """Phrase 'S4 RCWA' doesn't appear contiguously in the rcwa entry —
    description has 'RCWA/S4 ...' (reversed) and 'S4' lives alone in
    packages. Exact substring fails; token-AND fallback must catch it."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          rcwa:
            description: "RCWA/S4 - Rigorous Coupled Wave Analysis"
            packages: [S4, numpy]
            capabilities: ["Photonic crystal simulations"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="S4 RCWA")
    assert out.success is True
    assert out.output["match_count"] == 1
    assert out.output["matches"][0]["name"] == "rcwa"
    assert out.output["match_mode"] == "token"


def test_token_fallback_handles_plural_query(tmp_path: Path):
    """'photonics' (plural) must find a registry entry whose capability says
    'Photonic crystal simulations' (singular). One-char plural strip in the
    fallback is enough — no full stemmer required."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          rcwa:
            description: "RCWA"
            packages: []
            capabilities: ["Photonic crystal simulations"]
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="photonics")
    assert out.success is True
    assert out.output["match_count"] == 1
    assert out.output["matches"][0]["name"] == "rcwa"
    assert out.output["match_mode"] == "token"


def test_service_detail_via_registry_does_not_collide_on_name(tmp_path: Path):
    """Regression: an agent call like ``service_detail(name="sci-core")`` used
    to crash with ``ToolRegistry.execute() got multiple values for argument
    'name'`` because the dispatcher's own ``name`` parameter (the tool key)
    collided with the spread ``**arguments`` carrying its own ``name`` kwarg.
    The fix makes the dispatcher's ``name`` positional-only — this test pins
    that contract."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          sci-core:
            description: "Core scientific computing"
            packages: [numpy, scipy]
            capabilities: ["Numerical computing"]
        """,
    )
    tools = ToolRegistry()
    tools.register(ServiceDetailTool(registry_path=str(registry)))

    # Exactly mirrors agent.py: tools.execute(tool_call.name, **tool_call.arguments)
    result = tools.execute("service_detail", name="sci-core")
    assert result.success is True, result.error
    assert result.output["name"] == "sci-core"
    assert result.output["entry"]["description"] == "Core scientific computing"


def test_exact_match_still_preferred_over_fallback(tmp_path: Path):
    """If the exact substring matches anything, the token fallback must not
    fire — exact is more precise and the match_mode should reflect that."""
    registry = _write_registry(
        tmp_path,
        """
        services:
          rcwa:
            description: "RCWA/S4"
            packages: []
            capabilities: []
        """,
    )
    tool = ServiceSearchTool(registry_path=str(registry))
    out = tool.execute(keyword="rcwa")
    assert out.output["match_count"] == 1
    assert out.output["match_mode"] == "exact"
