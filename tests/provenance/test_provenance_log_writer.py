"""Unit tests for the M1B append-only JSONL provenance writer.

Covers the contract documented in docs/provenance_log_schema.md:

  - Envelope shape and required fields per event kind.
  - Sequence numbers are monotonic per session and survive process restart.
  - Status-change dedup is process-local and emits status_previous correctly.
  - Truncation stubs replace oversized truncatable fields and leave
    load-bearing fields alone.
  - Read path skips malformed lines defensively (synthetic _parse_error).
  - intent / expected_artifacts are recorded verbatim per v4.2 §C6.
  - Append semantics: lines ordered by seq, prior events never rewritten.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from sciagent.provenance_log import (
    MAX_FIELD_BYTES,
    SCHEMA_VERSION,
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton_cache():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


@pytest.fixture
def log(tmp_path: Path) -> ProvenanceLog:
    return ProvenanceLog(session_id="testsess01", base_dir=tmp_path)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# File layout
# ---------------------------------------------------------------------------


def test_session_dir_and_log_file_created(tmp_path: Path):
    log = ProvenanceLog(session_id="abc123", base_dir=tmp_path)
    assert log.session_dir == tmp_path / "abc123"
    assert log.path.exists()
    assert log.lock_path.exists()


def test_empty_session_id_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        ProvenanceLog(session_id="", base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Envelope contract
# ---------------------------------------------------------------------------


def test_envelope_fields_present_on_every_event(log: ProvenanceLog):
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"cmd": "ls"})
    events = _read_jsonl(log.path)
    assert len(events) == 1
    ev = events[0]
    for field in ("schema_version", "event_id", "event_kind", "session_id", "seq", "ts"):
        assert field in ev, f"missing envelope field: {field}"
    assert ev["schema_version"] == SCHEMA_VERSION
    assert ev["event_kind"] == "tool_call"
    assert ev["session_id"] == "testsess01"
    assert ev["seq"] == 1
    # ts has explicit +00:00 suffix per the schema
    assert ev["ts"].endswith("+00:00")


def test_actor_field_optional_and_recorded_when_provided(log: ProvenanceLog):
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={}, actor=None)
    log.emit_tool_call(tool_call_id="c2", tool_name="shell", arguments={}, actor="claude-opus-4-7")
    events = _read_jsonl(log.path)
    assert "actor" not in events[0]
    assert events[1]["actor"] == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Sequence numbers
# ---------------------------------------------------------------------------


def test_seq_monotonic_within_session(log: ProvenanceLog):
    for i in range(5):
        log.emit_tool_call(tool_call_id=f"c{i}", tool_name="shell", arguments={"i": i})
    events = _read_jsonl(log.path)
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]


def test_seq_resumes_across_process_restart(tmp_path: Path):
    log1 = ProvenanceLog(session_id="rs", base_dir=tmp_path)
    log1.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={})
    log1.emit_tool_call(tool_call_id="c2", tool_name="shell", arguments={})

    # Simulate a fresh process opening the same session
    log2 = ProvenanceLog(session_id="rs", base_dir=tmp_path)
    log2.emit_tool_call(tool_call_id="c3", tool_name="shell", arguments={})

    events = _read_jsonl(log1.path)
    assert [e["seq"] for e in events] == [1, 2, 3]


# ---------------------------------------------------------------------------
# tool_call / tool_result
# ---------------------------------------------------------------------------


def test_tool_call_records_arguments_and_sha256(log: ProvenanceLog):
    args = {"command": "echo hi", "timeout_sec": 30}
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments=args)
    ev = _read_jsonl(log.path)[0]
    assert ev["arguments"] == args
    assert len(ev["arguments_sha256"]) == 64
    # sha256 stable across calls with equivalent (sorted) arguments
    log.emit_tool_call(tool_call_id="c2", tool_name="shell",
                       arguments={"timeout_sec": 30, "command": "echo hi"})
    ev2 = _read_jsonl(log.path)[1]
    assert ev["arguments_sha256"] == ev2["arguments_sha256"]


def test_tool_result_pairs_via_tool_call_id(log: ProvenanceLog):
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"cmd": "ls"})
    log.emit_tool_result(
        tool_call_id="c1",
        tool_name="shell",
        success=True,
        output_summary={"stdout": "file.txt\n"},
        error=None,
        duration_ms=42,
    )
    events = _read_jsonl(log.path)
    assert events[0]["tool_call_id"] == events[1]["tool_call_id"] == "c1"
    assert events[1]["success"] is True
    assert events[1]["duration_ms"] == 42


# ---------------------------------------------------------------------------
# compute_job_launched (m1a-followups #5)
# ---------------------------------------------------------------------------


def test_compute_job_launched_records_command_original_and_resolved(log: ProvenanceLog):
    log.emit_compute_job_launched(
        job_id="sciagent-abc",
        managed_job_id=42,
        backend="skypilot",
        service="openfoam",
        image="ghcr.io/sciagent-ai/openfoam:latest",
        command_original="bash Allrun",
        command_resolved="timeout 3600 bash -c 'cd /workspace && bash Allrun'",
        mount_path="/workspace",
        mount_bucket="my-bucket",
        requirements={"cpus": 4, "memory_gb": 32, "gpus": 0, "gpu_type": None, "timeout_sec": 3600},
        intent={"paper": "doi:10.example/foo", "case": "typical_c"},
        expected_artifacts=["postProcessing/probes/0/U"],
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["command_original"] == "bash Allrun"
    assert "cd /workspace" in ev["command_resolved"]
    assert ev["mount_path"] == "/workspace"
    assert ev["mount_bucket"] == "my-bucket"


def test_intent_and_expected_artifacts_recorded_verbatim(log: ProvenanceLog):
    """v4.2 §C6: intent / expected_artifacts are opaque-by-design.

    The writer must not normalize or validate them. A verifier reading the
    log must see exactly what the LLM passed, including unusual shapes.
    """
    quirky_intent = {"nested": {"deep": {"value": [1, 2, 3]}}, "tags": ["a", "b"]}
    quirky_artifacts = ["a/b/c", "*.csv", ""]
    log.emit_compute_job_launched(
        job_id="j1",
        managed_job_id=None,
        backend="skypilot",
        service=None,
        image="python:3.11",
        command_original="python -V",
        command_resolved="timeout 60 bash -c 'python -V'",
        mount_path=None,
        mount_bucket=None,
        requirements={"cpus": 1, "memory_gb": 1, "gpus": 0, "gpu_type": None, "timeout_sec": 60},
        intent=quirky_intent,
        expected_artifacts=quirky_artifacts,
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["intent"] == quirky_intent
    assert ev["expected_artifacts"] == quirky_artifacts


def test_compute_job_launched_with_null_mount(log: ProvenanceLog):
    log.emit_compute_job_launched(
        job_id="j2", managed_job_id=None, backend="skypilot",
        service=None, image="python:3.11",
        command_original="python -V",
        command_resolved="timeout 60 bash -c 'python -V'",
        mount_path=None, mount_bucket=None,
        requirements={"cpus": 1, "memory_gb": 1, "gpus": 0, "gpu_type": None, "timeout_sec": 60},
        intent=None, expected_artifacts=None,
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["mount_path"] is None
    assert ev["expected_artifacts"] == []


# ---------------------------------------------------------------------------
# compute_job_status_changed dedup
# ---------------------------------------------------------------------------


def test_status_change_dedup_suppresses_repeat_status(log: ProvenanceLog):
    eid1 = log.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=42, status="running", sky_status_raw="RUNNING")
    eid2 = log.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=42, status="running", sky_status_raw="RUNNING")
    eid3 = log.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=42, status="completed", sky_status_raw="SUCCEEDED")
    assert eid1 is not None
    assert eid2 is None  # suppressed
    assert eid3 is not None

    events = _read_jsonl(log.path)
    assert len(events) == 2
    assert events[0]["status"] == "running"
    assert events[0]["status_previous"] is None
    assert events[1]["status"] == "completed"
    assert events[1]["status_previous"] == "running"


def test_status_dedup_is_per_job(log: ProvenanceLog):
    log.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=1, status="running", sky_status_raw="RUNNING")
    log.emit_compute_job_status_changed(
        job_id="j2", managed_job_id=2, status="running", sky_status_raw="RUNNING")
    events = _read_jsonl(log.path)
    assert len(events) == 2  # both emitted; dedup keyed by job_id


def test_status_failed_records_error_preview_and_log_file(log: ProvenanceLog):
    log.emit_compute_job_status_changed(
        job_id="j1",
        managed_job_id=42,
        status="failed",
        sky_status_raw="FAILED_NO_RESOURCE",
        error_preview="No GPU capacity in us-east-2",
        log_file="/tmp/sciagent/sky-j1.log",
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["status"] == "failed"
    assert ev["sky_status_raw"] == "FAILED_NO_RESOURCE"
    assert ev["error_preview"] == "No GPU capacity in us-east-2"
    assert ev["log_file"] == "/tmp/sciagent/sky-j1.log"


# ---------------------------------------------------------------------------
# artifact_produced (m1a-followups #6)
# ---------------------------------------------------------------------------


def test_artifact_path_relative_to_mount_derived(log: ProvenanceLog):
    log.emit_artifact_produced(
        path="/workspace/postProcessing/probes/0/U",
        mount_path="/workspace",
        job_id="j1",
        size_bytes=1024,
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["path"] == "/workspace/postProcessing/probes/0/U"
    assert ev["mount_path"] == "/workspace"
    assert ev["path_relative_to_mount"] == "postProcessing/probes/0/U"


def test_artifact_relative_to_mount_null_when_path_outside_mount(log: ProvenanceLog):
    log.emit_artifact_produced(
        path="/var/log/something.log",
        mount_path="/workspace",
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["path_relative_to_mount"] is None


def test_artifact_local_no_mount(log: ProvenanceLog):
    log.emit_artifact_produced(path="/tmp/local.csv")
    ev = _read_jsonl(log.path)[0]
    assert ev["mount_path"] is None
    assert ev["path_relative_to_mount"] is None


def test_artifact_relative_to_mount_supports_non_workspace_mount(log: ProvenanceLog):
    """m1a-followups #6: artifact paths must be honest about non-/workspace mounts."""
    log.emit_artifact_produced(
        path="/data/output.parquet",
        mount_path="/data",
        job_id="j1",
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["path_relative_to_mount"] == "output.parquet"


# ---------------------------------------------------------------------------
# verification_result
# ---------------------------------------------------------------------------


def test_verification_result_data_gate(log: ProvenanceLog):
    log.emit_verification_result(
        gate="data",
        task_id="t1",
        claim={"kind": "data_acquisition", "url": "https://example.com/data.csv"},
        verdict="verified",
        confidence=None,
        evidence={"fetch_log_match": True, "file_size": 4096},
        issues=[],
        verifier="provenance_checker",
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["gate"] == "data"
    assert ev["verdict"] == "verified"
    assert ev["verifier"] == "provenance_checker"


def test_verification_result_llm_gate_with_confidence(log: ProvenanceLog):
    log.emit_verification_result(
        gate="llm",
        task_id="t-final",
        claim={"kind": "task_outcome", "summary": "downloaded 100 rows"},
        verdict="refuted",
        confidence=0.92,
        evidence={"file_row_count": 12},
        issues=[{"severity": "error", "category": "row_count_mismatch", "message": "claimed 100, got 12"}],
        verifier="gpt-4o-mini",
    )
    ev = _read_jsonl(log.path)[0]
    assert ev["gate"] == "llm"
    assert ev["verdict"] == "refuted"
    assert ev["confidence"] == 0.92
    assert ev["verifier"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_oversized_arguments_replaced_with_stub(log: ProvenanceLog):
    huge = "x" * (MAX_FIELD_BYTES * 2)
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"blob": huge})
    ev = _read_jsonl(log.path)[0]
    # arguments dict was truncated as a structured value
    assert isinstance(ev["arguments"], dict)
    assert ev["arguments"].get("_truncated") is True
    assert ev["arguments"]["_original_size"] > MAX_FIELD_BYTES
    assert "_sha256" in ev["arguments"]
    # arguments_sha256 still hashes the ORIGINAL canonical-json
    assert len(ev["arguments_sha256"]) == 64


def test_oversized_error_string_replaced_with_stub(log: ProvenanceLog):
    huge_err = "E" * (MAX_FIELD_BYTES * 2)
    log.emit_tool_result(
        tool_call_id="c1", tool_name="shell",
        success=False, output_summary=None,
        error=huge_err, duration_ms=1,
    )
    ev = _read_jsonl(log.path)[0]
    assert isinstance(ev["error"], dict)
    assert ev["error"]["_truncated"] is True
    assert ev["error"]["_original_size"] >= MAX_FIELD_BYTES * 2


def test_load_bearing_fields_not_truncated(log: ProvenanceLog):
    """command_resolved, intent, expected_artifacts must never be replaced
    by a truncation stub — a verifier reading them must see the real values
    even when oversized. The line goes slightly over budget instead."""
    huge_command = "echo " + ("y" * (MAX_FIELD_BYTES * 2))
    huge_intent = {"k": "z" * (MAX_FIELD_BYTES * 2)}
    log.emit_compute_job_launched(
        job_id="j1", managed_job_id=1, backend="skypilot",
        service=None, image="python:3.11",
        command_original=huge_command,
        command_resolved=huge_command,
        mount_path=None, mount_bucket=None,
        requirements={"cpus": 1, "memory_gb": 1, "gpus": 0, "gpu_type": None, "timeout_sec": 60},
        intent=huge_intent,
        expected_artifacts=["a", "b"],
    )
    ev = _read_jsonl(log.path)[0]
    assert isinstance(ev["command_resolved"], str)
    assert ev["command_resolved"].startswith("echo ")
    assert isinstance(ev["intent"], dict)
    assert "k" in ev["intent"]


# ---------------------------------------------------------------------------
# Append-only / read path
# ---------------------------------------------------------------------------


def test_read_events_returns_all_in_order(log: ProvenanceLog):
    for i in range(3):
        log.emit_tool_call(tool_call_id=f"c{i}", tool_name="shell", arguments={"i": i})
    events = log.read_events()
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["arguments"]["i"] for e in events] == [0, 1, 2]


