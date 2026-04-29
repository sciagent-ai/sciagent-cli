"""Tests for verification_result + artifact_produced emission from
ProvenanceChecker (DATA / EXEC gates) and the orchestrator (LLM gate).

The active-session register lets these layers find the right log without
plumbing session_id through their constructors. set_active_session is
called in each test to point at the per-test isolated singleton.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from sciagent.orchestrator import OrchestratorConfig, TaskOrchestrator
from sciagent.provenance import ProvenanceChecker
from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
    set_active_session,
)
from sciagent.tools.atomic.todo import TodoTool


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    set_active_session(None)
    yield
    reset_provenance_logs()
    set_active_session(None)


@pytest.fixture
def session_log(tmp_path: Path) -> ProvenanceLog:
    log = get_provenance_log("verifsess", base_dir=tmp_path)
    set_active_session("verifsess")
    return log


def _events(log: ProvenanceLog, kind: Optional[str] = None) -> list[dict]:
    raw = [json.loads(line) for line in log.path.read_text().splitlines() if line.strip()]
    if kind:
        return [e for e in raw if e["event_kind"] == kind]
    return raw


# ---------------------------------------------------------------------------
# DATA gate (verify_data_acquisition) — verification_result + artifact_produced
# ---------------------------------------------------------------------------


def test_verify_data_acquisition_emits_verified_when_file_valid(
    tmp_path: Path, session_log: ProvenanceLog
):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b,c\n1,2,3\n4,5,6\n")

    checker = ProvenanceChecker()
    checker.verify_data_acquisition(
        local_file=str(csv_path),
        expected_type="csv",
        task_id="t-data-1",
    )

    verifs = _events(session_log, "verification_result")
    arts = _events(session_log, "artifact_produced")

    assert len(verifs) == 1
    v = verifs[0]
    assert v["gate"] == "data"
    assert v["task_id"] == "t-data-1"
    assert v["verdict"] == "verified"
    assert v["verifier"] == "provenance_checker"
    assert v["claim"]["kind"] == "data_acquisition"
    assert v["claim"]["file_path"] == str(csv_path)

    assert len(arts) == 1
    assert arts[0]["path"] == str(csv_path.resolve())
    assert arts[0]["size_bytes"] == csv_path.stat().st_size


def test_verify_data_acquisition_emits_refuted_on_missing_file(
    tmp_path: Path, session_log: ProvenanceLog
):
    checker = ProvenanceChecker()
    checker.verify_data_acquisition(local_file=str(tmp_path / "ghost.csv"))

    verifs = _events(session_log, "verification_result")
    arts = _events(session_log, "artifact_produced")
    assert len(verifs) == 1
    assert verifs[0]["verdict"] == "refuted"
    assert any(i["category"] == "file_not_found" for i in verifs[0]["issues"])
    # No artifact emission when verification fails
    assert arts == []


def test_no_active_session_means_no_emission(tmp_path: Path):
    """ProvenanceChecker must skip emission silently when no session is set,
    so non-agent test paths don't pollute the user's home dir."""
    set_active_session(None)
    csv = tmp_path / "x.csv"
    csv.write_text("a,b\n1,2\n")
    ProvenanceChecker().verify_data_acquisition(local_file=str(csv), expected_type="csv")
    # Nothing landed in tmp_path/verifsess (we never even created that singleton)
    assert not (tmp_path / "verifsess" / "provenance.jsonl").exists()


# ---------------------------------------------------------------------------
# EXEC gate (verify_execution / verify_tests_ran)
# ---------------------------------------------------------------------------


def test_verify_execution_emits_verification_result(monkeypatch, session_log):
    """verify_execution returns through multiple early-return paths; the
    finally-wrapped emit must fire on every one."""
    fake_exec_logger = MagicMock()
    fake_exec_logger.find_execution.return_value = []

    checker = ProvenanceChecker()
    checker.exec_logger = fake_exec_logger
    checker.verify_execution(claimed_command="pytest", task_id="t-exec-1")

    verifs = _events(session_log, "verification_result")
    assert len(verifs) == 1
    v = verifs[0]
    assert v["gate"] == "exec"
    assert v["task_id"] == "t-exec-1"
    assert v["verdict"] == "refuted"
    assert v["claim"]["kind"] == "execution"
    assert v["claim"]["claimed_command"] == "pytest"
    assert v["verifier"] == "provenance_checker"


def test_verify_execution_no_command_still_emits(session_log: ProvenanceLog):
    """Early no-command return must also emit a verification_result."""
    ProvenanceChecker().verify_execution(claimed_command=None)
    verifs = _events(session_log, "verification_result")
    assert len(verifs) == 1
    assert verifs[0]["verdict"] == "refuted"


def test_verify_tests_ran_emits_verification_result(session_log: ProvenanceLog):
    fake_exec_logger = MagicMock()
    fake_exec_logger.get_verification_runs.return_value = [
        {"command": "pytest -q", "success": True, "is_verification": True},
        {"command": "pytest -k foo", "success": True, "is_verification": True},
    ]
    checker = ProvenanceChecker()
    checker.exec_logger = fake_exec_logger
    checker.verify_tests_ran(task_id="t-tests-1")

    verifs = _events(session_log, "verification_result")
    assert len(verifs) == 1
    v = verifs[0]
    assert v["gate"] == "exec"
    assert v["claim"] == {"kind": "tests_ran"}
    assert v["task_id"] == "t-tests-1"
    assert v["verdict"] == "verified"


# ---------------------------------------------------------------------------
# LLM gate (orchestrator._run_llm_verification_gate)
# ---------------------------------------------------------------------------


def test_llm_gate_emits_verification_result_per_task(session_log: ProvenanceLog):
    todo = TodoTool()
    todo.execute(todos=[
        {"id": "t1", "content": "produce final answer", "task_type": "output",
         "depends_on": [], "result_key": "t1", "priority": "medium", "can_parallel": False,
         "status": "completed"},
    ])

    # Stub subagent: always returns a JSON-encoded "verified" verdict
    class _FakeSubagent:
        llm = MagicMock(model="gpt-4o-mini")

        def spawn(self, _agent_name, _prompt):
            return MagicMock(
                success=True,
                output=json.dumps({
                    "verdict": "verified",
                    "confidence": 0.95,
                    "issues": [],
                    "supporting_facts": ["the file exists with expected shape"],
                }),
                error=None,
                iterations=1,
            )

    config = OrchestratorConfig(verbose=False)
    orch = TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=_FakeSubagent(),
        config=config,
    )
    # Mark the task completed so it's a candidate for the gate
    graph = todo.get_graph()
    for t in graph.get_all():
        t.status = "completed"

    tasks = orch._get_tasks_requiring_verification()
    assert tasks, "test setup expected at least one task to verify"

    orch._run_llm_verification_gate(tasks)

    verifs = _events(session_log, "verification_result")
    llm_events = [v for v in verifs if v["gate"] == "llm"]
    assert len(llm_events) == 1
    ev = llm_events[0]
    assert ev["task_id"] == "t1"
    assert ev["verdict"] == "verified"
    assert ev["confidence"] == 0.95
    assert ev["verifier"] == "gpt-4o-mini"
    assert ev["claim"]["kind"] == "task_outcome"


def test_llm_gate_emits_insufficient_when_subagent_errors(session_log: ProvenanceLog):
    todo = TodoTool()
    todo.execute(todos=[
        {"id": "t1", "content": "deliver report", "task_type": "output",
         "depends_on": [], "result_key": "t1", "priority": "medium", "can_parallel": False,
         "status": "completed"},
    ])

    class _BrokenSubagent:
        llm = MagicMock(model="claude-opus-4-7")

        def spawn(self, *_args, **_kwargs):
            raise RuntimeError("verifier subagent crashed")

    config = OrchestratorConfig(verbose=False)
    orch = TaskOrchestrator(todo_tool=todo, subagent_orchestrator=_BrokenSubagent(), config=config)
    graph = todo.get_graph()
    for t in graph.get_all():
        t.status = "completed"

    orch._run_llm_verification_gate(orch._get_tasks_requiring_verification())

    verifs = _events(session_log, "verification_result")
    llm_events = [v for v in verifs if v["gate"] == "llm"]
    assert len(llm_events) == 1
    assert llm_events[0]["verdict"] == "insufficient"
    assert llm_events[0]["verifier"] == "claude-opus-4-7"
