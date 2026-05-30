"""Trajectory-aware LLM verification gate tests.

These tests cover the surface_merge_findings.md §1 gap: the verifier
subagent must read the raw session log (`~/.sciagent/sessions/<sid>/
provenance.jsonl`) rather than a curated FetchLogger/ExecLogger summary.

The gate's prompt now ships the session log path + claim and lets the
verifier subagent open the log via its `file_ops` tool. Each fixture
below writes a real provenance.jsonl into a temp session dir, invokes the
gate with a fake subagent that captures the rendered prompt and returns a
canned verdict, then asserts:

  - the prompt header carries the right session log path + claim,
  - the verdict round-trips through the gate's parser, and
  - the `verification_result` event lands with the new evidence shape.

Per memory `feedback_no_mock_litellm.md`: we never `@patch("litellm.
completion")`. The fake subagent is the seam the orchestrator already
supports (see `test_verification_emission.py`); the verifier subagent's
own real-LLM behavior is covered by the opt-in integration test elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from sciagent.orchestrator import OrchestratorConfig, TaskOrchestrator
from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
    set_active_session,
)
from sciagent.tools.atomic.todo import TodoTool


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    set_active_session(None)
    yield
    reset_provenance_logs()
    set_active_session(None)


@pytest.fixture
def session(tmp_path: Path) -> ProvenanceLog:
    log = get_provenance_log("trajsess", base_dir=tmp_path)
    set_active_session("trajsess")
    return log


class _RecordingSubagent:
    """Fake SubAgentOrchestrator that captures the verifier prompt and
    returns a caller-supplied JSON verdict. Mirrors the seam the existing
    `test_llm_gate_emits_verification_result_per_task` test uses."""

    def __init__(self, verdict_json: str, model: str = "anthropic/claude-sonnet-4-6"):
        self.verdict_json = verdict_json
        self.llm = MagicMock(model=model)
        self.captured_prompts: List[str] = []
        self.captured_agent_names: List[str] = []

    def spawn(self, agent_name: str, prompt: str) -> Any:
        self.captured_prompts.append(prompt)
        self.captured_agent_names.append(agent_name)
        return MagicMock(
            success=True,
            output=self.verdict_json,
            error=None,
            iterations=1,
        )


def _write_trajectory(log: ProvenanceLog, events: List[Dict[str, Any]]) -> None:
    """Push a list of pre-built events through the log writer so they get
    real envelopes (session_id, seq, ts, event_id)."""
    for ev in events:
        kind = ev.pop("event_kind")
        actor = ev.pop("actor", None)
        log._write_event(kind, ev, actor=actor)


def _events(log: ProvenanceLog, kind: str | None = None) -> List[Dict[str, Any]]:
    raw = [json.loads(line) for line in log.path.read_text().splitlines() if line.strip()]
    if kind:
        return [e for e in raw if e["event_kind"] == kind]
    return raw


def _run_gate(
    session_log: ProvenanceLog,
    *,
    task_content: str,
    task_result: str,
    verdict_json: str,
    original_request: str | None = None,
) -> tuple[_RecordingSubagent, Dict[str, Any]]:
    todo = TodoTool()
    todo.execute(todos=[{
        "id": "t1",
        "content": task_content,
        "task_type": "output",
        "depends_on": [],
        "result_key": "t1",
        "priority": "medium",
        "can_parallel": False,
        "status": "completed",
    }])
    graph = todo.get_graph()
    for t in graph.get_all():
        t.status = "completed"
        t.result = task_result

    config = OrchestratorConfig(verbose=False, original_request=original_request)
    fake = _RecordingSubagent(verdict_json)
    orch = TaskOrchestrator(todo_tool=todo, subagent_orchestrator=fake, config=config)
    tasks = orch._get_tasks_requiring_verification()
    assert tasks, "test setup expected at least one task to verify"

    gate_result = orch._run_llm_verification_gate(tasks)
    return fake, gate_result


# ---------------------------------------------------------------------------
# Prompt-shape contract: the verifier sees the session log path + claim only
# ---------------------------------------------------------------------------


def test_prompt_carries_session_log_path_and_claim(session: ProvenanceLog):
    """The new prompt body must name the absolute session log path and the
    session id so the verifier can `file_ops` it directly."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "c1", "tool_name": "bash",
         "arguments": {"command": "echo hi"}, "arguments_sha256": "x"},
    ])

    verdict = json.dumps({"verdict": "verified", "confidence": 0.9})
    fake, _ = _run_gate(
        session,
        task_content="produce final report",
        task_result="done",
        verdict_json=verdict,
        original_request="please report",
    )

    assert len(fake.captured_prompts) == 1
    prompt = fake.captured_prompts[0]
    assert fake.captured_agent_names == ["verifier"]
    assert str(session.path) in prompt, "verifier prompt must name the session log path"
    assert "trajsess" in prompt, "verifier prompt must name the session id"
    assert "produce final report" in prompt
    assert "Claimed result: done" in prompt
    assert "ORIGINAL USER GOAL: please report" in prompt
    # The old curated evidence summary must NOT be in the prompt anymore.
    assert "Fetch Log (recent HTTP requests)" not in prompt
    assert "Exec Log (recent commands)" not in prompt


