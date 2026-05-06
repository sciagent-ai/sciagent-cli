"""compute_run mode= dispatch — managed-jobs vs cluster routing.

Verifies:
  - Default (no mode kwarg) routes through the existing managed-jobs path
    (selected_backend.run). This keeps every existing test green.
  - mode="cluster" routes through router.launch_cluster, NOT
    selected_backend.run.
  - mode="cluster" with a non-skypilot backend produces a structured
    error pointing the agent at backend='skypilot'.
  - mode="cluster" auto-generates a cluster_name when omitted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.tools.atomic.compute import ComputeTool, ToolResult


def _tool_with_mock_router(skypilot=True):
    """Build a ComputeTool whose router is mocked, with skypilot backend
    selected by default."""
    from sciagent.compute.job import StorageMount, StorageMode

    tool = ComputeTool(working_dir=".")

    fake_router = MagicMock()
    fake_backend = MagicMock()
    fake_backend.name = "skypilot" if skypilot else "local"
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    # P0.5 auto-mount stub: real StorageMount so storage_list stays string-typed.
    fake_backend.build_session_workspace_mount.return_value = StorageMount(
        path="/workspace",
        bucket="sciagent-workspace-test",
        store="s3",
        mode=StorageMode.MOUNT,
        source=None,
        persistent=True,
        kind="durable",
    )
    fake_router.list_backends.return_value = (
        ["skypilot"] if skypilot else ["local"]
    )
    fake_router._backends = {fake_backend.name: fake_backend}
    fake_router.select.return_value = (fake_backend, "test routing")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}

    tool._router = fake_router
    return tool, fake_router, fake_backend


def test_mode_default_uses_managed_jobs_path():
    """Default mode='job' must call backend.run, NOT router.launch_cluster.
    Backward compat for every existing compute_run caller."""
    tool, router, backend = _tool_with_mock_router()
    backend.run.return_value = ("sciagent-job-abc", 1)

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
    )
    assert out.success is True
    backend.run.assert_called_once()
    router.launch_cluster.assert_not_called()


def test_mode_cluster_routes_to_launch_cluster():
    tool, router, backend = _tool_with_mock_router()
    router.launch_cluster.return_value = ("my-cluster", 1)

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
        mode="cluster",
        cluster_name="my-cluster",
        autostop_minutes=15,
    )
    assert out.success is True
    router.launch_cluster.assert_called_once()
    _, kwargs = router.launch_cluster.call_args
    assert kwargs["cluster_name"] == "my-cluster"
    assert kwargs["autostop_minutes"] == 15
    # Must NOT also call managed-jobs path.
    backend.run.assert_not_called()


def test_mode_cluster_returns_structured_cluster_output():
    tool, router, backend = _tool_with_mock_router()
    router.launch_cluster.return_value = ("c1", 7)

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
        mode="cluster",
        cluster_name="c1",
    )
    assert out.success is True
    # Cluster-mode result shape differs from managed-jobs ("cluster_name",
    # "cluster_job_id", "mode": "cluster") — agents and the prompt-side
    # follow-up logic depend on the discriminator.
    assert out.output["mode"] == "cluster"
    assert out.output["cluster_name"] == "c1"
    assert out.output["cluster_job_id"] == 7


def test_mode_cluster_with_local_backend_returns_structured_error():
    """Cluster mode is a SkyPilot concept. Asking for it on local Docker
    must surface a clear error, not silently fall back to managed-jobs
    or to local Docker."""
    tool, router, backend = _tool_with_mock_router(skypilot=False)

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="local",
        mode="cluster",
    )
    assert out.success is False
    assert out.output["failure_type"] == "mode_backend_mismatch"
    router.launch_cluster.assert_not_called()


def test_mode_cluster_auto_generates_cluster_name():
    """If the agent doesn't pass cluster_name, the tool generates one of
    the form sciagent-<session>-i. This makes 'just give me a warm
    cluster' a one-arg call."""
    tool, router, backend = _tool_with_mock_router()
    router.launch_cluster.return_value = ("auto-name", 1)

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
        mode="cluster",
        # cluster_name intentionally omitted
    )
    assert out.success is True
    _, kwargs = router.launch_cluster.call_args
    cn = kwargs["cluster_name"]
    assert cn.startswith("sciagent-")
    assert cn.endswith("-i")