def test_read_events_skips_malformed_line(log: ProvenanceLog):
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"a": 1})
    # Corrupt the file by appending a garbage line
    with open(log.path, "ab") as f:
        f.write(b"this is not json\n")
    log.emit_tool_call(tool_call_id="c2", tool_name="shell", arguments={"a": 2})

    events = log.read_events()
    parse_errors = [e for e in events if e.get("_parse_error")]
    real_events = [e for e in events if not e.get("_parse_error")]
    assert len(parse_errors) == 1
    assert len(real_events) == 2


def test_correction_event_recorded(log: ProvenanceLog):
    eid = log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"a": 1})
    log.emit_correction(
        corrects_event_id=eid,
        reason="argument was mis-recorded — actual call was different",
        replacement={"arguments": {"a": 2}},
    )
    events = _read_jsonl(log.path)
    assert events[1]["event_kind"] == "correction"
    assert events[1]["corrects_event_id"] == eid
    assert events[1]["replacement"] == {"arguments": {"a": 2}}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_writers_produce_no_torn_lines(log: ProvenanceLog):
    """Many threads emitting in parallel must produce well-formed JSON
    on every line, with strictly monotonic seq values."""
    n_threads = 8
    per_thread = 25

    def worker(tid: int):
        for i in range(per_thread):
            log.emit_tool_call(
                tool_call_id=f"t{tid}-{i}",
                tool_name="shell",
                arguments={"tid": tid, "i": i},
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _read_jsonl(log.path)
    assert len(events) == n_threads * per_thread
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1 and seqs[-1] == n_threads * per_thread


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def test_get_provenance_log_returns_singleton_per_session(tmp_path: Path):
    a1 = get_provenance_log("sessA", base_dir=tmp_path)
    a2 = get_provenance_log("sessA", base_dir=tmp_path)
    b = get_provenance_log("sessB", base_dir=tmp_path)
    assert a1 is a2
    assert a1 is not b


def test_singleton_status_memo_shared_across_callers(tmp_path: Path):
    """Two callers that resolve the same session must share status memo —
    this is what keeps compute_job_status_changed dedup coherent when
    different code paths emit transitions for the same job."""
    a = get_provenance_log("sX", base_dir=tmp_path)
    b = get_provenance_log("sX", base_dir=tmp_path)
    a.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=1, status="running", sky_status_raw="RUNNING")
    suppressed = b.emit_compute_job_status_changed(
        job_id="j1", managed_job_id=1, status="running", sky_status_raw="RUNNING")
    assert suppressed is None