def test_prompt_path_resolves_to_real_file_for_verifier_file_ops(
    session: ProvenanceLog, tmp_path: Path
):
    """The path injected into the prompt must actually exist so the
    verifier subagent's `file_ops(read, path=...)` resolves."""
    _write_trajectory(session, [
        {"event_kind": "session_end", "model": "anthropic/claude-sonnet-4-6",
         "iterations": 1, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0,
         "wall_seconds": 1.0, "exit_reason": "done"},
    ])

    verdict = json.dumps({"verdict": "verified", "confidence": 0.9})
    fake, _ = _run_gate(
        session, task_content="t", task_result="done", verdict_json=verdict,
    )
    # Pull the path out of the prompt and confirm it's readable JSONL.
    prompt = fake.captured_prompts[0]
    path_line = next(line for line in prompt.splitlines() if line.startswith("Session log:"))
    log_path = Path(path_line.split("Session log:", 1)[1].strip())
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert any(e["event_kind"] == "session_end" for e in lines)


# ---------------------------------------------------------------------------
# Verdict round-trip: the existing parser stays shape-compatible
# ---------------------------------------------------------------------------


def test_refuted_verdict_with_fabrication_indicators_round_trips(
    session: ProvenanceLog,
):
    """Self-write-then-cite fixture: agent wrote /tmp/data.json via inline
    bash, then 'verified' against it. Verifier returns refuted naming the
    write tool_call. The gate must emit the verdict with fabrication
    indicators intact."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "write-1", "tool_name": "bash",
         "arguments": {"command": "cat > /tmp/data.json << EOF\n{\"entries\": 10}\nEOF"},
         "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "write-1", "tool_name": "bash",
         "success": True, "output_summary": "", "error": None, "duration_ms": 5,
         "cost_usd": None, "cost_kind": None, "tokens_in": None,
         "tokens_out": None, "model": None},
        {"event_kind": "tool_call", "tool_call_id": "read-1", "tool_name": "file_ops",
         "arguments": {"command": "read", "path": "/tmp/data.json"},
         "arguments_sha256": "y"},
        {"event_kind": "tool_result", "tool_call_id": "read-1", "tool_name": "file_ops",
         "success": True, "output_summary": '{"entries": 10}', "error": None,
         "duration_ms": 1, "cost_usd": None, "cost_kind": None,
         "tokens_in": None, "tokens_out": None, "model": None},
    ])

    verdict = json.dumps({
        "verdict": "refuted",
        "confidence": 0.95,
        "issues": ["claim cites a file the agent wrote in-session, not external data"],
        "supporting_facts": [],
        "fabrication_indicators": [
            "self-write-then-cite: tool_call_id=write-1 wrote /tmp/data.json before the read"
        ],
        "missing_evidence": ["no external network fetch in the trajectory"],
        "reasoning": "Task is data_acquisition; trajectory has no external fetch, "
                     "only an inline self-write the agent then read back.",
    })

    fake, gate_result = _run_gate(
        session,
        task_content="fetch peptide entries from DBAASP",
        task_result="fetched 10 entries; saved to /tmp/data.json",
        verdict_json=verdict,
    )

    assert gate_result["tasks_verified"] == 0
    assert gate_result["tasks_failed"] == 1

    llm_events = [e for e in _events(session, "verification_result") if e["gate"] == "llm"]
    assert len(llm_events) == 1
    ev = llm_events[0]
    assert ev["verdict"] == "refuted"
    assert ev["confidence"] == 0.95
    fab = ev["evidence"]["fabrication_indicators"]
    assert any("self-write-then-cite" in s for s in fab)
    assert ev["evidence"]["missing_evidence"]


def test_insufficient_verdict_for_vague_claim(session: ProvenanceLog):
    """Vague claim + vague trajectory → insufficient. The gate must thread
    that verdict through without coercing it to refuted/verified."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "b1", "tool_name": "bash",
         "arguments": {"command": "ls"}, "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "b1", "tool_name": "bash",
         "success": True, "output_summary": "a.txt b.txt", "error": None,
         "duration_ms": 2, "cost_usd": None, "cost_kind": None,
         "tokens_in": None, "tokens_out": None, "model": None},
    ])

    verdict = json.dumps({
        "verdict": "insufficient",
        "confidence": 0.4,
        "issues": ["claim is vague; trajectory shows no concrete analysis output"],
        "supporting_facts": [],
        "fabrication_indicators": [],
        "missing_evidence": ["no concrete analysis output tying back to a claimed result"],
        "reasoning": "Task classification: analysis. Trajectory has one ls; "
                     "claim says 'analysis is complete' without specifics.",
    })

    _, gate_result = _run_gate(
        session,
        task_content="run the analysis",
        task_result="the analysis is complete",
        verdict_json=verdict,
    )

    assert gate_result["tasks_verified"] == 0
    assert gate_result["tasks_failed"] == 1  # insufficient counts as failure today
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    assert ev["verdict"] == "insufficient"
    assert ev["confidence"] == 0.4


