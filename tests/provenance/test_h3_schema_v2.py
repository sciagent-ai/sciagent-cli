"""H3 — schema v2: cost / tokens / model on tool_result, cost on
compute_job_status_changed, LLMClient._last_usage capture, and v1/v2
backward compatibility on the verify_session reader.

Constraints:
  - Never @patch("litellm.completion"). Use litellm.completion(mock_response=...)
    or a litellm.ModelResponse fixture passed directly to the capture helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import litellm
from litellm import ModelResponse

from sciagent.llm import LLMClient, Message
from sciagent.provenance_log import (
    SCHEMA_VERSION,
    ProvenanceLog,
    reset_provenance_logs,
)
from sciagent.tools.atomic.verify import verify_session


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Schema version + writer extension
# ---------------------------------------------------------------------------


def test_schema_version_is_2():
    assert SCHEMA_VERSION == "2"


def test_tool_result_event_carries_v2_cost_fields(tmp_path: Path):
    log = ProvenanceLog(session_id="h3-sess", base_dir=tmp_path)
    log.emit_tool_call(tool_call_id="c1", tool_name="shell", arguments={"cmd": "ls"})
    log.emit_tool_result(
        tool_call_id="c1",
        tool_name="shell",
        success=True,
        output_summary="ok",
        error=None,
        duration_ms=12,
        cost_usd=0.000123,
        tokens_in=100,
        tokens_out=42,
        model="claude-opus-4-7",
    )
    events = _read_jsonl(log.path)
    result_ev = next(e for e in events if e["event_kind"] == "tool_result")
    assert result_ev["cost_usd"] == pytest.approx(0.000123)
    assert result_ev["tokens_in"] == 100
    assert result_ev["tokens_out"] == 42
    assert result_ev["model"] == "claude-opus-4-7"
    assert result_ev["schema_version"] == "2"


def test_tool_result_event_omits_v2_fields_when_unset(tmp_path: Path):
    log = ProvenanceLog(session_id="h3-sess", base_dir=tmp_path)
    log.emit_tool_result(
        tool_call_id="c1",
        tool_name="shell",
        success=True,
        output_summary="ok",
        error=None,
        duration_ms=12,
    )
    ev = next(e for e in _read_jsonl(log.path) if e["event_kind"] == "tool_result")
    assert ev["cost_usd"] is None
    assert ev["tokens_in"] is None
    assert ev["tokens_out"] is None
    assert ev["model"] is None


def test_compute_job_status_changed_carries_v2_cost_usd(tmp_path: Path):
    log = ProvenanceLog(session_id="h3-sess", base_dir=tmp_path)
    log.emit_compute_job_status_changed(
        job_id="sciagent-x", managed_job_id=7, status="running",
    )
    log.emit_compute_job_status_changed(
        job_id="sciagent-x",
        managed_job_id=7,
        status="succeeded",
        cost_usd=0.42,
    )
    transitions = [e for e in _read_jsonl(log.path) if e["event_kind"] == "compute_job_status_changed"]
    assert transitions[0]["cost_usd"] is None
    assert transitions[1]["cost_usd"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# LLMClient._last_usage capture (no @patch on litellm)
# ---------------------------------------------------------------------------


def test_last_usage_populated_via_litellm_mock_response():
    """litellm's native ``mock_response`` kwarg routes through real response
    shaping and populates both ``usage`` and ``_hidden_params['response_cost']``.
    Threading it through ``LLMClient.chat()`` exercises the full capture path
    end-to-end without ever patching litellm.
    """
    client = LLMClient(model="claude-opus-4-7")
    client.chat(
        [Message(role="user", content="hi")],
        mock_response="ok",
    )
    last = client._last_usage
    assert last["tokens_in"] is not None and last["tokens_in"] > 0
    assert last["tokens_out"] is not None and last["tokens_out"] > 0
    assert last["model"] == "claude-opus-4-7"
    # litellm populates response_cost on its mock path for supported providers.
    assert last["cost_usd"] is not None
    assert last["cost_usd"] > 0


def test_last_usage_missing_usage_safe():
    """A ModelResponse fixture without a ``usage`` attribute must not raise;
    fields default to None. Mirrors a provider response where litellm
    didn't surface token counts."""
    client = LLMClient(model="claude-opus-4-7")
    fixture = ModelResponse(
        choices=[
            {"message": {"role": "assistant", "content": "ok", "tool_calls": None}, "finish_reason": "stop"}
        ]
    )
    # No _hidden_params either.
    client._capture_last_usage(fixture, {"model": "claude-opus-4-7"})
    assert client._last_usage == {
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
        "model": "claude-opus-4-7",
    }


