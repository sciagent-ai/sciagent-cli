"""Main agent must NOT see compute_* tools — they're reachable only via
the `compute` subagent (which has them in allowed_tools).

The architectural intent is documented at subagent.py:380-388: cloud
chatter (install logs, status polls, large bg_output) stays inside the
compute subagent's context bubble; the main agent only ever sees a
bounded summary returned by TaskTool. If compute_* live on the main
agent's toolset, that boundary leaks.

These tests guard the boundary by asserting:
  1. The full atomic registry has compute_* (so subagents can filter them in).
  2. ToolRegistry.clone(exclude=...) drops them on demand.
  3. Subagent filtering still finds compute_* on the unfiltered registry.
"""

from __future__ import annotations

from sciagent.tools.registry import create_atomic_registry


COMPUTE_TOOLS = {"compute_run", "compute_exec", "compute_cluster"}


def test_full_atomic_registry_has_all_compute_tools():
    """The shared registry that backs subagent tool filtering must have
    every compute tool — else compute subagent's allowed_tools resolves
    to None and the subagent can't run anything."""
    full = create_atomic_registry()
    for name in COMPUTE_TOOLS:
        assert full.get(name) is not None, (
            f"{name} missing from create_atomic_registry; compute subagent "
            f"will be unable to run cloud jobs."
        )


def test_clone_drops_compute_tools_on_demand():
    full = create_atomic_registry()
    main_view = full.clone(exclude=COMPUTE_TOOLS)

    for name in COMPUTE_TOOLS:
        assert main_view.get(name) is None, (
            f"{name} leaked into the main agent's view of the toolset."
        )

    # Non-compute tools must still be present so the main agent's normal
    # work (bash, file_ops, search, todo, etc.) is unaffected.
    for name in ("bash", "file_ops", "search", "todo", "service_search",
                 "bg_status", "bg_wait"):
        assert main_view.get(name) is not None, (
            f"{name} accidentally dropped from main agent view by the "
            f"clone(exclude=...) call."
        )


def test_subagent_filtering_still_finds_compute_tools_on_full_registry():
    """SubAgent filters from the orchestrator's reference to the full
    registry, not from the main agent's clone. The compute tools must be
    discoverable there for compute subagent's allowed_tools to bind."""
    full = create_atomic_registry()
    # Simulate the lookup SubAgent.__init__ performs (subagent.py:107-110).
    found = {name: full.get(name) for name in COMPUTE_TOOLS}
    assert all(v is not None for v in found.values())


def test_clone_returns_independent_registry():
    """Mutating one view must not affect the other — they share tool
    instances but the registry containers are separate."""
    full = create_atomic_registry()
    main_view = full.clone(exclude=COMPUTE_TOOLS)

    # Add a new tool to the clone; full registry shouldn't see it.
    from sciagent.tools.registry import BaseTool

    class _Marker(BaseTool):
        name = "_marker_only_in_clone"
        description = "test"
        parameters = {"type": "object", "properties": {}}

    main_view.register(_Marker())
    assert full.get("_marker_only_in_clone") is None
    assert main_view.get("_marker_only_in_clone") is not None