def test_verified_verdict_with_supporting_facts(session: ProvenanceLog):
    """Successful fetch trajectory: external web_fetch returning a real
    body; claim cites entries verbatim. Verdict: verified with
    supporting_facts referencing tool_call ids."""
    body = json.dumps({"entries": [{"id": 1}, {"id": 2}, {"id": 3}]})
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "fetch-1", "tool_name": "web",
         "arguments": {"command": "fetch", "url": "https://api.example.org/peptides"},
         "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "fetch-1", "tool_name": "web",
         "success": True, "output_summary": body, "error": None, "duration_ms": 320,
         "cost_usd": 0.0, "cost_kind": "llm",
         "tokens_in": None, "tokens_out": None, "model": None},
    ])

    verdict = json.dumps({
        "verdict": "verified",
        "confidence": 0.9,
        "issues": [],
        "supporting_facts": [
            "tool_call_id=fetch-1 hit https://api.example.org/peptides; body has 3 entries"
        ],
        "fabrication_indicators": [],
        "missing_evidence": [],
        "reasoning": "Task: data_acquisition. External fetch returned the claimed body.",
    })

    _, gate_result = _run_gate(
        session,
        task_content="fetch peptide entries",
        task_result="fetched 3 entries",
        verdict_json=verdict,
    )

    assert gate_result["tasks_verified"] == 1
    assert gate_result["tasks_failed"] == 0
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    assert ev["verdict"] == "verified"
    assert any("fetch-1" in s for s in ev["evidence"]["supporting_facts"])


def test_tool_result_mismatch_refuted(session: ProvenanceLog):
    """bash(curl) returned {count:5} but claim says 10. Refuted with issues
    citing the mismatch."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "c1", "tool_name": "bash",
         "arguments": {"command": "curl https://api.example.org/count"},
         "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "c1", "tool_name": "bash",
         "success": True, "output_summary": '{"count": 5}', "error": None,
         "duration_ms": 200, "cost_usd": None, "cost_kind": None,
         "tokens_in": None, "tokens_out": None, "model": None},
    ])

    verdict = json.dumps({
        "verdict": "refuted",
        "confidence": 0.95,
        "issues": ["tool_call_id=c1 returned count=5; claim says 10"],
        "supporting_facts": [],
        "fabrication_indicators": [
            "tool-result mismatch: trajectory shows 5, claim asserts 10"
        ],
        "missing_evidence": [],
        "reasoning": "Task: data_acquisition. Trajectory contradicts the claim.",
    })

    _, _ = _run_gate(
        session,
        task_content="fetch entry count from the API",
        task_result="fetched 10 entries",
        verdict_json=verdict,
    )
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    assert ev["verdict"] == "refuted"
    assert any("mismatch" in s for s in ev["evidence"]["fabrication_indicators"])


def test_compute_task_fabrication_refuted_with_two_patterns(session: ProvenanceLog):
    """Task-type-independent check: compute/simulation task. Agent claims
    a 4h runtime and a sim_results.json output, but trajectory has no
    compute_run and the file came from inline bash. Verdict: refuted with
    BOTH the missing-compute-step and the self-write flagged. Demonstrates
    the prompt isn't DBAASP-tuned."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "w1", "tool_name": "bash",
         "arguments": {"command": "python -c 'import json; json.dump({\"runtime\": \"4h\"}, open(\"sim_results.json\",\"w\"))'"},
         "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "w1", "tool_name": "bash",
         "success": True, "output_summary": "", "error": None, "duration_ms": 3,
         "cost_usd": None, "cost_kind": None,
         "tokens_in": None, "tokens_out": None, "model": None},
    ])

    verdict = json.dumps({
        "verdict": "refuted",
        "confidence": 0.95,
        "issues": [
            "no compute_run / compute_exec for the claimed simulation",
            "sim_results.json was written inline by tool_call_id=w1, not by a real run",
        ],
        "supporting_facts": [],
        "fabrication_indicators": [
            "missing required step: compute task with no compute_run or compute_cost_observed",
            "self-write-then-cite: tool_call_id=w1 wrote sim_results.json with literal content",
        ],
        "missing_evidence": ["compute_cost_observed event", "compute_run tool_call"],
        "reasoning": "Task classification: compute_or_simulation. Trajectory has neither "
                     "a cluster job nor a compute_cost_observed event; the cited output "
                     "file is an inline self-write.",
    })

    _, _ = _run_gate(
        session,
        task_content="build the model and run the simulation",
        task_result="simulation complete; ran 4h; sim_results.json written",
        verdict_json=verdict,
    )
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    fab = ev["evidence"]["fabrication_indicators"]
    assert any("self-write" in s for s in fab)
    assert any("compute" in s and ("missing" in s or "no compute" in s) for s in fab)