def test_last_usage_cost_missing_when_hidden_params_absent():
    """Some providers leave _hidden_params off the response entirely."""
    client = LLMClient(model="gpt-5-mini")
    # Construct a fixture with usage but no _hidden_params.
    fixture = ModelResponse(
        choices=[
            {"message": {"role": "assistant", "content": "ok", "tool_calls": None}, "finish_reason": "stop"}
        ],
        usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    )
    client._capture_last_usage(fixture, {"model": "gpt-5-mini"})
    last = client._last_usage
    assert last["tokens_in"] == 9
    assert last["tokens_out"] == 3
    assert last["cost_usd"] is None
    assert last["model"] == "gpt-5-mini"


# ---------------------------------------------------------------------------
# verify_session reader: accept v1 + v2
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, session_id: str, events: List[Dict[str, Any]]) -> Path:
    """Write a synthetic provenance.jsonl with the given event lines so we
    can exercise the verify_session reader against historical schemas."""
    sess_dir = tmp_path / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    log = sess_dir / "provenance.jsonl"
    with log.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return log


def test_verify_session_reads_v1_log_with_no_cost_fields(tmp_path: Path):
    _write_log(
        tmp_path,
        "v1sess",
        [
            {
                "schema_version": "1",
                "event_id": "e1",
                "event_kind": "tool_call",
                "session_id": "v1sess",
                "seq": 1,
                "ts": "2026-05-27T00:00:00.000000+00:00",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "arguments": {"cmd": "ls"},
                "arguments_sha256": "x" * 64,
            },
            {
                "schema_version": "1",
                "event_id": "e2",
                "event_kind": "tool_result",
                "session_id": "v1sess",
                "seq": 2,
                "ts": "2026-05-27T00:00:00.001000+00:00",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "success": True,
                "output_summary": "ok",
                "error": None,
                "duration_ms": 5,
            },
            {
                "schema_version": "1",
                "event_id": "e3",
                "event_kind": "compute_job_launched",
                "session_id": "v1sess",
                "seq": 3,
                "ts": "2026-05-27T00:00:00.002000+00:00",
                "job_id": "j1",
                "managed_job_id": 1,
                "backend": "skypilot",
                "service": "openfoam",
                "image": "ghcr.io/x:1",
                "command_original": "echo hi",
                "command_resolved": "cd /workspace && echo hi",
                "mount_path": "/workspace",
                "mount_bucket": "sciagent-workspace-v1sess",
                "requirements": {},
                "intent": None,
                "expected_artifacts": [],
            },
            {
                "schema_version": "1",
                "event_id": "e4",
                "event_kind": "compute_job_status_changed",
                "session_id": "v1sess",
                "seq": 4,
                "ts": "2026-05-27T00:00:00.003000+00:00",
                "job_id": "j1",
                "managed_job_id": 1,
                "status": "succeeded",
                "status_previous": None,
                "sky_status_raw": "SUCCEEDED",
                "error_preview": None,
                "log_file": None,
            },
        ],
    )

    report = verify_session("v1sess", base_dir=tmp_path)
    assert report["session_id"] == "v1sess"
    assert report["events_total"] == 4
    job = report["compute_jobs"][0]
    assert job["status_transitions"][0]["status"] == "succeeded"
    assert job["status_transitions"][0]["cost_usd"] is None


def test_verify_session_reads_v2_log_with_cost_fields(tmp_path: Path):
    _write_log(
        tmp_path,
        "v2sess",
        [
            {
                "schema_version": "2",
                "event_id": "e1",
                "event_kind": "compute_job_launched",
                "session_id": "v2sess",
                "seq": 1,
                "ts": "2026-05-28T00:00:00.000000+00:00",
                "job_id": "j1",
                "managed_job_id": 1,
                "backend": "skypilot",
                "service": "openfoam",
                "image": "ghcr.io/x:1",
                "command_original": "echo hi",
                "command_resolved": "cd /workspace && echo hi",
                "mount_path": "/workspace",
                "mount_bucket": "sciagent-workspace-v2sess",
                "requirements": {},
                "intent": None,
                "expected_artifacts": [],
            },
            {
                "schema_version": "2",
                "event_id": "e2",
                "event_kind": "compute_job_status_changed",
                "session_id": "v2sess",
                "seq": 2,
                "ts": "2026-05-28T00:00:00.001000+00:00",
                "job_id": "j1",
                "managed_job_id": 1,
                "status": "succeeded",
                "status_previous": None,
                "sky_status_raw": "SUCCEEDED",
                "error_preview": None,
                "log_file": None,
                "cost_usd": 0.42,
            },
        ],
    )

    report = verify_session("v2sess", base_dir=tmp_path)
    job = report["compute_jobs"][0]
    assert job["status_transitions"][0]["cost_usd"] == pytest.approx(0.42)
