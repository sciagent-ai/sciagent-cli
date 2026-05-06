"""Pin the session_context_block behavior.

Background: the orchestrator dispatches sub-agents with `produces_uris`
declared up-front. For cluster-internal handoff via the workspace bucket,
those URIs must reference the concrete session bucket name. Without the
session_id concretely known to the orchestrator, it falls back to a
wildcard URI (`*` in bucket name) which AWS / GCS cloud-CLIs reject in
list operations — observed killing live sessions.

This test pins the contract: when ComputeTool has a session_id set,
session_context_block() returns a prompt-ready block naming the URI;
otherwise it returns empty so the prompt composer can skip cleanly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sciagent.tools.atomic.compute import (
    ComputeTool,
    session_context_block,
)


@pytest.fixture(autouse=True)
def _restore_shared_session():
    prev = ComputeTool._shared_session_id
    yield
    ComputeTool._shared_session_id = prev


def test_block_empty_when_no_session():
    ComputeTool._shared_session_id = None
    assert session_context_block() == ""


def test_block_names_concrete_uri_when_session_set():
    ComputeTool._shared_session_id = "abc12345"
    with patch(
        "sciagent.compute.backends.skypilot.SkyPilotBackend.resolve_workspace_store",
        return_value="s3",
    ):
        block = session_context_block()
    # The concrete URI MUST appear (the whole point — no wildcards, no
    # placeholders the orchestrator would substitute incorrectly).
    assert "s3://sciagent-workspace-abc12345/" in block
    # And the prompt language should drive the orchestrator toward
    # using it in produces_uris.
    assert "produces_uris" in block
    assert "wildcard" in block.lower()


def test_block_uses_chosen_store_scheme():
    """When the resolved store is GCS, the URI scheme must be gs://, not s3://.
    Cloud-aware: never hardcode S3."""
    ComputeTool._shared_session_id = "abc12345"
    with patch(
        "sciagent.compute.backends.skypilot.SkyPilotBackend.resolve_workspace_store",
        return_value="gcs",
    ):
        block = session_context_block()
    assert "gs://sciagent-workspace-abc12345/" in block
    assert "s3://" not in block


def test_block_empty_when_sky_unavailable():
    """If SkyPilot isn't installed/configured, return empty rather than
    inject a half-formed URI. Cluster work won't run anyway, and the
    orchestrator's prompt shouldn't be polluted with broken hints."""
    ComputeTool._shared_session_id = "abc12345"
    with patch(
        "sciagent.compute.backends.skypilot.SkyPilotBackend.resolve_workspace_store",
        side_effect=RuntimeError("SkyPilot not installed"),
    ):
        assert session_context_block() == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
