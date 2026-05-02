"""Tests for SkyPilotBackend's M1B lifecycle emission.

Two events are wired into the backend:

  - compute_job_launched, after run() resolves a (name, managed_job_id),
    carrying command_original / command_resolved / mount_path / intent /
    expected_artifacts (m1a-followups #5).
  - compute_job_status_changed, on each get_status() call where the
    mapped sciagent JobStatus differs from the last value emitted in
    this process for that job_id.

Both emissions are best-effort; a log write failure (or a missing
session_id) must never break launch or status retrieval.

Sky is mocked away — these tests run unconditionally, no AWS credentials
or paid resources required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import (
    ComputeRequirements,
    Job,
    JobStatus,
    StorageMode,
    StorageMount,
)
from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


@pytest.fixture
def session_log(tmp_path: Path) -> ProvenanceLog:
    """Per-test isolated provenance log keyed under tmp_path. Returns a
    pre-primed singleton — subsequent get_provenance_log calls hit the
    same instance."""
    return get_provenance_log("testsess", base_dir=tmp_path)


def _read_events(log: ProvenanceLog) -> list[dict]:
    return [json.loads(line) for line in log.path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# resolve_command helper (extracted for emission)
# ---------------------------------------------------------------------------


def test_resolve_command_applies_cd_and_timeout():
    """Layered prologue: outputs always-on (mkdir + export), cd into the
    primary input mount when one is declared, then timeout-wrap."""
    job = Job(
        id="j1",
        command="bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=600,
            storage=[StorageMount(path="/workspace", bucket="b", store="s3", kind="input")],
        ),
    )
    resolved = SkyPilotBackend.resolve_command(job)
    assert resolved == (
        "timeout 600 bash -c 'mkdir -p /outputs/j1 && export OUTPUTS_DIR=/outputs/j1 "
        "&& cd /workspace && bash Allrun'"
    )


def test_resolve_command_passthrough_when_no_mount_no_timeout():
    """No mount, no timeout: only the always-on outputs prologue runs."""
    job = Job(
        id="j2",
        command="python -V",
        requirements=ComputeRequirements(timeout_sec=0),
    )
    assert SkyPilotBackend.resolve_command(job) == (
        "mkdir -p /outputs/j2 && export OUTPUTS_DIR=/outputs/j2 && python -V"
    )


def test_resolve_command_idempotent_on_caller_cd():
    """v4.2 §C5: idempotent against M0 cd-prefixed callers — sciagent's
    own cd is suppressed when the user command already starts with one.
    The always-on outputs prologue (mkdir + export) still applies."""
    job = Job(
        id="j3",
        command="cd /workspace && bash Allrun",
        requirements=ComputeRequirements(
            timeout_sec=0,
            storage=[StorageMount(path="/workspace", bucket="b", store="s3", kind="input")],
        ),
    )
    assert SkyPilotBackend.resolve_command(job) == (
        "mkdir -p /outputs/j3 && export OUTPUTS_DIR=/outputs/j3 && "
        "cd /workspace && bash Allrun"
    )


# ---------------------------------------------------------------------------
# compute_job_launched
# ---------------------------------------------------------------------------


def test_emit_launched_records_all_required_fields(session_log: ProvenanceLog):
    backend = SkyPilotBackend()
    job = Job(
        id="abc123",
        service="openfoam",
        image="ghcr.io/sciagent-ai/openfoam:latest",
        command="bash Allrun",
        requirements=ComputeRequirements(
            cpus=4, memory_gb=32, gpus=0, timeout_sec=3600,
            storage=[StorageMount(path="/workspace", bucket="b8-bucket", store="s3", kind="input")],
        ),
        session_id="testsess",
        intent={"paper": "doi:10.example/foo", "case": "typical_c"},
        expected_artifacts=["postProcessing/probes/0/U", "log.simpleFoam"],
    )
    backend._emit_launched_event(job, name="sciagent-abc123", managed_job_id=4231)

    events = _read_events(session_log)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_kind"] == "compute_job_launched"
    assert ev["job_id"] == "sciagent-abc123"
    assert ev["managed_job_id"] == 4231
    assert ev["backend"] == "skypilot"
    assert ev["service"] == "openfoam"
    assert ev["image"].startswith("ghcr.io/sciagent-ai/openfoam")
    assert ev["command_original"] == "bash Allrun"
    assert ev["command_resolved"] == (
        "timeout 3600 bash -c 'mkdir -p /outputs/abc123 && "
        "export OUTPUTS_DIR=/outputs/abc123 && cd /workspace && bash Allrun'"
    )
    # Primary input mount — the cd target — is what _emit_launched_event records.
    assert ev["mount_path"] == "/workspace"
    assert ev["mount_bucket"] == "b8-bucket"
    assert ev["requirements"]["cpus"] == 4
    assert ev["requirements"]["memory_gb"] == 32
    assert ev["requirements"]["timeout_sec"] == 3600
    assert ev["intent"] == {"paper": "doi:10.example/foo", "case": "typical_c"}
    assert ev["expected_artifacts"] == ["postProcessing/probes/0/U", "log.simpleFoam"]


def test_emit_launched_skipped_without_session_id(tmp_path: Path):
    """Standalone callers with no agent context must not pollute home dir."""
    backend = SkyPilotBackend()
    job = Job(
        id="abc",
        command="echo hi",
        requirements=ComputeRequirements(timeout_sec=0),
        session_id=None,
    )
    # Should be a no-op — no session, no log to write to.
    backend._emit_launched_event(job, name="sciagent-abc", managed_job_id=1)
    # Confirm nothing was created under the test's tmp dir
    assert not (tmp_path / "testsess").exists()


def test_emit_launched_with_no_mount(session_log: ProvenanceLog):
    backend = SkyPilotBackend()
    job = Job(
        id="j1",
        image="python:3.11",
        command="python -V",
        requirements=ComputeRequirements(timeout_sec=60),
        session_id="testsess",
    )
    backend._emit_launched_event(job, name="sciagent-j1", managed_job_id=None)

    ev = _read_events(session_log)[0]
    assert ev["mount_path"] is None
    assert ev["mount_bucket"] is None
    assert ev["managed_job_id"] is None
    assert ev["expected_artifacts"] == []


def test_emit_launched_intent_recorded_verbatim(session_log: ProvenanceLog):
    """v4.2 §C6: intent passes through verbatim — no shape, no validation."""
    backend = SkyPilotBackend()
    quirky = {"nested": {"deep": {"value": [1, 2, 3]}}, "tag": "x"}
    job = Job(
        id="j1",
        command="echo",
        requirements=ComputeRequirements(timeout_sec=0),
        session_id="testsess",
        intent=quirky,
    )
    backend._emit_launched_event(job, name="sciagent-j1", managed_job_id=None)
    ev = _read_events(session_log)[0]
    assert ev["intent"] == quirky


def test_emit_launched_failure_does_not_raise(session_log: ProvenanceLog):
    backend = SkyPilotBackend()
    job = Job(
        id="j1", command="echo",
        requirements=ComputeRequirements(timeout_sec=0),
        session_id="testsess",
    )
    with patch(
        "sciagent.compute.backends.skypilot.get_provenance_log",
        side_effect=RuntimeError("disk full"),
    ):
        # Must NOT raise — the cluster job is already running.
        backend._emit_launched_event(job, name="sciagent-j1", managed_job_id=1)


# ---------------------------------------------------------------------------
# compute_job_status_changed
# ---------------------------------------------------------------------------


def _stub_manifest(tmp_home: Path, job_id: str, session_id: str) -> None:
    """Plant a fake per-job manifest at ~/.sciagent/tasks/<job_id>.json."""
    tasks_dir = tmp_home / ".sciagent" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id,
        "session_id": session_id,
    }))


def test_emit_status_changed_resolves_session_via_manifest(monkeypatch, tmp_path: Path):
    """get_status() doesn't see session_id directly — it must look up the
    per-job manifest. Validates the bridge that connects bg_status polling
    to the per-session log."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _stub_manifest(tmp_path, "sciagent-zzz", "testsess")

    log = get_provenance_log("testsess", base_dir=tmp_path / "logs")

    # Patch the manifest reader the backend uses to point at our stub
    with patch(
        "sciagent.compute.backends.skypilot._read_task_manifest",
        return_value={"job_id": "sciagent-zzz", "session_id": "testsess"},
    ), patch(
        "sciagent.compute.backends.skypilot.get_provenance_log",
        side_effect=lambda sid: log,
    ):
        backend = SkyPilotBackend()
        backend._emit_status_changed_event(
            job_id="sciagent-zzz",
            managed_job_id=42,
            status=JobStatus.RUNNING,
            sky_status_raw="RUNNING",
        )

    events = _read_events(log)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_kind"] == "compute_job_status_changed"
    assert ev["job_id"] == "sciagent-zzz"
    assert ev["status"] == "running"
    assert ev["sky_status_raw"] == "RUNNING"
    assert ev["status_previous"] is None