def test_scope_downgrade_silent_substitution_refuted(session: ProvenanceLog):
    """Pattern 8 (scope downgrade). A real cluster job ran — no
    missing-step issue, no self-write, compute_run + compute_cost_observed
    are both present — but the `tool_call.arguments` narrow scope on
    several axes at once (mode, resolution/convergence, scale) compared
    to the claim. The verifier must compare claim text against
    `tool_call.arguments`, not just outputs.

    Fixture is a generic scientific cluster job (solver / sweep / inference
    over an ensemble) so the pattern isn't anchored on any one science.
    The same indicator shape applies to a coarse-mesh CFD run claiming
    fine-grid convergence, a 2-epoch training run claiming "trained to
    convergence", a 10-sample inference run claiming "ran on the full
    ensemble", or a parameter sweep that ran one configuration but claimed
    all."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "run-1", "tool_name": "compute_run",
         "arguments": {
             "command": "python run.py --mode=test --max-iterations 50 "
                        "--tolerance 1e-2 --ensemble-size 10",
             "cluster_name": "sci-small",
             "resources": {"cpus": 4, "nodes": 1},
         },
         "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "run-1", "tool_name": "compute_run",
         "success": True,
         "output_summary": "run finished: 10 ensemble members, "
                           "stopped at iteration 50 (tolerance 1e-2 reached)",
         "error": None, "duration_ms": 60000,
         "cost_usd": 0.05, "cost_kind": "compute",
         "tokens_in": None, "tokens_out": None, "model": None},
        {"event_kind": "compute_cost_observed", "cluster_name": "sci-small",
         "cost_usd": 0.05, "source": "sky_cost_report"},
    ])

    verdict = json.dumps({
        "verdict": "refuted",
        "confidence": 0.9,
        "issues": [
            "tool_call_id=run-1 ran with --mode=test, --max-iterations 50, "
            "--tolerance 1e-2, --ensemble-size 10 on a single node; claim asserts "
            "a production run at full convergence over the full ensemble on the "
            "requested cluster",
        ],
        "supporting_facts": [
            "compute_cost_observed confirms a real cluster job ran",
        ],
        "fabrication_indicators": [
            "scope downgrade: tool_call.arguments show --mode=test (mode), "
            "tolerance=1e-2 with iteration cap=50 (resolution / convergence), "
            "ensemble-size=10 and nodes=1 (scale); claim asserts full convergence "
            "over the full ensemble on the requested cluster",
        ],
        "missing_evidence": [
            "no compute_run in production mode at the requested tolerance, "
            "ensemble size, and parallelism",
        ],
        "reasoning": "Task: scientific cluster run. A real job completed at "
                     "downgraded scope on three axes (mode, resolution, scale); "
                     "the run that finished is not the run that was claimed.",
    })

    _, gate_result = _run_gate(
        session,
        task_content="run the production job at full convergence over the full "
                     "ensemble on the requested cluster",
        task_result="run complete; converged at full resolution over the full ensemble",
        verdict_json=verdict,
    )
    assert gate_result["tasks_failed"] == 1
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    assert ev["verdict"] == "refuted"
    fab = ev["evidence"]["fabrication_indicators"]
    assert any("scope downgrade" in s.lower() for s in fab)
    # The point of this fixture: this pattern is NOT caught by any of the
    # earlier indicators (no self-write, no missing-required-step — the
    # compute_run and compute_cost_observed are both present).
    assert not any("self-write" in s.lower() for s in fab)
    assert not any("missing required step" in s.lower() for s in fab)


def test_form_only_response_insufficient(session: ProvenanceLog):
    """Two tool calls, one failure, polished 'I cannot' answer for a
    plausibly-pursuable task. Verifier returns insufficient with low effort
    cited in issues."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "b1", "tool_name": "bash",
         "arguments": {"command": "cargo build"}, "arguments_sha256": "x"},
        {"event_kind": "tool_result", "tool_call_id": "b1", "tool_name": "bash",
         "success": False, "output_summary": "error[E0425]: cannot find value",
         "error": "build failed", "duration_ms": 1500,
         "cost_usd": None, "cost_kind": None,
         "tokens_in": None, "tokens_out": None, "model": None},
        {"event_kind": "tool_call", "tool_call_id": "b2", "tool_name": "bash",
         "arguments": {"command": "cargo --version"}, "arguments_sha256": "y"},
        {"event_kind": "tool_result", "tool_call_id": "b2", "tool_name": "bash",
         "success": True, "output_summary": "cargo 1.75.0", "error": None,
         "duration_ms": 50, "cost_usd": None, "cost_kind": None,
         "tokens_in": None, "tokens_out": None, "model": None},
    ])

    verdict = json.dumps({
        "verdict": "insufficient",
        "confidence": 0.6,
        "issues": [
            "two tool calls and one failure does not exhaust plausible fixes for E0425",
            "form-only refusal without exploring the build error",
        ],
        "supporting_facts": [],
        "fabrication_indicators": [],
        "missing_evidence": ["no attempt to read the source file that triggered E0425"],
        "reasoning": "Task: code_execution. Trajectory shows trivial effort relative to "
                     "the task difficulty; verdict is insufficient.",
    })

    _, gate_result = _run_gate(
        session,
        task_content="fix the build error and get cargo build passing",
        task_result="I cannot do this, recommend manual workflow",
        verdict_json=verdict,
    )
    assert gate_result["tasks_failed"] == 1
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    assert ev["verdict"] == "insufficient"


