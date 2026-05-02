"""Tests for the layered prologue contract in resolve_command.

The compute layer's CWD policy (image-agnostic, identical across every
registry image):

  1. The always-on output prologue runs first:
     ``mkdir -p /outputs/<job_id> && export OUTPUTS_DIR=/outputs/<job_id>``.
     Always present. The user command writes results to ``$OUTPUTS_DIR``;
     bg_wait auto-fetches them on terminal status.

  2. ``cd <primary input mount>`` is added ONLY when the caller declared
     an input mount (workspace_source=). The conventional /workspace path
     is the primary if present; else the first input mount in declaration
     order; else no cd is added at all.

  3. The image's WORKDIR is honored when no input mount is declared and no
     ship_workdir= rsync target is set — sciagent never invents a CWD.

  4. ``ship_workdir`` (Job field) drives sky.Task(workdir=). When unset
     (default), no rsync, no SkyPilot CWD-override.

This consolidates the prior ``test_workdir_honored.py`` (which was pinned
on the legacy single-mount cd-prepend) onto the new contract.
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


def _input_mount(path: str = "/workspace") -> StorageMount:
    return StorageMount(path=path, bucket="b", store="s3", kind="input")


def _output_mount() -> StorageMount:
    return StorageMount(path="/outputs", bucket="ob", store="s3", kind="output")


def _build_run_command(job: Job) -> str:
    """Drive _build_task's run-command synthesis without touching real Sky."""
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
    fake_sky.StoreType = type("ST", (), {"S3": "s3", "GCS": "gcs", "AZURE": "azure"})
    backend._sky = fake_sky

    backend._build_task(job)
    return captured["run"]


def _build_task_capturing(job: Job) -> dict:
    """Return the full kwargs dict so tests can assert on workdir= too."""
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


# ---- always-on outputs prologue ---------------------------------------


def test_outputs_prologue_present_with_no_mount():
    """Even when the caller declares no mounts, mkdir + export OUTPUTS_DIR
    are always there. The image's WORKDIR is honored — no cd injected."""
    job = Job(
        id="abc",
        service="scipy-base",
        command="python -c 'print(1+1)'",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run = _build_run_command(job)
    assert run == (
        "mkdir -p /outputs/abc && export OUTPUTS_DIR=/outputs/abc && "
        "python -c 'print(1+1)'"
    )
    # Critical: no phantom cd. rcwa-style (image WORKDIR=/opt) must not regress.
    assert "cd " not in run.split(" && ")[2]


def test_outputs_prologue_with_only_output_mount_attached():
    """The auto-attached output mount must NOT trigger a cd — it's an
    output sink, not an input."""
    job = Job(
        id="abc",
        service="scipy-base",
        command="python solve.py",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_output_mount()],
        ),
    )
    run = _build_run_command(job)
    assert run == (
        "mkdir -p /outputs/abc && export OUTPUTS_DIR=/outputs/abc && "
        "python solve.py"
    )


# ---- input mount drives cd --------------------------------------------


def test_workspace_input_mount_triggers_cd():
    """Single input mount at /workspace: cd into it after the outputs
    prologue. Mirrors the B8 OpenFOAM case."""
    job = Job(
        id="abc",
        service="openfoam",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_output_mount(), _input_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run == (
        "mkdir -p /outputs/abc && export OUTPUTS_DIR=/outputs/abc && "
        "cd /workspace && bash Allrun"
    )


def test_non_workspace_input_path_drives_cd():
    """When the primary input is mounted at /data/run/, cd targets that."""
    job = Job(
        id="abc",
        service="custom-service",
        command="python analyze.py",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_output_mount(), _input_mount("/data/run")],
        ),
    )
    run = _build_run_command(job)
    assert "cd /data/run" in run
    assert "cd /workspace" not in run


def test_workspace_wins_when_multiple_input_mounts_declared():
    """Multi-mount inputs: /workspace (when present) is the primary cd
    target. Other mounts are reachable by absolute path inside the
    user's command."""
    job = Job(
        id="abc",
        command="blastn -query query.fa -db /data/nr/nr",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[
                _output_mount(),
                _input_mount("/data/nr"),
                _input_mount("/workspace"),
            ],
        ),
    )
    run = _build_run_command(job)
    assert "cd /workspace && blastn" in run


def test_first_input_wins_when_no_workspace_path():
    """When no input mount uses the conventional /workspace path, the
    first input mount in declaration order becomes the cd target."""
    job = Job(
        id="abc",
        command="python run.py",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[
                _output_mount(),
                _input_mount("/data/aux"),
                _input_mount("/data/main"),
            ],
        ),
    )
    run = _build_run_command(job)
    assert "cd /data/aux" in run
    assert "cd /data/main" not in run


def test_input_mount_path_with_special_chars_is_shell_quoted():
    job = Job(
        id="abc",
        command="ls",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_input_mount("/path with spaces")],
        ),
    )
    run = _build_run_command(job)
    assert "cd '/path with spaces' && ls" in run


# ---- idempotency + edge cases -----------------------------------------


def test_caller_already_cd_prefixed_skips_cd_injection():
    """If the user's command starts with cd, sciagent doesn't second-guess
    it. The outputs prologue still runs (export $OUTPUTS_DIR is a
    prerequisite for outputs)."""
    job = Job(
        id="abc",
        command="cd /workspace && bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_input_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run == (
        "mkdir -p /outputs/abc && export OUTPUTS_DIR=/outputs/abc && "
        "cd /workspace && bash Allrun"
    )
    # Not doubled.
    assert run.count("cd /workspace") == 1


