"""End-to-end M1B test (single process, no API calls).

Exercises every event kind from the schema in one session, then runs
verify_session against the resulting log and asserts the report
contains the expected sections.

This is the M1B acceptance bar: "A session run produces a complete
provenance.jsonl with every event type from the schema represented at
least once. verify_session(session_id) produces a structured report."

Cross-LLM verification (different provider via LiteLLM consuming the
same log) is in test_e2e_cross_llm.py and is gated behind
RUN_CROSS_LLM_TESTS=1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
    set_active_session,
)
from sciagent.tools.atomic.verify import verify_session


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    set_active_session(None)
    yield
    reset_provenance_logs()
    set_active_session(None)


def test_session_log_contains_every_event_kind(tmp_path: Path):
    """Single-process e2e: drive the writer through every event kind in
    one session, then run verify_session and assert the report sections."""
    log: ProvenanceLog = get_provenance_log("e2e-sess", base_dir=tmp_path)

    # ------- 1. tool_call + tool_result --------
    cid_call = log.emit_tool_call(
        tool_call_id="tc1",
        tool_name="compute_run",
        arguments={"service": "openfoam", "command": "bash Allrun"},
        actor="claude-opus-4-7",
    )
    log.emit_tool_result(
        tool_call_id="tc1",
        tool_name="compute_run",
        success=True,
        output_summary={"job_id": "sciagent-x", "status": "running"},
        error=None,
        duration_ms=1234,
        actor="claude-opus-4-7",
    )

    # ------- 2. compute_job_launched + compute_job_status_changed -------
    log.emit_compute_job_launched(
        job_id="sciagent-x",
        managed_job_id=42,
        backend="skypilot",
        service="openfoam",
        image="ghcr.io/sciagent-ai/openfoam:latest",
        command_original="bash Allrun",
        command_resolved="timeout 3600 bash -c 'cd /workspace && bash Allrun'",
        mount_path="/workspace",
        mount_bucket="b8-bucket",
        requirements={"cpus": 4, "memory_gb": 32, "gpus": 0, "gpu_type": None, "timeout_sec": 3600},
        intent={"paper": "doi:10.example/foo", "case": "typical_c"},
        expected_artifacts=["postProcessing/probes/0/U"],
    )
    log.emit_compute_job_status_changed(
        job_id="sciagent-x", managed_job_id=42, status="running", sky_status_raw="RUNNING")
    log.emit_compute_job_status_changed(
        job_id="sciagent-x", managed_job_id=42, status="completed", sky_status_raw="SUCCEEDED")

    # ------- 3. artifact_produced -------
    log.emit_artifact_produced(
        path="/workspace/postProcessing/probes/0/U",
        mount_path="/workspace",
        job_id="sciagent-x",
        size_bytes=8192,
        content_type="text/plain",
        metadata={"row_count": 100},
    )

    # ------- 4. verification_result (data, exec, llm) -------
    log.emit_verification_result(
        gate="data", task_id="t1",
        claim={"kind": "data_acquisition", "url": "https://example/x.csv", "file_path": "/data/x.csv"},
        verdict="verified", confidence=None,
        evidence={"file_size": 4096}, issues=[],
        verifier="provenance_checker",
    )
    log.emit_verification_result(
        gate="exec", task_id="t1",
        claim={"kind": "execution", "claimed_command": "pytest"},
        verdict="verified", confidence=None,
        evidence={"runs": 2}, issues=[],
        verifier="provenance_checker",
    )
    log.emit_verification_result(
        gate="llm", task_id="t-final",
        claim={"kind": "task_outcome", "task_content": "deliver final report"},
        verdict="verified", confidence=0.91,
        evidence={"reasoning": "all artifacts match expected"},
        issues=[],
        verifier="gpt-4o-mini",
    )

    # ------- 5. correction -------
    log.emit_correction(
        corrects_event_id=cid_call,
        reason="argument was mis-recorded — actual call had a different shape",
        replacement={"arguments": {"service": "openfoam", "command": "bash Allrun -parallel"}},
    )

    # ------- run verify_session and assert structure -------
    report = verify_session("e2e-sess", base_dir=tmp_path)

    # Schema version + identity
    assert report["schema_version"] == "1"
    assert report["session_id"] == "e2e-sess"

    # All seven event kinds represented
    expected_kinds = {
        "tool_call",
        "tool_result",
        "compute_job_launched",
        "compute_job_status_changed",
        "artifact_produced",
        "verification_result",
        "correction",
    }
    assert expected_kinds <= set(report["events_by_kind"].keys()), (
        f"missing kinds: {expected_kinds - set(report['events_by_kind'].keys())}"
    )
    # 1 tool_call + 1 tool_result + 1 launched + 2 status_changed
    # + 1 artifact + 3 verification_result + 1 correction = 10
    assert report["events_total"] == 10

    # Tool calls paired
    assert report["tool_calls"]["total"] == 1
    assert report["tool_calls"]["results_total"] == 1
    assert report["tool_calls"]["unmatched"] == []

    # Compute job: launched + 2 transitions + 1 artifact
    jobs = report["compute_jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == "sciagent-x"
    assert job["launched"]["command_original"] == "bash Allrun"
    assert "cd /workspace" in job["launched"]["command_resolved"]
    assert job["launched"]["mount_path"] == "/workspace"
    assert job["launched"]["intent"] == {"paper": "doi:10.example/foo", "case": "typical_c"}
    assert job["launched"]["expected_artifacts"] == ["postProcessing/probes/0/U"]
    assert [t["status"] for t in job["status_transitions"]] == ["running", "completed"]
    assert job["current_status"] == "completed"
    assert len(job["artifacts"]) == 1
    assert job["artifacts"][0]["path_relative_to_mount"] == "postProcessing/probes/0/U"

    # Verifications grouped per gate
    v = report["verifications"]
    assert v["data"]["total"] == 1 and v["data"]["verdicts"]["verified"] == 1
    assert v["exec"]["total"] == 1 and v["exec"]["verdicts"]["verified"] == 1
    assert v["llm"]["total"] == 1 and v["llm"]["verdicts"]["verified"] == 1
    assert v["llm"]["entries"][0]["verifier"] == "gpt-4o-mini"

    # Corrections
    assert len(report["corrections"]) == 1
    assert report["corrections"][0]["corrects_event_id"] == cid_call

    # No parse errors and no summary issues (everything succeeded)
    assert report["parse_errors"] == 0
    assert report["summary_issues"] == []