# ---------------------------------------------------------------------------
# Regression: allowed_tools / schema compatibility
# ---------------------------------------------------------------------------


def test_verifier_subagent_tool_surface_unchanged():
    """Regression: the verifier's allowed_tools must remain file_ops /
    search / bash so it can open the session log + grep it."""
    from sciagent.subagent import SubAgentOrchestrator

    orch = SubAgentOrchestrator(working_dir=".")
    cfg = orch.registry.get("verifier")
    assert cfg is not None
    assert set(cfg.allowed_tools) == {"file_ops", "search", "bash"}
    assert cfg.temperature == 0.0
    assert cfg.max_iterations == 20


def test_verification_result_event_shape_compatible(session: ProvenanceLog):
    """Schema regression: every field downstream readers depend on
    (verdict, confidence, issues, evidence.reasoning, evidence.supporting_facts,
    evidence.fabrication_indicators) is still present after the rewrite."""
    _write_trajectory(session, [
        {"event_kind": "tool_call", "tool_call_id": "t1", "tool_name": "bash",
         "arguments": {"command": "true"}, "arguments_sha256": "x"},
    ])
    verdict = json.dumps({
        "verdict": "verified",
        "confidence": 0.8,
        "issues": [],
        "supporting_facts": ["seq 1: bash true succeeded"],
        "fabrication_indicators": [],
        "missing_evidence": [],
        "reasoning": "trivial verified case",
    })
    _, _ = _run_gate(
        session, task_content="run true", task_result="ok", verdict_json=verdict,
    )
    ev = [e for e in _events(session, "verification_result") if e["gate"] == "llm"][0]
    for key in ("verdict", "confidence", "issues", "evidence", "verifier", "claim"):
        assert key in ev, f"missing top-level key: {key}"
    for key in ("reasoning", "supporting_facts", "fabrication_indicators", "missing_evidence"):
        assert key in ev["evidence"], f"missing evidence sub-key: {key}"
    # New audit-grade field: the session log path that the verifier was
    # pointed at. Downstream consumers can use this to re-open the same log.
    assert ev["evidence"]["session_log"] == str(session.path)
