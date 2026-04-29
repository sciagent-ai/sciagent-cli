"""Tests for M0 follow-up #1: cd into the workspace mount before running.

B8 #2 (sciagent-job-fe0e4e60) failed with ``bash: Allrun: No such file or
directory`` because Sky's managed jobs run from the cluster user's home
by default, ignoring the image's ``WORKDIR /workspace`` even when data
is mounted there. The M0 workaround required every caller to prefix
``cd /workspace && `` themselves.

M1A drives the cd off the **actual storage-mount path**, NOT the registry's
``workdir:`` hint. Driving off the mount is correct because:

  - the registry's hint and the mount path can drift (registry says
    /workspace; a future caller mounts at /data) — only the mount path is
    guaranteed to point at user data;
  - image-only callers (no service in the registry) with workspace_source=
    also get the cd-prepend, which a registry-driven approach would miss;
  - service-only callers without a mount keep Sky's default CWD, so
    images whose Dockerfile WORKDIR isn't /workspace (e.g. rcwa: /opt)
    don't regress with a phantom ``cd /workspace`` that fails.
"""

from __future__ import annotations

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import (
    ComputeRequirements,
    Job,
    StorageMode,
    StorageMount,
)


def _build_run_command(job: Job) -> str:
    """Drive _build_task's run-command synthesis without touching real Sky.

    sky.Task is mocked away; we just inspect the run= kwarg the backend
    passes in. That's the surface that decides whether a ``cd <mount> &&``
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
    fake_sky.Resources = lambda **kwargs: None
    fake_sky.StorageMode = StorageMode  # _build_storage_mounts dereferences this
    fake_sky.Storage = lambda **kwargs: None
    fake_sky.StoreType = type("ST", (), {"S3": "s3"})
    backend._sky = fake_sky

    backend._build_task(job)
    return captured["run"]


def _mount(path: str = "/workspace") -> StorageMount:
    return StorageMount(path=path, bucket="test-bucket", store="s3")


# ---- mount-attached: cd is prepended ----------------------------------


def test_workspace_mount_at_workspace_triggers_cd():
    """The B8 case: openfoam-style mount at /workspace, bare bash Allrun."""
    job = Job(
        id="abc",
        service="openfoam",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash Allrun"


def test_image_only_call_with_mount_also_gets_cd():
    """Image-only caller with workspace_source= attaches a mount the same
    way; cd must apply. The registry-driven version of this fix would
    have missed this case (no service -> no registry lookup -> no cd)."""
    job = Job(
        id="abc",
        image="custom:tag",  # no service
        command="bash run.sh",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash run.sh"


def test_non_workspace_mount_path_drives_cd():
    """A future mount at /data must cd into /data, not /workspace.
    This is the drift the registry-hint approach couldn't handle."""
    job = Job(
        id="abc",
        service="custom-service",
        command="python analyze.py",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/data")],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /data && python analyze.py"


# ---- no mount: M0 default CWD preserved -------------------------------


def test_service_call_without_mount_does_not_cd():
    """compute_run(service="scipy-base", command="python -c '...'") with
    no workspace_source must not get a phantom cd. Sky's default CWD
    works for inline commands; rcwa-style images whose WORKDIR isn't
    /workspace don't regress to a failing ``cd /workspace``."""
    job = Job(
        id="abc",
        service="scipy-base",
        command="python -c 'print(1+1)'",
        requirements=ComputeRequirements(timeout_sec=0),  # no storage
    )
    run = _build_run_command(job)
    assert run == "python -c 'print(1+1)'"


def test_image_only_call_without_mount_does_not_cd():
    job = Job(
        id="abc",
        image="alpine",
        command="echo hi",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run = _build_run_command(job)
    assert run == "echo hi"


# ---- idempotency + edge cases ----------------------------------------


def test_caller_already_cd_prefixed_is_not_double_prepended():
    """Idempotent against the M0 workaround. Legacy callers (B8 historically,
    user code that hand-wrote ``cd /workspace && bash Allrun``) keep working
    without a doubled cd."""
    job = Job(
        id="abc",
        command="cd /workspace && bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash Allrun"


def test_cd_lives_inside_timeout_wrapper():
    """The on-VM timeout must wrap the whole pipeline (cd + command), so the
    timeout applies to the user's command and not just the (instant) cd."""
    job = Job(
        id="abc",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=300,
            storage=[_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run.startswith("timeout 300 bash -c ")
    assert "cd /workspace && bash Allrun" in run


def test_mount_path_with_special_chars_is_shell_quoted():
    """Defensive: a hand-edited mount path with spaces must be shlex-quoted
    so the cd survives without crashing the shell."""
    job = Job(
        id="abc",
        command="ls",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/path with spaces")],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd '/path with spaces' && ls"


def test_first_mount_wins_when_multiple_attached():
    """If a future caller attaches multiple mounts, the first one in the
    list determines the cd target. Today only workspace mounts are attached
    so this is forward-compat insurance, not a contract callers should
    rely on — pin in case M2A adds secondary mounts."""
    job = Job(
        id="abc",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[
                _mount("/workspace"),
                _mount("/aux"),
            ],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash Allrun"
