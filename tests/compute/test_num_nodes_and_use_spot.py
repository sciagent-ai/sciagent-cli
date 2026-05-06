"""Sky-native pass-through for num_nodes and use_spot.

Verifies the slim-P0 contract that compute_run plumbs these straight to
Sky's Task / Resources without re-naming or wrapping. The unit tests stay
mock-only — a real Sky launch is the smoke-test territory in the manual
section of the plan.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.tools.atomic.compute import ComputeTool
from sciagent.compute.job import Job, ComputeRequirements


def _stub_router():
    from sciagent.compute.job import StorageMount, StorageMode

    fake_router = MagicMock()
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.run.return_value = ("sciagent-job-1", 1)
    fake_skypilot.build_outputs_mount.return_value = None
    fake_skypilot.build_input_mounts.return_value = []
    # P0.5 auto-mount: stub returns a real StorageMount.
    fake_skypilot.build_session_workspace_mount.return_value = StorageMount(
        path="/workspace",
        bucket="sciagent-workspace-test",
        store="s3",
        mode=StorageMode.MOUNT,
        source=None,
        persistent=True,
        kind="durable",
    )
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router.select.return_value = (fake_skypilot, "test routing")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.05, "estimated_total": 0.05}
    return fake_router, fake_skypilot


def test_num_nodes_default_is_1():
    fake_router, fake_sky = _stub_router()
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    out = tool.execute(command="echo hi", image="python:3.11", backend="skypilot")
    assert out.success is True
    job_arg = fake_sky.run.call_args.args[0]
    assert isinstance(job_arg, Job)
    assert job_arg.requirements.num_nodes == 1
    assert job_arg.requirements.use_spot is False


def test_num_nodes_passed_through():
    fake_router, fake_sky = _stub_router()
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    out = tool.execute(
        command="echo hi", image="python:3.11", backend="skypilot",
        num_nodes=4,
    )
    assert out.success is True
    job_arg = fake_sky.run.call_args.args[0]
    assert job_arg.requirements.num_nodes == 4


def test_use_spot_passed_through():
    fake_router, fake_sky = _stub_router()
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    out = tool.execute(
        command="echo hi", image="python:3.11", backend="skypilot",
        use_spot=True,
    )
    assert out.success is True
    job_arg = fake_sky.run.call_args.args[0]
    assert job_arg.requirements.use_spot is True


def test_compute_requirements_dataclass_carries_fields():
    """Defensive: ComputeRequirements must expose num_nodes / use_spot so the
    SkyPilot backend can read them in _build_task. A typo or accidental
    dataclass regression here cascades silently — single-node / on-demand
    launches when the caller asked for spot multi-node."""
    req = ComputeRequirements(num_nodes=8, use_spot=True)
    assert req.num_nodes == 8
    assert req.use_spot is True
    # Defaults remain backward-compat for every existing caller.
    default = ComputeRequirements()
    assert default.num_nodes == 1
    assert default.use_spot is False