def test_timeout_wraps_full_pipeline():
    """The on-VM timeout must enclose the whole prologue + cd + command."""
    job = Job(
        id="abc",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=300,
            storage=[_input_mount("/workspace")],
        ),
    )
    run = _build_run_command(job)
    assert run.startswith("timeout 300 bash -c ")
    assert "mkdir -p /outputs/abc" in run
    assert "cd /workspace" in run
    assert "bash Allrun" in run


def test_parallel_jobs_get_distinct_outputs_prefixes():
    """Per-job isolation: each job's outputs go to /outputs/<job_id>/. Two
    parallel sweep jobs in the same session never collide."""
    job_a = Job(
        id="sweep-001",
        command="python solve.py --re=100",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    job_b = Job(
        id="sweep-002",
        command="python solve.py --re=200",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    run_a = _build_run_command(job_a)
    run_b = _build_run_command(job_b)
    assert "mkdir -p /outputs/sweep-001" in run_a
    assert "mkdir -p /outputs/sweep-002" in run_b
    # Cross-check: each job's prologue references only its own job_id.
    assert "sweep-002" not in run_a
    assert "sweep-001" not in run_b


# ---- conditional workdir= (rsync of local code) ------------------------


def test_no_ship_workdir_means_no_rsync():
    """Default behavior: no rsync. SkyPilot's workdir= is omitted entirely
    so the image's WORKDIR is honored. ship_workdir defaults to None."""
    job = Job(
        id="abc",
        image="python:3.11",
        command="python -c 'print(1)'",
        working_dir="/tmp/my-project",  # local-side bookkeeping; NOT shipped
        requirements=ComputeRequirements(timeout_sec=0),
    )
    captured = _build_task_capturing(job)
    assert captured.get("workdir") is None


def test_explicit_ship_workdir_is_passed_to_sky_task():
    """When the caller asks to ship local code via ship_workdir=, that
    propagates to sky.Task(workdir=)."""
    job = Job(
        id="abc",
        image="python:3.11",
        command="python hello.py",
        ship_workdir="/tmp/my-project",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    captured = _build_task_capturing(job)
    assert captured["workdir"] == "/tmp/my-project"


def test_ship_workdir_coexists_with_input_mount():
    """ship_workdir= and an input mount are independent: rsync ships local
    code; mount attaches a bucket. Both can be active. The mount-driven cd
    still wins for run-CWD."""
    job = Job(
        id="abc",
        service="openfoam",
        command="bash Allrun",
        ship_workdir="/tmp/my-project",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[_output_mount(), _input_mount("/workspace")],
        ),
    )
    captured = _build_task_capturing(job)
    assert captured["workdir"] == "/tmp/my-project"
    assert "cd /workspace" in captured["run"]


# ---- canonical job-id alignment (regression for the bucket-prefix bug) ---


def test_run_canonicalizes_job_id_to_cluster_name():
    """Regression: the prologue's per-job key MUST match the cluster name
    that flows into the manifest's outputs_uri and the auto-fetch prefix.
    Before the fix, prologue used raw `job.id` (e.g., "job-abc") while the
    manifest used the cluster name (e.g., "sciagent-job-abc"), so user
    writes landed at bucket prefix /job-abc/ but the fetcher looked at
    /sciagent-job-abc/ and silently returned 0 files. SkyPilotBackend.run
    must canonicalize job.id at the launch boundary so both sides agree.
    """
    from unittest.mock import MagicMock

    backend = SkyPilotBackend()
    fake_sky = MagicMock()
    fake_sky.jobs.launch.return_value = "fake-request-id"
    fake_sky.api_status.return_value = []  # budget elapses cleanly
    fake_sky.StorageMode = StorageMode
    fake_sky.StoreType = type("ST", (), {"S3": "s3"})
    backend._sky = fake_sky

    job = Job(
        id="job-abc12345",
        command="echo hi > $OUTPUTS_DIR/proof.txt",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    name, _ = backend.run(job, background=True)

    # Cluster name returned matches the canonical id stored on the Job.
    assert name == "sciagent-job-abc12345"
    assert job.id == "sciagent-job-abc12345"
    # The Task that was launched carried the canonical id as its name AND
    # its run-command's prologue references the SAME id.
    task_kwargs = fake_sky.Task.call_args.kwargs
    assert task_kwargs["name"] == "sciagent-job-abc12345"
    assert "/outputs/sciagent-job-abc12345" in task_kwargs["run"]
    assert "OUTPUTS_DIR=/outputs/sciagent-job-abc12345" in task_kwargs["run"]


def test_run_does_not_double_prefix_already_canonicalized_id():
    """If something upstream already set job.id to a sciagent-prefixed
    string, run() must not turn it into sciagent-sciagent-..."""
    from unittest.mock import MagicMock

    backend = SkyPilotBackend()
    fake_sky = MagicMock()
    fake_sky.jobs.launch.return_value = "fake-request-id"
    fake_sky.api_status.return_value = []
    fake_sky.StorageMode = StorageMode
    fake_sky.StoreType = type("ST", (), {"S3": "s3"})
    backend._sky = fake_sky

    job = Job(
        id="sciagent-already-prefixed",
        command="echo hi",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    name, _ = backend.run(job, background=True)

    assert name == "sciagent-already-prefixed"  # no double prefix
    assert job.id == "sciagent-already-prefixed"
