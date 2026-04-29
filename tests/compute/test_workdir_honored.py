"""Tests for M0 follow-up #1: registry's ``workdir:`` honored by the backend.

B8 #2 (sciagent-job-fe0e4e60) failed with ``bash: Allrun: No such file or
directory`` because Sky's managed jobs run from the cluster user's home
by default, ignoring the OpenFOAM image's ``WORKDIR /workspace``. The M0
workaround was to require every caller to prefix ``cd /workspace && ``.
M1A reads the registry's ``workdir:`` field (walking the extends chain)
and prepends the cd inside ``_build_task`` so the registry stops needing
per-caller workarounds.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import ComputeRequirements, Job
from sciagent.tools.atomic import compute as compute_mod
from sciagent.tools.atomic.compute import _get_service_workdir


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    compute_mod._registry_cache.clear()
    yield
    compute_mod._registry_cache.clear()


def _patched_registry(registry: dict):
    return patch.object(
        compute_mod, "_load_service_registry", return_value=registry
    )


# ---- _get_service_workdir chain walk -------------------------------------


def test_workdir_walks_extends_chain_leaf_wins():
    """A leaf without its own workdir: inherits from the nearest ancestor;
    when the leaf declares one explicitly, leaf wins."""
    registry = {
        "defaults": {},
        "services": {
            "root": {"extends": None, "workdir": "/workspace"},
            "mid": {"extends": "root"},  # inherits /workspace
            "leaf-override": {"extends": "mid", "workdir": "/data"},
        },
    }
    with _patched_registry(registry):
        assert _get_service_workdir("root") == "/workspace"
        assert _get_service_workdir("mid") == "/workspace"
        assert _get_service_workdir("leaf-override") == "/data"


def test_workdir_returns_none_when_no_chain_declares_one():
    """If nobody in the chain declares workdir, the helper returns None
    so the backend falls back to Sky's default CWD (M0 behavior)."""
    registry = {
        "defaults": {},
        "services": {
            "no-wd": {"extends": None},
        },
    }
    with _patched_registry(registry):
        assert _get_service_workdir("no-wd") is None
        assert _get_service_workdir("not-in-registry") is None


def test_workdir_chain_terminates_on_cycle():
    """Hand-edited registry safety: a cycle must not infinite-loop."""
    registry = {
        "defaults": {},
        "services": {
            "a": {"extends": "b"},
            "b": {"extends": "a", "workdir": "/x"},
        },
    }
    with _patched_registry(registry):
        # 'a' is visited first, then 'b' (which has the workdir).
        assert _get_service_workdir("a") == "/x"


def test_workdir_real_registry_openfoam_chain():
    """Integration: the on-disk registry's openfoam chain resolves to /workspace."""
    assert _get_service_workdir("openfoam-swak4foam-2012") == "/workspace"


# ---- _build_task prepends cd ---------------------------------------------


def _build_run_command(job: Job) -> str:
    """Drive _build_task's run-command synthesis without touching real Sky.

    sky.Task is mocked away; we just inspect the run= kwarg the backend
    passes in. That's the surface that decides whether ``cd <workdir> &&``
    appears.
    """
    backend = SkyPilotBackend()
    captured = {}

    class _FakeTask:
        def __init__(self, name=None, run=None):
            captured["name"] = name
            captured["run"] = run

        def set_resources(self, *_a, **_k):
            return self

        def set_storage_mounts(self, *_a, **_k):
            return self

    fake_sky = type("FS", (), {})()
    fake_sky.Task = _FakeTask
    # Resources is a no-op constructor; we don't inspect it here.
    fake_sky.Resources = lambda **kwargs: None
    backend._sky = fake_sky

    backend._build_task(job)
    return captured["run"]


def test_build_task_prepends_cd_when_container_workdir_set():
    job = Job(
        id="abc",
        service="openfoam",
        image="ghcr.io/sciagent-ai/openfoam",
        command="bash Allrun",
        container_workdir="/workspace",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash Allrun"


def test_build_task_does_not_double_prepend_when_caller_already_cd():
    """Idempotent against the M0 workaround: if the caller already starts
    with ``cd ...``, we trust them and don't prepend a second cd. Keeps
    legacy callers green during the migration."""
    job = Job(
        id="abc",
        command="cd /workspace && bash Allrun",
        container_workdir="/workspace",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash Allrun"


def test_build_task_no_prepend_when_container_workdir_unset():
    """Callers that don't set container_workdir (image-only calls, or
    services without workdir: in the registry) get the M0 behavior:
    Sky's default CWD."""
    job = Job(
        id="abc",
        command="echo hi",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run = _build_run_command(job)
    assert run == "echo hi"


def test_build_task_cd_lives_inside_timeout_wrapper():
    """The on-VM timeout wrapper must wrap the cd+command together, so the
    timeout applies to the whole pipeline (not just the cd, which is
    instant). Easy correctness check: the wrapped form contains the cd."""
    job = Job(
        id="abc",
        command="bash Allrun",
        container_workdir="/workspace",
        requirements=ComputeRequirements(timeout_sec=300),
    )
    run = _build_run_command(job)
    assert run.startswith("timeout 300 bash -c ")
    # The quoted inner string should contain the cd.
    assert "cd /workspace && bash Allrun" in run


def test_build_task_quotes_workdir_with_special_chars():
    """A pathological workdir with spaces must be shell-quoted so the cd
    survives. Defensive: hand-edited registries shouldn't crash sky."""
    job = Job(
        id="abc",
        command="ls",
        container_workdir="/path with spaces",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run = _build_run_command(job)
    assert run == "cd '/path with spaces' && ls"
