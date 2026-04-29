"""Tests for ComputeTool's hint-vs-explicit-value resolution (M1A bug fix).

The M0 code conflated "caller didn't specify" with "caller specified the
default-shaped value":

    if cpus == 2:                               # silently true if you ASKED for 2
        cpus = max(cpus, hints.get("min_cpus", 2))

That clobbers a legitimate ``compute_run(service="openfoam", cpus=2)`` —
limiting agent autonomy in exactly the way the registry isn't supposed to.
M1A switches to ``is None`` so explicit values win, including default-shaped
ones.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.tools.atomic import compute as compute_mod
from sciagent.tools.atomic.compute import ComputeTool


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    compute_mod._registry_cache.clear()
    yield
    compute_mod._registry_cache.clear()


def _patched_registry(registry: dict):
    return patch.object(
        compute_mod, "_load_service_registry", return_value=registry
    )


def _stub_router_returning(name="sciagent-job1"):
    """Build a fake router whose backend records the job spec it received."""
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.run.return_value = (name, 1)
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (fake_skypilot, "Using requested backend: skypilot")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    return fake_router, fake_skypilot


_OPENFOAM_REGISTRY = {
    "defaults": {
        "resources": {
            "min_memory_gb": 4,
            "recommended_memory_gb": 8,
            "min_cpus": 2,
            "gpu_beneficial": False,
            "gpu_required": False,
        }
    },
    "services": {
        "openfoam": {
            "extends": None,
            "resources": {
                "min_memory_gb": 8,
                "recommended_memory_gb": 32,
                "min_cpus": 4,
            },
        },
        "openfoam-leaf": {"extends": "openfoam"},
    },
}


def test_explicit_cpus_2_is_not_clobbered_by_registry_hint():
    """``compute_run(service="openfoam", cpus=2, memory_gb=4)`` honors the
    explicit values verbatim — the M0 bug auto-promoted these to the
    registry hints because 2 and 4 happened to be the python-default
    sentinels."""
    fake_router, fake_skypilot = _stub_router_returning()
    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with _patched_registry(_OPENFOAM_REGISTRY), patch.object(
        tool, "_get_router", return_value=fake_router
    ):
        result = tool.execute(
            command="echo hi",
            service="openfoam",
            cpus=2,
            memory_gb=4,
            backend="skypilot",
        )

    assert result.success is True
    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.requirements.cpus == 2, (
        f"explicit cpus=2 must not be clobbered to registry's min_cpus=4; "
        f"got {job_arg.requirements.cpus}"
    )
    assert job_arg.requirements.memory_gb == 4, (
        f"explicit memory_gb=4 must not be clobbered to registry's "
        f"recommended_memory_gb=32; got {job_arg.requirements.memory_gb}"
    )


def test_omitted_cpus_resolves_via_registry_hint():
    """When the caller doesn't pass cpus, the registry's min_cpus wins."""
    fake_router, fake_skypilot = _stub_router_returning()
    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with _patched_registry(_OPENFOAM_REGISTRY), patch.object(
        tool, "_get_router", return_value=fake_router
    ):
        result = tool.execute(
            command="echo hi",
            service="openfoam",
            backend="skypilot",
        )

    assert result.success is True
    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.requirements.cpus == 4
    assert job_arg.requirements.memory_gb == 32


def test_omitted_cpus_inherits_through_extends_chain():
    """Leaf without resources block inherits parent's hint via the chain walk."""
    fake_router, fake_skypilot = _stub_router_returning()
    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with _patched_registry(_OPENFOAM_REGISTRY), patch.object(
        tool, "_get_router", return_value=fake_router
    ):
        result = tool.execute(
            command="echo hi",
            service="openfoam-leaf",
            backend="skypilot",
        )

    assert result.success is True
    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.requirements.cpus == 4  # inherited from openfoam
    assert job_arg.requirements.memory_gb == 32


def test_explicit_gpus_zero_disables_auto_promote():
    """Caller saying ``gpus=0`` explicitly must NOT be auto-promoted to 1
    even if the service declares gpu_required. The M0 bug treated 0 as the
    sentinel for "caller didn't specify"; M1A separates the two."""
    registry = {
        "defaults": {"resources": {"min_cpus": 2}},
        "services": {
            "needs-gpu": {
                "extends": None,
                "resources": {"gpu_required": True, "min_cpus": 4},
            }
        },
    }
    fake_router, fake_skypilot = _stub_router_returning()
    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with _patched_registry(registry), patch.object(
        tool, "_get_router", return_value=fake_router
    ):
        result = tool.execute(
            command="echo hi",
            service="needs-gpu",
            gpus=0,
            backend="skypilot",
        )

    assert result.success is True
    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.requirements.gpus == 0, (
        "explicit gpus=0 must override gpu_required auto-promote"
    )


def test_omitted_gpus_with_gpu_required_auto_promotes():
    """When the caller doesn't pass gpus, gpu_required services get 1 GPU."""
    registry = {
        "defaults": {"resources": {"min_cpus": 2}},
        "services": {
            "needs-gpu": {
                "extends": None,
                "resources": {"gpu_required": True, "min_cpus": 4},
            }
        },
    }
    fake_router, fake_skypilot = _stub_router_returning()
    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with _patched_registry(registry), patch.object(
        tool, "_get_router", return_value=fake_router
    ):
        result = tool.execute(
            command="echo hi",
            service="needs-gpu",
            backend="skypilot",
        )

    assert result.success is True
    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.requirements.gpus == 1


def test_image_only_call_uses_ultimate_defaults():
    """No service → no registry hints → ultimate defaults (cpus=2, memory_gb=4, gpus=0)."""
    registry = {
        "defaults": {"resources": {"min_cpus": 2, "recommended_memory_gb": 8}},
        "services": {},
    }
    fake_router, fake_skypilot = _stub_router_returning()
    fake_local = MagicMock()
    fake_local.name = "local"
    fake_local.run.return_value = "local-1"
    fake_router._backends = {"local": fake_local, "skypilot": fake_router._backends["skypilot"]}
    fake_router.list_backends.return_value = ["local", "skypilot"]
    fake_router.select.return_value = (fake_local, "Using local Docker")

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with _patched_registry(registry), patch.object(
        tool, "_get_router", return_value=fake_router
    ):
        result = tool.execute(
            command="echo hi",
            image="alpine",
            backend="local",
        )

    assert result.success is True
    job_arg = fake_local.run.call_args.args[0]
    assert job_arg.requirements.cpus == 2
    assert job_arg.requirements.memory_gb == 4
    assert job_arg.requirements.gpus == 0