def test_emit_status_changed_skipped_when_no_manifest(tmp_path: Path):
    """An orphan job (no manifest) emits nothing — there's no session to
    write to, and we don't try to manufacture one."""
    log = get_provenance_log("testsess", base_dir=tmp_path)
    backend = SkyPilotBackend()
    with patch(
        "sciagent.compute.backends.skypilot._read_task_manifest",
        return_value=None,
    ):
        backend._emit_status_changed_event(
            job_id="orphan", managed_job_id=None,
            status=JobStatus.PENDING, sky_status_raw="PENDING",
        )
    assert not _read_events(log)


def test_emit_status_changed_dedup_across_polls(tmp_path: Path):
    """Calling twice with the same status should produce one event."""
    log = get_provenance_log("testsess", base_dir=tmp_path)
    backend = SkyPilotBackend()
    with patch(
        "sciagent.compute.backends.skypilot._read_task_manifest",
        return_value={"job_id": "j", "session_id": "testsess"},
    ), patch(
        "sciagent.compute.backends.skypilot.get_provenance_log",
        side_effect=lambda sid: log,
    ):
        backend._emit_status_changed_event(
            job_id="j", managed_job_id=1, status=JobStatus.RUNNING, sky_status_raw="RUNNING")
        backend._emit_status_changed_event(
            job_id="j", managed_job_id=1, status=JobStatus.RUNNING, sky_status_raw="RUNNING")
        backend._emit_status_changed_event(
            job_id="j", managed_job_id=1, status=JobStatus.COMPLETED, sky_status_raw="SUCCEEDED")

    events = _read_events(log)
    assert len(events) == 2
    assert [e["status"] for e in events] == ["running", "completed"]
    assert events[0]["status_previous"] is None
    assert events[1]["status_previous"] == "running"


def test_emit_status_changed_preserves_sky_status_raw_on_failure(tmp_path: Path):
    """A FAILED_NO_RESOURCE collapse must preserve the original variant
    in sky_status_raw so a verifier can attribute the failure correctly."""
    log = get_provenance_log("testsess", base_dir=tmp_path)
    backend = SkyPilotBackend()
    with patch(
        "sciagent.compute.backends.skypilot._read_task_manifest",
        return_value={"job_id": "j", "session_id": "testsess"},
    ), patch(
        "sciagent.compute.backends.skypilot.get_provenance_log",
        side_effect=lambda sid: log,
    ):
        backend._emit_status_changed_event(
            job_id="j",
            managed_job_id=42,
            status=JobStatus.FAILED,
            sky_status_raw="FAILED_NO_RESOURCE",
            error_preview="No GPU capacity in us-east-2",
            log_file="/tmp/sky-j.log",
        )

    ev = _read_events(log)[0]
    assert ev["status"] == "failed"
    assert ev["sky_status_raw"] == "FAILED_NO_RESOURCE"
    assert ev["error_preview"] == "No GPU capacity in us-east-2"
    assert ev["log_file"] == "/tmp/sky-j.log"
