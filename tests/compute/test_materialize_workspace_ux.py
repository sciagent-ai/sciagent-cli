"""UX tests for materialize_workspace.

The dominant first-turn failure in real sessions: an agent called
`materialize_workspace()` with no args in a fresh session whose own
workspace bucket didn't exist yet, expecting to fetch a PRIOR session's
data. The default-session resolution returned an empty bucket name and
aws s3 sync failed with NoSuchBucket — opaque, agent burned a turn on
recovery.

Fixes pinned here:
  - Accept a `uri=` parameter for full-URI fetches (cross-session,
    hand-off, manuscript-cited paths).
  - Recover when `subpath=` is mistakenly passed a full URI (common
    LLM slip).
  - When the resolved URI doesn't exist, surface an actionable error
    naming the three recovery paths instead of a raw cloud-CLI error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.tools.atomic.materialize_workspace import (
    MaterializeWorkspaceTool,
    _extract_session_from_uri,
    _looks_like_uri,
)
from sciagent.tools.registry import ToolResult


@pytest.fixture(autouse=True)
def _isolate_shared_session():
    from sciagent.tools.atomic.compute import ComputeTool
    prev = ComputeTool._shared_session_id
    yield
    ComputeTool._shared_session_id = prev


# ----- helpers --------------------------------------------------------------


def test_looks_like_uri_recognizes_supported_schemes():
    assert _looks_like_uri("s3://bucket/x")
    assert _looks_like_uri("gs://bucket/x")
    assert _looks_like_uri("az://container/x")
    assert _looks_like_uri("r2://bucket/x")
    assert _looks_like_uri("oci://bucket/x")
    assert not _looks_like_uri("/local/path")
    assert not _looks_like_uri("run-001/fields/")
    assert not _looks_like_uri("")
    assert not _looks_like_uri(None)


def test_extract_session_from_uri_parses_bucket_name():
    assert _extract_session_from_uri("s3://sciagent-workspace-c33a068d6fcc/") == "c33a068d6fcc"
    assert _extract_session_from_uri(
        "s3://sciagent-workspace-abc12345/run-001/fields/"
    ) == "abc12345"
    # The '-input-N' suffix on local-source mounts shouldn't break extraction —
    # the session id is still the prefix capture.
    assert _extract_session_from_uri(
        "s3://sciagent-workspace-c33a068d6fcc-input-0/postProcessing/"
    ) == "c33a068d6fcc"
    assert _extract_session_from_uri("s3://other-bucket/") is None


# ----- three call shapes ----------------------------------------------------


def test_no_session_no_uri_returns_actionable_error():
    """Fresh session with no compute_run yet AND no uri= → don't crash on
    the cloud CLI. Surface the three recovery paths."""
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool._shared_session_id = None

    tool = MaterializeWorkspaceTool(working_dir=".")
    result = tool.execute()

    assert result.success is False
    err = (result.error or "").lower()
    assert "session_id" in err
    assert "uri" in err
    assert "compute_run" in err  # third recovery path


def test_full_uri_form_skips_session_resolution():
    """uri='s3://sciagent-workspace-other-sid/...' must be passed through
    to materialize verbatim, regardless of the current session."""
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool._shared_session_id = "current-sess"

    tool = MaterializeWorkspaceTool(working_dir=".")
    fake_materialize_result = ToolResult(
        success=True,
        output={"uri": "s3://sciagent-workspace-prior-sid/", "files": []},
    )
    with patch(
        "sciagent.tools.atomic.materialize_workspace.MaterializeTool"
    ) as MaterializeCls:
        MaterializeCls.return_value.execute.return_value = fake_materialize_result
        result = tool.execute(uri="s3://sciagent-workspace-prior-sid/run-1/")

    assert result.success is True
    # The URI was forwarded to materialize.execute verbatim.
    call_kwargs = MaterializeCls.return_value.execute.call_args.kwargs
    assert call_kwargs["uri"] == "s3://sciagent-workspace-prior-sid/run-1/"
    # session_id in the result reflects the URI's session, not the current one.
    assert result.output["session_id"] == "prior-sid"


def test_explicit_session_id_builds_workspace_uri():
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool._shared_session_id = "current-sess"

    tool = MaterializeWorkspaceTool(working_dir=".")
    fake_materialize_result = ToolResult(success=True, output={"files": []})
    with patch(
        "sciagent.tools.atomic.materialize_workspace.MaterializeTool"
    ) as MaterializeCls, patch(
        "sciagent.compute.backends.skypilot.SkyPilotBackend.resolve_workspace_store",
        return_value="s3",
    ):
        MaterializeCls.return_value.execute.return_value = fake_materialize_result
        result = tool.execute(session_id="prior-sid", subpath="run-1/fields/")

    call_kwargs = MaterializeCls.return_value.execute.call_args.kwargs
    assert call_kwargs["uri"] == "s3://sciagent-workspace-prior-sid/run-1/fields/"
    assert result.output["session_id"] == "prior-sid"


def test_subpath_holding_full_uri_is_promoted():
    """Common LLM mistake: passing the URI in `subpath`. Don't fail the
    request — recover and treat it as the URI."""
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool._shared_session_id = "current-sess"

    tool = MaterializeWorkspaceTool(working_dir=".")
    fake_materialize_result = ToolResult(success=True, output={"files": []})
    with patch(
        "sciagent.tools.atomic.materialize_workspace.MaterializeTool"
    ) as MaterializeCls:
        MaterializeCls.return_value.execute.return_value = fake_materialize_result
        result = tool.execute(subpath="s3://sciagent-workspace-prior-sid/data/")

    call_kwargs = MaterializeCls.return_value.execute.call_args.kwargs
    assert call_kwargs["uri"] == "s3://sciagent-workspace-prior-sid/data/"
    assert result.success is True


# ----- NoSuchBucket recovery -----------------------------------------------


@pytest.mark.parametrize(
    "raw_error",
    [
        "aws sync exit 1: fatal error: An error occurred (NoSuchBucket) when calling the ListObjectsV2 operation: The specified bucket does not exist",
        "gsutil rsync exit 1: BucketNotFoundException: 404 bucket does not exist",
        "az storage blob: NoSuchBucket",
    ],
)
def test_no_such_bucket_error_is_rewritten_to_actionable(raw_error):
    """The exact agent_34 failure shape: cloud-CLI returns NoSuchBucket;
    materialize_workspace must rewrite it to name the recovery paths
    instead of leaving the LLM to parse aws/gsutil/az error grammar."""
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool._shared_session_id = "stale-sid"

    tool = MaterializeWorkspaceTool(working_dir=".")
    materialize_result = ToolResult(success=False, output=None, error=raw_error)
    with patch(
        "sciagent.tools.atomic.materialize_workspace.MaterializeTool"
    ) as MaterializeCls, patch(
        "sciagent.compute.backends.skypilot.SkyPilotBackend.resolve_workspace_store",
        return_value="s3",
    ):
        MaterializeCls.return_value.execute.return_value = materialize_result
        result = tool.execute()  # no args; relies on stale current-session

    assert result.success is False
    assert result.output["failure_type"] == "no_such_bucket"
    err = (result.error or "").lower()
    # Recovery hints name all three escape paths.
    assert "session_id" in err
    assert "uri" in err
    assert "compute_run" in err
    # The actual URI we tried is in the payload so the agent can correct it.
    assert "stale-sid" in result.output["workspace_uri"]


# ----- result enrichment ----------------------------------------------------


def test_successful_call_enriches_payload_with_session_and_uri():
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool._shared_session_id = "abc123"

    tool = MaterializeWorkspaceTool(working_dir=".")
    fake_materialize_result = ToolResult(
        success=True,
        output={"files": [{"path": "x", "bytes": 1}], "file_count": 1},
    )
    with patch(
        "sciagent.tools.atomic.materialize_workspace.MaterializeTool"
    ) as MaterializeCls, patch(
        "sciagent.compute.backends.skypilot.SkyPilotBackend.resolve_workspace_store",
        return_value="s3",
    ):
        MaterializeCls.return_value.execute.return_value = fake_materialize_result
        result = tool.execute()

    assert result.success is True
    assert result.output["session_id"] == "abc123"
    assert result.output["workspace_uri"] == "s3://sciagent-workspace-abc123/"
    # Original materialize fields preserved.
    assert result.output["file_count"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
