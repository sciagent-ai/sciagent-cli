"""compute_cluster atomic tool — four actions on one tool.

Pins:
  - action validation (rejects unknown actions, requires cluster_name)
  - status / down / autostop / refresh_mounts each route to the right
    router method with the right kwargs
  - autostop requires idle_minutes
  - refresh_mounts requires command (passing service+image rejected)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sciagent.compute.job import LaunchError
from sciagent.tools.atomic.compute_cluster import ComputeClusterTool


def _tool_with_router():
    tool = ComputeClusterTool()
    tool._router = MagicMock()
    return tool


# ---- argument validation -------------------------------------------


def test_missing_action_returns_error():
    out = ComputeClusterTool().execute(cluster_name="x")
    assert out.success is False
    assert "action" in (out.error or "").lower()


def test_missing_cluster_name_returns_error():
    out = ComputeClusterTool().execute(action="status")
    assert out.success is False
    assert "cluster_name" in (out.error or "")


def test_unknown_action_returns_error():
    tool = _tool_with_router()
    out = tool.execute(action="restart", cluster_name="x")
    assert out.success is False
    assert "Unknown action" in (out.error or "")


def test_alias_cluster_kwarg_accepted():
    tool = _tool_with_router()
    tool._router.cluster_status.return_value = {"exists": True, "status": "UP"}
    out = tool.execute(action="status", cluster="x")
    assert out.success is True


# ---- status --------------------------------------------------------


def test_status_returns_router_response():
    tool = _tool_with_router()
    tool._router.cluster_status.return_value = {
        "cluster_name": "x",
        "exists": True,
        "status": "UP",
        "autostop": {"idle_minutes": 30, "down": False},
        "manifest": None,
    }
    out = tool.execute(action="status", cluster_name="x")
    assert out.success is True
    assert out.output["status"] == "UP"


# ---- down ----------------------------------------------------------


def test_down_passes_graceful_default():
    tool = _tool_with_router()
    tool._router.cluster_down.return_value = True
    out = tool.execute(action="down", cluster_name="x")
    assert out.success is True
    tool._router.cluster_down.assert_called_once_with("x", graceful=True)


def test_down_failure_surfaces_error():
    tool = _tool_with_router()
    tool._router.cluster_down.return_value = False
    out = tool.execute(action="down", cluster_name="x")
    assert out.success is False
    assert "sky.down failed" in (out.error or "")


# ---- autostop ------------------------------------------------------


def test_autostop_requires_idle_minutes():
    tool = _tool_with_router()
    out = tool.execute(action="autostop", cluster_name="x")
    assert out.success is False
    assert "idle_minutes" in (out.error or "")


def test_autostop_passes_kwargs_to_router():
    tool = _tool_with_router()
    tool._router.set_cluster_autostop.return_value = True
    out = tool.execute(
        action="autostop",
        cluster_name="x",
        idle_minutes=20,
        wait_for="jobs",
        hook="echo done",
    )
    assert out.success is True
    _, kwargs = tool._router.set_cluster_autostop.call_args
    assert kwargs["idle_minutes"] == 20
    assert kwargs["wait_for"] == "jobs"
    assert kwargs["hook"] == "echo done"


# ---- refresh_mounts ------------------------------------------------


def test_refresh_mounts_requires_command():
    tool = _tool_with_router()
    out = tool.execute(action="refresh_mounts", cluster_name="x")
    assert out.success is False
    assert "command" in (out.error or "")


def test_refresh_mounts_rejects_both_service_and_image():
    tool = _tool_with_router()
    out = tool.execute(
        action="refresh_mounts",
        cluster_name="x",
        command="bash Allrun",
        service="openfoam",
        image="python:3.11",
    )
    assert out.success is False
    assert "service" in (out.error or "").lower()


def test_refresh_mounts_calls_router_when_skypilot_available():
    """The wrapper must reach the router's refresh_cluster_mounts when
    SkyPilot is available. We mock the backend lookup to satisfy the
    storage-mount precondition."""
    tool = _tool_with_router()
    fake_backend = MagicMock()
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    tool._router._backends = {"skypilot": fake_backend}
    tool._router.refresh_cluster_mounts.return_value = ("x", 9)

    out = tool.execute(
        action="refresh_mounts",
        cluster_name="x",
        command="bash Allrun",
        service="openfoam",
    )
    assert out.success is True
    assert out.output["cluster_job_id"] == 9
    assert out.output["action"] == "refresh_mounts"


def test_refresh_mounts_launch_error_surfaces():
    tool = _tool_with_router()
    fake_backend = MagicMock()
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    tool._router._backends = {"skypilot": fake_backend}
    tool._router.refresh_cluster_mounts.side_effect = LaunchError(
        "no setup config", cluster_name="x"
    )

    out = tool.execute(
        action="refresh_mounts",
        cluster_name="x",
        command="bash Allrun",
        service="openfoam",
    )
    assert out.success is False
    assert "rejected" in (out.error or "").lower()
