"""When sky.launch is rejected, the request_id and next_step hint must
be in the structured output so the agent can run `sky api logs <id>` via
bash to discover the real cause.

Symptom we're guarding against (from real traces):

    → compute_run(...)
    ✗ Error: sky.launch rejected: sky.launch failed for cluster
              sciagent-job-ee4d1c9f (request_id=242889e1-4e5...

The request_id is truncated by the agent's display, the auto log-tail
fetch came back empty, and the agent has nothing actionable. This test
pins the structured-output fields so a future refactor can't silently
break the audit trail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sciagent.compute.job import LaunchError


def test_launch_error_carries_request_id():
    """The exception itself must carry request_id so callers can surface it
    structurally without parsing the message string."""
    exc = LaunchError(
        "sky.launch failed for cluster x",
        cluster_name="x",
        request_id="242889e1-4e57-4afe-9a0a-f7e0f57b4abf",
    )
    assert exc.request_id == "242889e1-4e57-4afe-9a0a-f7e0f57b4abf"
    assert exc.cluster_name == "x"


def test_compute_run_rejected_output_includes_request_id_and_next_step():
    """compute_run must put request_id + next_step in the structured output
    so the agent gets actionable info even when the error string is
    truncated by the display."""
    from sciagent.tools.atomic.compute import ComputeTool

    tool = ComputeTool(working_dir=".")

    fake_router = MagicMock()
    fake_backend = MagicMock()
    fake_backend.name = "skypilot"
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    fake_backend.run.side_effect = LaunchError(
        "sky.launch failed for cluster sciagent-job-abc",
        cluster_name="sciagent-job-abc",
        request_id="242889e1-4e57-4afe-9a0a-f7e0f57b4abf",
    )
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_backend}
    fake_router.select.return_value = (fake_backend, "test")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}
    tool._router = fake_router

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
    )

    assert out.success is False
    assert out.output["failure_type"] == "launch_rejected"
    assert out.output["request_id"] == "242889e1-4e57-4afe-9a0a-f7e0f57b4abf"
    # next_step must explicitly tell the agent to run the bash command.
    assert "sky api logs" in out.output["next_step"]
    assert "242889e1-4e57-4afe-9a0a-f7e0f57b4abf" in out.output["next_step"]


def test_compute_run_cluster_mode_rejected_includes_request_id():
    """Same guarantee for mode='cluster' — the cluster-mode rejection path
    is a separate code path inside compute_run.execute and must carry
    request_id too."""
    from sciagent.tools.atomic.compute import ComputeTool

    tool = ComputeTool(working_dir=".")

    fake_router = MagicMock()
    fake_backend = MagicMock()
    fake_backend.name = "skypilot"
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_backend}
    fake_router.select.return_value = (fake_backend, "test")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}
    fake_router.launch_cluster.side_effect = LaunchError(
        "sky.launch failed for cluster sciagent-c1",
        cluster_name="sciagent-c1",
        request_id="rid-cluster-mode",
    )
    tool._router = fake_router

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
        mode="cluster",
        cluster_name="sciagent-c1",
    )

    assert out.success is False
    assert out.output["failure_type"] == "launch_rejected"
    assert out.output["request_id"] == "rid-cluster-mode"
    assert "rid-cluster-mode" in out.output["next_step"]


def test_no_request_id_falls_back_to_sky_check_hint():
    """If sky.launch failed before producing a request_id (e.g., creds
    error during preflight), next_step should still give the agent
    something actionable — pointing at `sky check`."""
    from sciagent.tools.atomic.compute import ComputeTool

    tool = ComputeTool(working_dir=".")

    fake_router = MagicMock()
    fake_backend = MagicMock()
    fake_backend.name = "skypilot"
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    fake_backend.run.side_effect = LaunchError(
        "no credentials",
        cluster_name="x",
        request_id=None,
    )
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_backend}
    fake_router.select.return_value = (fake_backend, "test")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}
    tool._router = fake_router

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
    )

    assert out.success is False
    assert out.output["request_id"] is None
    assert "sky check" in out.output["next_step"]
