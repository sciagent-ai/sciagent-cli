"""Lock the ComputeTool class surface against accidental displacement.

Background: a careless edit can move methods out of the class scope (e.g.,
inserting a module-level function in the middle of class indentation
displaces methods into nested-function scope of that helper). The tests
that exercise compute_run still parse the file fine — Python's syntax
allows it — but the class loses methods. Symptom in production:
`'ComputeTool' object has no attribute 'execute'` (or 'to_schema') when
the agent dispatches compute. Hard to catch by running the suite if the
displaced methods aren't on the test path.

This test does a cheap structural check on the public surface so a
regression like that fails locally before reaching the agent.
"""

from __future__ import annotations

import pytest

from sciagent.tools.atomic.compute import ComputeTool, session_context_block


# Methods/attrs every consumer of ComputeTool depends on. Add to this
# list as the surface grows; treat removals as deliberate (they should
# break this test loud).
_REQUIRED_INSTANCE_ATTRS = (
    "name",
    "description",
    "parameters",
    "execute",
    "to_schema",
    "set_shared_session",
    "_get_session_id",
    "_validate_path_contract",
    "_write_session_manifest",
)


@pytest.mark.parametrize("attr", _REQUIRED_INSTANCE_ATTRS)
def test_compute_tool_has_required_attribute(attr):
    tool = ComputeTool()
    assert hasattr(tool, attr), (
        f"ComputeTool is missing '{attr}'. Likely cause: a recent edit "
        f"misplaced a module-level helper inside the class scope, "
        f"displacing methods into nested-function scope."
    )


def test_session_context_block_is_module_level():
    """The helper must live at module level (importable from
    sciagent.tools.atomic.compute), not inside ComputeTool. SubAgent
    construction imports it directly; if it accidentally became a
    method, that import would fail."""
    # Importing it directly is the contract — succeeds only if it's
    # at module level.
    from sciagent.tools.atomic.compute import session_context_block as _h
    assert callable(_h)
    # And it shouldn't be a bound method on the class.
    assert not hasattr(ComputeTool, "session_context_block")


def test_compute_tool_to_schema_returns_expected_shape():
    """to_schema is what the agent's tool-dispatch layer calls; lock the
    return shape so a quiet rename doesn't break the dispatch."""
    schema = ComputeTool().to_schema()
    assert isinstance(schema, dict)
    assert schema.get("name") == "compute_run"
    assert "description" in schema
    assert "parameters" in schema


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
