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
        def __init__(self, name=None, run=None, workdir=None):
            captured["name"] = name
            captured["run"] = run
            captured["workdir"] = workdir

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


def _mount(path: str = "/workspace", implicit: bool = False) -> StorageMount:
    return StorageMount(path=path, bucket="test-bucket", store="s3", implicit=implicit)


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


# ---- workdir= propagation (local CWD upload to cluster) ---------------


def _build_task_capturing(job: Job) -> dict:
    """Variant that returns the full captured kwargs dict so tests can
    assert on workdir= as well as run=."""
    backend = SkyPilotBackend()
    captured: dict = {}

    class _FakeTask:
        def __init__(self, name=None, run=None, workdir=None):
            captured["name"] = name
            captured["run"] = run
            captured["workdir"] = workdir

        def set_resources(self, *_a, **_k):
            return self

        def set_storage_mounts(self, *_a, **_k):
            return self

    fake_sky = type("FS", (), {})()
    fake_sky.Task = _FakeTask
    fake_sky.Resources = lambda **kwargs: None
    fake_sky.StorageMode = StorageMode
    fake_sky.Storage = lambda **kwargs: None
    fake_sky.StoreType = type("ST", (), {"S3": "s3"})
    backend._sky = fake_sky

    backend._build_task(job)
    return captured


def test_workdir_kwarg_set_to_job_working_dir():
    """SkyPilot's workdir= rsyncs the local CWD to the cluster. Without
    this, scripts the user just wrote locally are invisible on the
    cluster — observed in real transcripts to force the agent into inline
    `python -c` workarounds."""
    job = Job(
        id="abc",
        image="python:3.11",
        command="python hello.py",
        working_dir="/tmp/my-project",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    captured = _build_task_capturing(job)
    assert captured["workdir"] == "/tmp/my-project"


def test_workdir_kwarg_coexists_with_storage_mount():
    """workdir= and storage mounts are independent: workdir uploads the
    local CWD ephemerally; mounts attach a persistent bucket. Both
    coexist, and the mount-driven cd= still wins for run-CWD."""
    job = Job(
        id="abc",
        service="openfoam",
        command="bash Allrun",
        working_dir="/tmp/my-project",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace")],
        ),
    )
    captured = _build_task_capturing(job)
    assert captured["workdir"] == "/tmp/my-project"
    assert captured["run"] == "cd /workspace && bash Allrun"


# ---- implicit mount: don't cd, symlink _outputs/ instead --------------


def test_implicit_mount_does_not_cd_and_injects_symlink_prologue():
    """The ad-hoc case: workspace=None default, sciagent auto-attached a
    bucket so outputs persist, but the user's script lives in workdir
    (~/sky_workdir/). cd-into-mount would defeat that — script not at
    /workspace. Instead the prologue links _outputs/ through to a
    per-job prefix in the bucket so (a) relative writes from the script
    still persist and (b) parallel jobs in the same session don't
    collide on /workspace/_outputs/."""
    job = Job(
        id="abc",
        image="python:3.11",
        command="python hello.py",
        working_dir="/tmp/my-project",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace", implicit=True)],
        ),
    )
    run = _build_run_command(job)
    assert "cd /workspace" not in run
    # Per-job prefix: /workspace/_outputs/<job_id>/, not flat _outputs/.
    assert "mkdir -p /workspace/_outputs/abc" in run
    assert "ln -sfn /workspace/_outputs/abc ./_outputs" in run
    assert run.endswith("python hello.py")


def test_explicit_mount_still_cd_no_symlink():
    """Sanity check: when the caller explicitly mounts (registry service,
    workspace_source=, or workspace=True), implicit=False — keep the old
    cd-into-mount behavior. The B8 openfoam case must not regress."""
    job = Job(
        id="abc",
        service="openfoam",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace", implicit=False)],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /workspace && bash Allrun"
    assert "ln -sfn" not in run


def test_implicit_mount_with_timeout_wraps_after_symlink():
    """Timeout wrapper must enclose the whole prologue+command, otherwise
    it'd time out only the user's command and the symlink setup would
    survive a timeout cancellation. Pin the order."""
    job = Job(
        id="abc",
        image="python:3.11",
        command="python hello.py",
        requirements=ComputeRequirements(
            timeout_sec=120,
            storage=[_mount("/workspace", implicit=True)],
        ),
    )
    run = _build_run_command(job)
    assert run.startswith("timeout 120 bash -c ")
    # The single-quoted blob inside should contain the per-job-prefixed prologue.
    assert "mkdir -p /workspace/_outputs/abc" in run
    assert "ln -sfn /workspace/_outputs/abc ./_outputs" in run
    assert "python hello.py" in run


def test_implicit_mount_skipped_when_caller_pre_cd():
    """If the user's command already starts with cd, we don't second-guess
    them — neither the cd-prepend nor the symlink prologue applies. This
    keeps the M0 idempotency contract."""
    job = Job(
        id="abc",
        image="python:3.11",
        command="cd /tmp && python hello.py",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace", implicit=True)],
        ),
    )
    run = _build_run_command(job)
    assert run == "cd /tmp && python hello.py"


def test_implicit_mount_parallel_jobs_get_distinct_prefixes():
    """The load-bearing property of per-job prefix: two parallel jobs
    sharing the same session bucket must symlink to *distinct* targets,
    so a parallel parameter sweep of N OpenFOAM runs doesn't collide on
    /workspace/_outputs/. Each job's job_id is unique by construction —
    pin that the symlink target uses it."""
    job_a = Job(
        id="sweep-001",
        image="python:3.11",
        command="python solve.py --re=100",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace", implicit=True)],
        ),
    )
    job_b = Job(
        id="sweep-002",
        image="python:3.11",
        command="python solve.py --re=200",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_mount("/workspace", implicit=True)],
        ),
    )
    run_a = _build_run_command(job_a)
    run_b = _build_run_command(job_b)

    assert "ln -sfn /workspace/_outputs/sweep-001 ./_outputs" in run_a
    assert "ln -sfn /workspace/_outputs/sweep-002 ./_outputs" in run_b
    # Cross-check: each job's prologue references *only* its own job_id.
    assert "sweep-002" not in run_a
    assert "sweep-001" not in run_b
