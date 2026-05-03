"""compute_exec atomic tool — happy path + error surfacing.

The tool itself is thin (build a Job, call router.exec_on_cluster,
return result). What we pin here:
  - Required-arg validation (cluster_name, command).
  - Alias kwargs (cluster, name) bind to cluster_name.
  - LaunchError from the backend surfaces as a structured failure
    pointing the agent at compute_cluster(action='status') for diagnosis.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.job import LaunchError
from sciagent.tools.atomic.compute_exec import ComputeExecTool


def test_missing_cluster_name_returns_error():
    out = ComputeExecTool().execute(command="ls")
    assert out.success is False
    assert "cluster_name" in (out.error or "")


def test_missing_command_returns_error():
    out = ComputeExecTool().execute(cluster_name="x")
    assert out.success is False
    assert "command" in (out.error or "")


def test_alias_cluster_kwarg_accepted():
    """Model often passes cluster= or name= instead of cluster_name=. Accept
    the obvious aliases instead of surfacing 'unexpected keyword argument'."""
    tool = ComputeExecTool()
    fake_router = MagicMock()
    fake_router.exec_on_cluster.return_value = ("x", 7)
    tool._router = fake_router

    out = tool.execute(cluster="x", command="ls")
    assert out.success is True
    assert out.output["cluster_name"] == "x"
    assert out.output["cluster_job_id"] == 7


def test_happy_path_returns_cluster_and_job_id():
    tool = ComputeExecTool()
    fake_router = MagicMock()
    fake_router.exec_on_cluster.return_value = ("warm", 42)
    tool._router = fake_router

    out = tool.execute(cluster_name="warm", command="echo hi")
    assert out.success is True
    assert out.output["cluster_job_id"] == 42
    assert out.output["mode"] == "cluster_exec"
    # Must surface the sky CLI hint so the agent knows how to fetch logs
    # without us building a separate bg_wait integration.
    assert "sky logs warm 42" in out.output["message"]


def test_launch_error_surfaces_status_hint():
    """When sky.exec rejects (e.g., cluster not UP), the tool must point
    the agent at compute_cluster(action='status') instead of returning
    just a raw 'sky.exec rejected' that the agent can't act on."""
    tool = ComputeExecTool()
    fake_router = MagicMock()
    fake_router.exec_on_cluster.side_effect = LaunchError(
        "cluster not found", cluster_name="dead"
    )
    tool._router = fake_router

    out = tool.execute(cluster_name="dead", command="ls")
    assert out.success is False
    assert "compute_cluster(action='status'" in out.output["hint"]


def test_runtime_error_from_router_surfaces_cleanly():
    """If SkyPilot isn't installed, router._require_skypilot raises
    RuntimeError. Must surface as the tool's error, not as an unhandled
    exception."""
    tool = ComputeExecTool()
    fake_router = MagicMock()
    fake_router.exec_on_cluster.side_effect = RuntimeError(
        "Cluster-mode operations require SkyPilot."
    )
    tool._router = fake_router

    out = tool.execute(cluster_name="x", command="ls")
    assert out.success is False
    assert "SkyPilot" in (out.error or "")
