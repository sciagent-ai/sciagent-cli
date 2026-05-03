"""When the agent runs a probe-shaped command via mode='job' on skypilot,
compute_run must surface a mode_hint pointing at mode='cluster'.

Symptom from real traces: agent does a sequence of probes (echo "...",
which buoyantBoussinesqPimpleFoam, ls -la, etc.), each via compute_run
with the default mode='job'. Each one spins up a fresh 3-5 min cluster.
20 probes = 60+ minutes of provisioning that mode='cluster' + compute_exec
would have done in seconds.

The hint is visibility-only (no auto-switch — respects the "expose Sky
idioms instead of hiding them" preference), but it has to be loud enough
that the agent reaches for cluster mode on the next iteration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sciagent.tools.atomic.compute import (
    ComputeTool,
    _looks_like_probe,
)


# ---- _looks_like_probe heuristic ------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        ("echo hi", True),
        ('echo "=== Testing OpenFOAM ==="', True),
        ("which buoyantBoussinesqPimpleFoam", True),
        ("ls -la /workspace", True),
        ("pwd && ls", True),  # head before && is just `pwd`
        ("env | grep FOAM", True),  # head before | is just `env`
        ("find /opt -name bashrc", True),
        ("printenv", True),
        # Real workloads — must NOT trip the heuristic.
        ("bash Allrun", False),
        ("python solver.py --out $OUTPUTS_DIR/result.json", False),
        ("buoyantBoussinesqPimpleFoam -parallel", False),
        ("blockMesh && snappyHexMesh -overwrite", False),
        ("pip install -q numpy && python script.py", False),
        ("", False),  # empty is not a probe
        # Long command — even if it starts with echo, treat as workload.
        ("echo " + "x" * 500, False),
    ],
)
def test_looks_like_probe_classification(command, expected):
    assert _looks_like_probe(command) is expected


# ---- mode_hint surfacing in compute_run -----------------------------


def _make_tool_with_skypilot():
    tool = ComputeTool(working_dir=".")
    fake_router = MagicMock()
    fake_backend = MagicMock()
    fake_backend.name = "skypilot"
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    fake_backend.run.return_value = ("sciagent-job-x", 1)
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_backend}
    fake_router.select.return_value = (fake_backend, "test")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}
    tool._router = fake_router
    return tool


def test_probe_command_in_mode_job_emits_mode_hint():
    """The exact regression: agent runs `echo "..." && ...` via default
    mode='job'. compute_run must surface a hint pointing at mode='cluster'."""
    tool = _make_tool_with_skypilot()
    out = tool.execute(
        command='echo "=== Testing OpenFOAM ==="',
        image="python:3.11",
        backend="skypilot",
    )
    assert out.success is True
    assert "mode_hint" in out.output
    assert "cluster" in out.output["mode_hint"].lower()
    assert "compute_exec" in out.output["mode_hint"]


def test_real_workload_in_mode_job_emits_no_mode_hint():
    """A real solver run via mode='job' is a legitimate use of managed-jobs
    (one-shot batch). Don't pollute its result with an irrelevant nudge."""
    tool = _make_tool_with_skypilot()
    out = tool.execute(
        command="bash Allrun && cp -r postProcessing $OUTPUTS_DIR/",
        image="python:3.11",
        backend="skypilot",
    )
    assert out.success is True
    assert "mode_hint" not in out.output


def test_probe_in_mode_cluster_emits_no_mode_hint():
    """When the agent already chose mode='cluster', the hint is moot —
    it's already on the warm-cluster path. Don't spam it."""
    tool = _make_tool_with_skypilot()
    tool._router.launch_cluster.return_value = ("c1", 1)
    out = tool.execute(
        command='echo "warm probe"',
        image="python:3.11",
        backend="skypilot",
        mode="cluster",
        cluster_name="c1",
    )
    assert out.success is True
    assert "mode_hint" not in out.output


def test_probe_on_local_backend_emits_no_mode_hint():
    """Local Docker has no cluster mode equivalent. The hint would be
    actionably wrong on local — skip it."""
    tool = ComputeTool(working_dir=".")
    fake_router = MagicMock()
    fake_backend = MagicMock()
    fake_backend.name = "local"
    fake_backend.build_outputs_mount.return_value = None
    fake_backend.build_input_mounts.return_value = []
    fake_backend.run.return_value = ("local-job-x",)
    fake_router.list_backends.return_value = ["local"]
    fake_router._backends = {"local": fake_backend}
    fake_router.select.return_value = (fake_backend, "test")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    tool._router = fake_router

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="local",
    )
    assert "mode_hint" not in out.output
