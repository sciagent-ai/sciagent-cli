"""End-to-end cross-LLM verification (M1B acceptance bar).

Gated behind ``RUN_CROSS_LLM_TESTS=1`` per the M1B handoff. This is the
load-bearing test for the cross-LLM verification claim: a non-Claude
provider, given the JSONL provenance log produced by sciagent, can
produce a coherent verification report.

Cost: roughly $0.01 per run with gpt-4o-mini (target model). Cap is
strict — the prompt is small and the output is JSON-only.

The test only runs when the user opts in. It is intentionally NOT part
of the default suite. Set:

  RUN_CROSS_LLM_TESTS=1 OPENAI_API_KEY=sk-... pytest tests/provenance/test_e2e_cross_llm.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
)
from sciagent.tools.atomic.verify import verify_session


SHOULD_RUN = os.environ.get("RUN_CROSS_LLM_TESTS") == "1"
HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (SHOULD_RUN and HAS_OPENAI_KEY),
    reason=(
        "Cross-LLM tests are gated. Set RUN_CROSS_LLM_TESTS=1 and "
        "OPENAI_API_KEY=... to enable."
    ),
)


# Default verifier model. Using gpt-4o-mini keeps the per-run cost
# at ~$0.01 (small input, JSON-only output).
VERIFIER_MODEL = os.environ.get("CROSS_LLM_VERIFIER_MODEL", "gpt-4o-mini")


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


def _seed_session(log: ProvenanceLog) -> dict:
    """Emit a representative session and return ground-truth facts the
    verifier will be asked to recover from the log."""
    log.emit_tool_call(
        tool_call_id="tc1",
        tool_name="compute_run",
        arguments={"service": "openfoam", "command": "bash Allrun"},
        actor="claude-opus-4-7",
    )
    log.emit_tool_result(
        tool_call_id="tc1",
        tool_name="compute_run",
        success=True,
        output_summary={"job_id": "sciagent-cross-x", "status": "running"},
        error=None,
        duration_ms=1500,
        actor="claude-opus-4-7",
    )
    log.emit_compute_job_launched(
        job_id="sciagent-cross-x",
        managed_job_id=4321,
        backend="skypilot",
        service="openfoam",
        image="ghcr.io/sciagent-ai/openfoam:latest",
        command_original="bash Allrun",
        command_resolved="timeout 3600 bash -c 'cd /workspace && bash Allrun'",
        mount_path="/workspace",
        mount_bucket="cross-llm-bucket",
        requirements={"cpus": 4, "memory_gb": 32, "gpus": 0, "gpu_type": None, "timeout_sec": 3600},
        intent={"paper": "doi:10.example/foo", "case": "typical_c"},
        expected_artifacts=["postProcessing/probes/0/U"],
    )
    log.emit_compute_job_status_changed(
        job_id="sciagent-cross-x", managed_job_id=4321,
        status="running", sky_status_raw="RUNNING")
    log.emit_compute_job_status_changed(
        job_id="sciagent-cross-x", managed_job_id=4321,
        status="completed", sky_status_raw="SUCCEEDED")
    log.emit_artifact_produced(
        path="/workspace/postProcessing/probes/0/U",
        mount_path="/workspace",
        job_id="sciagent-cross-x",
        size_bytes=8192,
    )
    log.emit_verification_result(
        gate="data", task_id="t1",
        claim={"kind": "data_acquisition", "file_path": "/workspace/postProcessing/probes/0/U"},
        verdict="verified", confidence=None,
        evidence={}, issues=[], verifier="provenance_checker",
    )

    return {
        "expected_job_id": "sciagent-cross-x",
        "expected_managed_job_id": 4321,
        "expected_final_status": "completed",
        "expected_artifact_path": "/workspace/postProcessing/probes/0/U",
    }


def _build_verifier_prompt(jsonl_text: str) -> str:
    return (
        "You are an independent verifier reading a JSONL provenance log "
        "produced by a different LLM-driven agent. Each line is one event. "
        "Use ONLY the on-disk evidence below to answer. Return STRICT JSON "
        "matching this schema and nothing else:\n\n"
        "{\n"
        '  "tools_called": [{"tool_call_id": "...", "tool_name": "..."}],\n'
        '  "compute_jobs": [{"job_id": "...", "managed_job_id": <int|null>, "final_status": "..."}],\n'
        '  "artifacts": [{"path": "...", "job_id": "..."}],\n'
        '  "data_gate_verdict": "verified|refuted|insufficient|warning|none",\n'
        '  "summary": "one short sentence"\n'
        "}\n\n"
        "Provenance log:\n"
        "----- BEGIN provenance.jsonl -----\n"
        f"{jsonl_text}"
        "----- END provenance.jsonl -----\n"
    )


def _call_verifier(prompt: str) -> str:
    """Direct litellm call so this test does not depend on AgentLoop."""
    from litellm import completion

    response = completion(
        model=VERIFIER_MODEL,
        messages=[
            {"role": "system", "content": "You return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    content = response["choices"][0]["message"]["content"]
    return content


def _extract_json(text: str) -> dict:
    """Best-effort: find the first {...} block and json.loads it."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in verifier response: {text!r}")
    return json.loads(text[start : end + 1])


def test_cross_provider_can_verify_session_from_jsonl(tmp_path: Path):
    """Acceptance bar: a non-Claude LLM, given the raw JSONL, recovers
    the same facts that verify_session() does — proving the schema is
    provider-neutral and the cross-LLM verification claim holds."""
    log = get_provenance_log("cross-sess", base_dir=tmp_path)
    truth = _seed_session(log)

    # Sanity-check: sciagent's own reader sees the same facts
    report = verify_session("cross-sess", base_dir=tmp_path)
    assert report["compute_jobs"][0]["current_status"] == truth["expected_final_status"]

    # Now the load-bearing call: ask a different provider to read the JSONL
    jsonl_text = log.path.read_text()
    prompt = _build_verifier_prompt(jsonl_text)
    raw = _call_verifier(prompt)
    parsed = _extract_json(raw)

    # The cross-LLM verifier must surface the right job and final status.
    assert any(
        j.get("job_id") == truth["expected_job_id"]
        and j.get("final_status") == truth["expected_final_status"]
        for j in parsed.get("compute_jobs", [])
    ), f"verifier failed to recover compute job state from log: {parsed}"

    # And the artifact.
    assert any(
        a.get("path") == truth["expected_artifact_path"]
        for a in parsed.get("artifacts", [])
    ), f"verifier failed to recover artifact path from log: {parsed}"

    # And the data-gate verdict.
    assert parsed.get("data_gate_verdict") == "verified", (
        f"verifier did not recover the data-gate verdict: {parsed}"
    )

    # Tools called list at minimum mentions compute_run.
    assert any(
        t.get("tool_name") == "compute_run"
        for t in parsed.get("tools_called", [])
    ), f"verifier did not surface the compute_run tool call: {parsed}"
