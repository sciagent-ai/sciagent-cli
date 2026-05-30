"""Tests for verify_session — the atomic tool that reads the durable
provenance log and produces a structured report.

Three hard rules apply (carried from M1A):

  1. Non-blocking, one-shot.
  2. No convenience helper hiding a wait.
  3. Snapshot, not stream.

The report shape is part of the M1B contract — a different LLM provider
must be able to consume it without sciagent-specific knowledge. These
tests pin down the fields that ride that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
)
from sciagent.tools.atomic.verify import verify_session


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


@pytest.fixture
def log(tmp_path: Path) -> ProvenanceLog:
    return get_provenance_log("rep-sess", base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Empty session
# ---------------------------------------------------------------------------


def test_empty_log_produces_zero_event_report(tmp_path: Path, log: ProvenanceLog):
    report = verify_session("rep-sess", base_dir=tmp_path)
    assert report["session_id"] == "rep-sess"
    assert report["events_total"] == 0
    assert report["events_by_kind"] == {}
    assert report["compute_jobs"] == []
    assert report["artifacts"] == []
    assert report["summary_issues"] == []


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def test_tool_call_pairs_counted(tmp_path: Path, log: ProvenanceLog):
    for i in range(3):
        log.emit_tool_call(tool_call_id=f"c{i}", tool_name="shell", arguments={"i": i})
        log.emit_tool_result(
            tool_call_id=f"c{i}", tool_name="shell", success=True,
            output_summary={"ok": True}, error=None, duration_ms=10,
        )

    report = verify_session("rep-sess", base_dir=tmp_path)
    assert report["tool_calls"]["total"] == 3
    assert report["tool_calls"]["results_total"] == 3
    assert report["tool_calls"]["unmatched"] == []


def test_unmatched_tool_call_surfaced_as_summary_issue(tmp_path: Path, log: ProvenanceLog):
    """A call without a result is surfaced — the session may have crashed
    or been interrupted mid-tool-dispatch."""
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"x": 1})
    # No matching tool_result.

    report = verify_session("rep-sess", base_dir=tmp_path)
    assert report["tool_calls"]["total"] == 1
    assert report["tool_calls"]["results_total"] == 0
    assert len(report["tool_calls"]["unmatched"]) == 1
    issues = [i for i in report["summary_issues"] if i["category"] == "unmatched_tool_calls"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Compute jobs
# ---------------------------------------------------------------------------


def test_compute_job_summary_joins_launch_status_and_artifacts(
    tmp_path: Path, log: ProvenanceLog
):
    log.emit_compute_job_launched(
        job_id="sciagent-x", managed_job_id=42, backend="skypilot",
        service="openfoam", image="ghcr.io/sciagent-ai/openfoam:latest",
        command_original="bash Allrun",
        command_resolved="timeout 3600 bash -c 'cd /workspace && bash Allrun'",
        mount_path="/workspace", mount_bucket="b8",
        requirements={"cpus": 4, "memory_gb": 32, "gpus": 0, "gpu_type": None, "timeout_sec": 3600},
        intent={"paper": "doi:1"}, expected_artifacts=["postProcessing/probes/0/U"],
    )
    log.emit_compute_job_status_changed(
        job_id="sciagent-x", managed_job_id=42, status="running", sky_status_raw="RUNNING")
    log.emit_compute_job_status_changed(
        job_id="sciagent-x", managed_job_id=42, status="completed", sky_status_raw="SUCCEEDED")
    log.emit_artifact_produced(
        path="/workspace/postProcessing/probes/0/U",
        mount_path="/workspace",
        job_id="sciagent-x",
        size_bytes=1024,
    )

    report = verify_session("rep-sess", base_dir=tmp_path)
    jobs = report["compute_jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == "sciagent-x"
    assert job["launched"]["command_original"] == "bash Allrun"
    assert "cd /workspace" in job["launched"]["command_resolved"]
    assert job["launched"]["mount_path"] == "/workspace"
    assert job["launched"]["intent"] == {"paper": "doi:1"}
    assert job["launched"]["expected_artifacts"] == ["postProcessing/probes/0/U"]
    assert job["current_status"] == "completed"
    assert [t["status"] for t in job["status_transitions"]] == ["running", "completed"]
    assert len(job["artifacts"]) == 1
    assert job["artifacts"][0]["path_relative_to_mount"] == "postProcessing/probes/0/U"


def test_failed_job_surfaced_as_summary_issue(tmp_path: Path, log: ProvenanceLog):
    log.emit_compute_job_launched(
        job_id="j1", managed_job_id=1, backend="skypilot",
        service=None, image="python:3.11",
        command_original="python -V",
        command_resolved="timeout 60 bash -c 'python -V'",
        mount_path=None, mount_bucket=None,
        requirements={"cpus": 1, "memory_gb": 1, "gpus": 0, "gpu_type": None, "timeout_sec": 60},
        intent=None, expected_artifacts=None,
    )
    log.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=1, status="failed", sky_status_raw="FAILED_NO_RESOURCE",
        error_preview="No GPU capacity",
    )

    report = verify_session("rep-sess", base_dir=tmp_path)
    issues = [i for i in report["summary_issues"] if i["category"] == "failed_compute_jobs"]
    assert len(issues) == 1
    assert issues[0]["job_ids"] == ["j1"]
    assert issues[0]["severity"] == "error"


def test_compute_job_with_only_status_transitions_appears_with_null_launched(
    tmp_path: Path, log: ProvenanceLog
):
    """A session that resumed and started polling without observing the
    launch event still lists the job — the verifier should see *something*,
    not silently drop it."""
    log.emit_compute_job_status_changed(
        job_id="resumed-job", managed_job_id=99, status="running", sky_status_raw="RUNNING")

    report = verify_session("rep-sess", base_dir=tmp_path)
    jobs = report["compute_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "resumed-job"
    assert jobs[0]["launched"] is None
    assert jobs[0]["current_status"] == "running"


# ---------------------------------------------------------------------------
# Verifications
# ---------------------------------------------------------------------------


def test_verifications_grouped_by_gate_with_verdict_counts(tmp_path: Path, log: ProvenanceLog):
    log.emit_verification_result(
        gate="data", task_id="t1",
        claim={"kind": "data_acquisition", "url": "https://example/x.csv"},
        verdict="verified", confidence=None,
        evidence={"file_size": 100}, issues=[], verifier="provenance_checker",
    )
    log.emit_verification_result(
        gate="exec", task_id="t1",
        claim={"kind": "execution", "claimed_command": "pytest"},
        verdict="refuted", confidence=None,
        evidence={}, issues=[
            {"severity": "error", "category": "no_execution_record", "message": "no exec"}
        ], verifier="provenance_checker",
    )
    log.emit_verification_result(
        gate="llm", task_id="t-final",
        claim={"kind": "task_outcome"},
        verdict="verified", confidence=0.9,
        evidence={}, issues=[], verifier="gpt-4o-mini",
    )

    report = verify_session("rep-sess", base_dir=tmp_path)
    v = report["verifications"]
    assert v["data"]["total"] == 1
    assert v["data"]["verdicts"]["verified"] == 1
    assert v["exec"]["total"] == 1
    assert v["exec"]["verdicts"]["refuted"] == 1
    assert v["llm"]["total"] == 1
    assert v["llm"]["entries"][0]["verifier"] == "gpt-4o-mini"

    refuted_issues = [i for i in report["summary_issues"] if i["category"] == "refuted_verifications"]
    assert len(refuted_issues) == 1


# ---------------------------------------------------------------------------
# Corrections + parse errors
# ---------------------------------------------------------------------------


def test_correction_event_carried_into_report(tmp_path: Path, log: ProvenanceLog):
    eid = log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"a": 1})
    log.emit_correction(
        corrects_event_id=eid, reason="argument shape was wrong",
        replacement={"arguments": {"a": 2}},
    )
    report = verify_session("rep-sess", base_dir=tmp_path)
    assert len(report["corrections"]) == 1
    assert report["corrections"][0]["corrects_event_id"] == eid


def test_parse_errors_counted_in_report(tmp_path: Path, log: ProvenanceLog):
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"a": 1})
    with open(log.path, "ab") as f:
        f.write(b"garbage line not json\n")

    report = verify_session("rep-sess", base_dir=tmp_path)
    assert report["parse_errors"] == 1
    issues = [i for i in report["summary_issues"] if i["category"] == "log_parse_errors"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Atomic-tool surface (three hard rules)
# ---------------------------------------------------------------------------


# VerifySessionTool was retired 2026-05-29 — tests for the tool-class wrapper
# (test_verify_session_is_a_one_shot_atomic_tool, test_tool_execute_*,
# test_tool_rejects_empty_session_id) have been removed. The pure function
# verify_session(...) below is still exercised; that's what tests + future
# H2 replay code will call.


def test_repeat_calls_produce_consistent_snapshots(tmp_path: Path, log: ProvenanceLog):
    """Snapshot, not stream: two calls in a row see the same state until
    new events are emitted, and seq monotonicity is preserved."""
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"x": 1})
    r1 = verify_session("rep-sess", base_dir=tmp_path)
    r2 = verify_session("rep-sess", base_dir=tmp_path)
    assert r1["events_total"] == r2["events_total"] == 1

    # Add an event; the next snapshot reflects it
    log.emit_tool_result(
        tool_call_id="c1", tool_name="shell", success=True,
        output_summary={}, error=None, duration_ms=1,
    )
    r3 = verify_session("rep-sess", base_dir=tmp_path)
    assert r3["events_total"] == 2
