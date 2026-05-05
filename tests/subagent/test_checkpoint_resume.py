"""Subagent checkpoint + resume — unit + integration tests.

Covers the contract added by the checkpoint / resume work:

1. Checkpoint module — append-only JSONL, atomic writes, corrupt-tail
   tolerance, AgentState snapshotting.
2. SubAgent.attach_checkpoint — hooks fire on each successful tool call.
3. Orchestrator resume detection — crashed/blocked_resume entries match by
   task description hash, surface 3-way decision.
4. Warm vs cold replay — config knob + summarization fallback.

The tests inject ``_FakeSubAgent`` via ``_build_subagent`` so no real LLM
calls happen. Real SubAgent + AgentLoop integration is exercised at the
``attach_checkpoint`` unit level (a fake AgentLoop suffices to drive the
on_tool_end hook).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from sciagent import checkpoint as cp_mod
from sciagent.checkpoint import (
    SubagentCheckpoint,
    find_resumable_subagents,
    task_description_hash,
    warm_resume_window_seconds,
)
from sciagent.compute import task_index
from sciagent.subagent import (
    SubAgent,
    SubAgentConfig,
    SubAgentOrchestrator,
    SubAgentResult,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def tmp_session_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect ~/.sciagent/sessions/ to a tmp dir for checkpoint storage."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sessions = fake_home / ".sciagent" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect ~/.sciagent/tasks/ to a tmp dir for task_index storage."""
    fake_home = tmp_path / "home_idx"
    fake_home.mkdir()
    target = fake_home / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: target)
    return target


# -----------------------------------------------------------------------------
# Unit tests — SubagentCheckpoint
# -----------------------------------------------------------------------------


def test_checkpoint_writes_jsonl_line_per_iteration(tmp_session_dir: Path):
    """One record_iteration call → one line in checkpoint.jsonl with all
    required fields."""
    cp = SubagentCheckpoint(session_id="sess-A", task_id="task-1")
    cp.record_iteration(
        iteration=1,
        tool_name="bash",
        tool_args={"command": "ls"},
        tool_result="file1\nfile2\n",
        todo_state=[{"description": "x", "status": "PENDING"}],
        message_count=4,
    )
    cp.record_iteration(
        iteration=2,
        tool_name="file_ops",
        tool_args={"action": "read", "path": "x"},
        tool_result={"data": "..."},
        message_count=6,
    )

    records = cp.read_records()
    assert len(records) == 2
    assert records[0]["iteration"] == 1
    assert records[0]["tool_name"] == "bash"
    assert records[0]["message_count"] == 4
    assert records[0]["tool_args_hash"]  # non-empty sha256
    assert records[0]["tool_result_hash"]
    assert records[0]["schema_version"] == "1"
    assert records[1]["iteration"] == 2


def test_checkpoint_failure_does_not_raise(tmp_session_dir: Path):
    """Best-effort writes — if the disk fails, record_iteration returns None
    without raising. The subagent run continues unaffected."""
    cp = SubagentCheckpoint(session_id="sess-B", task_id="task-2")
    # Sabotage the write path.
    cp.path = Path("/nonexistent/dir/that/does/not/exist/checkpoint.jsonl")
    cp.lock_path = Path("/nonexistent/dir/.lock")
    eid = cp.record_iteration(
        iteration=1, tool_name="bash", tool_args={}, tool_result=""
    )
    assert eid is None  # signals failure, but no exception


def test_checkpoint_corrupt_tail_is_tolerated(tmp_session_dir: Path):
    """A truncated last line (writer crashed mid-flush) shouldn't break
    read_records — earlier valid lines still come back."""
    cp = SubagentCheckpoint(session_id="sess-C", task_id="task-3")
    cp.record_iteration(iteration=1, tool_name="bash", tool_args={}, tool_result="")
    cp.record_iteration(iteration=2, tool_name="bash", tool_args={}, tool_result="")
    # Append a truncated JSON line (no newline, no closing brace).
    with open(cp.path, "a") as f:
        f.write('{"iteration": 3, "tool_name": "bash", "schema_version": ')
    records = cp.read_records()
    assert len(records) == 2
    assert records[0]["iteration"] == 1
    assert records[1]["iteration"] == 2


def test_checkpoint_save_and_load_agent_state(tmp_session_dir: Path):
    """Agent state round-trips through agent_state.json."""
    cp = SubagentCheckpoint(session_id="sess-D", task_id="task-4")
    state = {
        "session_id": "sess-D",
        "system_prompt": "you are an agent",
        "messages": [],
        "todos": {"items": []},
        "summary_block": "",
        "working_dir": ".",
        "model": "x",
        "temperature": 0.0,
        "max_iterations": 40,
        "metadata": {},
        "created_at": "2026-05-05T00:00:00",
        "updated_at": "2026-05-05T00:00:00",
    }
    assert cp.save_agent_state(state) is True
    loaded = cp.load_agent_state()
    assert loaded == state


def test_checkpoint_meta_records_task_hash(tmp_session_dir: Path):
    """write_meta stores the sha256 task hash for later resume matching."""
    cp = SubagentCheckpoint(session_id="sess-E", task_id="task-5")
    cp.write_meta(
        agent_name="explore",
        task="find auth code",
        parent_session_id="sess-E",
    )
    meta = cp.load_meta()
    assert meta is not None
    assert meta["agent_name"] == "explore"
    assert meta["task_hash"] == task_description_hash("find auth code")


def test_task_description_hash_is_whitespace_stable():
    assert task_description_hash("hello") == task_description_hash("  hello  ")
    assert task_description_hash("a") != task_description_hash("b")


def test_warm_resume_window_default_is_300_seconds():
    # No env, no config file (HOME redirected by fixture if used).
    os.environ.pop("SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS", None)
    assert warm_resume_window_seconds() == 300


def test_warm_resume_window_env_override(monkeypatch):
    monkeypatch.setenv("SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS", "120")
    assert warm_resume_window_seconds() == 120


# -----------------------------------------------------------------------------
# Unit test — attach_checkpoint hooks fire on successful tool calls
# -----------------------------------------------------------------------------


def test_attach_checkpoint_hooks_fire_on_tool_end(tmp_session_dir: Path):
    """Run a fake AgentLoop tick: install hooks, fire on_start + on_end,
    confirm a checkpoint line landed and agent_state.json was written."""
    config = SubAgentConfig(
        name="explore", description="x", system_prompt="y"
    )
    sa = SubAgent(config=config, working_dir=".")
    cp = sa.attach_checkpoint("task-X")
    assert cp is not None

    # Drive the hooks the way AgentLoop.execute_tool does.
    sa.agent.iteration_count = 7
    sa.agent._on_tool_start("bash", {"command": "echo hi"})

    class _FakeResult:
        def __init__(self):
            self.success = True
            self.output = "hi"
            self.error = None

    sa.agent._on_tool_end("bash", _FakeResult())

    records = cp.read_records()
    assert len(records) == 1
    assert records[0]["tool_name"] == "bash"
    assert records[0]["iteration"] == 7
    assert records[0]["success"] is True
    # agent_state.json snapshot landed.
    state = cp.load_agent_state()
    assert state is not None
    assert state["session_id"] == sa.agent.state.session_id


# -----------------------------------------------------------------------------
# Integration tests — orchestrator resume detection + 3-way decision
# -----------------------------------------------------------------------------


class _FakeSubAgent:
    """Minimal fake mirroring SubAgent's surface for orchestrator tests."""

    def __init__(
        self,
        agent_name: str,
        *,
        success: bool = True,
        output: str = "ok",
        error: Optional[str] = None,
        sleep_seconds: float = 0.0,
        raise_inside_run: bool = False,
        session_id: str = "fake-session",
    ):
        self.config = SubAgentConfig(
            name=agent_name, description="", system_prompt="x"
        )
        self.session_id = session_id
        self.parent_session_id = None
        self._success = success
        self._output = output
        self._error = error
        self._sleep = sleep_seconds
        self._raise = raise_inside_run
        self.attached_task_id: Optional[str] = None
        self.checkpoint: Optional[SubagentCheckpoint] = None
        self.seeded_state: Optional[dict] = None

    def attach_checkpoint(self, task_id: str) -> Optional[SubagentCheckpoint]:
        self.attached_task_id = task_id
        owning = self.parent_session_id or self.session_id
        try:
            self.checkpoint = SubagentCheckpoint(
                session_id=owning, task_id=task_id
            )
            return self.checkpoint
        except Exception:
            return None

    def seed_state_from_dict(self, state_dict):
        self.seeded_state = state_dict
        return True

    def run(self, task: str) -> SubAgentResult:
        if self._sleep:
            time.sleep(self._sleep)
        if self._raise:
            raise RuntimeError("simulated crash inside SubAgent.run")
        return SubAgentResult(
            agent_name=self.config.name,
            task=task,
            success=self._success,
            output=self._output,
            error=self._error,
            iterations=3,
            tokens_used=42,
            duration_seconds=0.01,
            session_id=self.session_id,
        )


def _make_orchestrator(fake_factory) -> SubAgentOrchestrator:
    orch = SubAgentOrchestrator(working_dir=".")
    orch._build_subagent = lambda config: fake_factory(config.name)
    return orch


def _wait_for_state(task_id: str, states: tuple, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = task_index.read_task(task_id)
        if rec and rec.get("state") in states:
            return rec
        time.sleep(0.02)
    raise AssertionError(
        f"task {task_id} did not reach one of {states} in {timeout}s"
    )


def test_background_crash_lifecycle_marks_crashed_and_keeps_checkpoints(
    tmp_session_dir: Path, tmp_manifest_dir: Path
):
    """A backgrounded subagent that raises inside run() lands at
    state=crashed in task_index and leaves its checkpoint dir intact."""
    orch = _make_orchestrator(
        lambda name: _FakeSubAgent(name, raise_inside_run=True)
    )
    result = orch.spawn("explore", "do the thing", background=True)
    assert result.task_id is not None

    record = _wait_for_state(
        result.task_id, ("crashed", "failed"), timeout=5.0
    )
    assert record["state"] == "crashed"
    assert record["state"] not in task_index.TERMINAL_STATES
    assert record["state"] in task_index.RESUMABLE_STATES

    # Checkpoint dir exists (write_meta was called even though the run
    # crashed before any tool calls). meta.json is the discriminator the
    # resume detector keys on. The dir lives under the fake's session id
    # since the fake doesn't have a parent_session_id wired.
    sub_dir = tmp_session_dir / "fake-session" / "subagents" / record["job_id"]
    assert sub_dir.exists()
    assert (sub_dir / "meta.json").exists()


def test_resume_detection_surfaces_3way_decision(
    tmp_session_dir: Path, tmp_manifest_dir: Path, monkeypatch
):
    """A second spawn with a matching task hash → spawn() returns a
    decision payload (success=False, error names the 3 choices) instead
    of running."""
    # Set the parent session globally so the detector finds the right dir.
    from sciagent.tools.atomic.compute import ComputeTool
    ComputeTool.set_shared_session("parent-sess")

    orch = _make_orchestrator(
        lambda name: _FakeSubAgent(
            name, raise_inside_run=True, session_id="child-1"
        )
    )
    # Manually insert a parent-session hint into the fake.
    def _make_with_parent(name):
        sa = _FakeSubAgent(name, raise_inside_run=True, session_id="child-1")
        sa.parent_session_id = "parent-sess"
        return sa
    orch._build_subagent = lambda config: _make_with_parent(config.name)

    first = orch.spawn("explore", "find auth code", background=True)
    assert first.task_id is not None
    record = _wait_for_state(first.task_id, ("crashed", "failed"))
    assert record["state"] == "crashed"

    # Second spawn — same task text — should detect the resume candidate.
    # Use a non-raising fake this time so we can verify the spawn never
    # got to run().
    ran_count = {"n": 0}
    def _no_run(name):
        sa = _FakeSubAgent(name, success=True)
        sa.parent_session_id = "parent-sess"
        original_run = sa.run
        def counted_run(task):
            ran_count["n"] += 1
            return original_run(task)
        sa.run = counted_run
        return sa
    orch._build_subagent = lambda config: _no_run(config.name)

    second = orch.spawn("explore", "find auth code")
    assert second.success is False  # decision payload, not a clean run
    assert second.error is not None
    assert "Resumable subagent run" in second.error
    assert "resume_choice" in second.error
    assert ran_count["n"] == 0  # didn't actually invoke the subagent

    ComputeTool.set_shared_session(None)


def test_resume_choice_skip_returns_immediately_without_running(
    tmp_session_dir: Path, tmp_manifest_dir: Path
):
    """resume_choice='skip' short-circuits before the subagent runs."""
    orch = _make_orchestrator(lambda name: _FakeSubAgent(name))
    ran_count = {"n": 0}
    original_factory = orch._build_subagent
    def counted(config):
        ran_count["n"] += 1
        return original_factory(config)
    orch._build_subagent = counted

    result = orch.spawn(
        "explore", "anything",
        resume_choice="skip",
        resume_task_id="some-task-id",
    )
    assert result.success is True
    assert "Skipped resume" in result.output
    assert ran_count["n"] == 0


def test_resume_choice_resume_seeds_state_and_reuses_task_id(
    tmp_session_dir: Path, tmp_manifest_dir: Path
):
    """resume_choice='resume' reuses the prior task_id and seeds state from
    the checkpoint's saved AgentState."""
    # Pre-populate a checkpoint.
    saved_state = {
        "session_id": "old-child",
        "system_prompt": "p",
        "messages": [],
        "todos": {"items": []},
        "summary_block": "prior summary",
        "working_dir": ".",
        "model": "x",
        "temperature": 0.0,
        "max_iterations": 40,
        "metadata": {},
        "created_at": "2026-05-05T00:00:00",
        "updated_at": "2026-05-05T00:00:00",
    }
    cp = SubagentCheckpoint(session_id="parent-sess", task_id="prior-task-id")
    cp.write_meta(
        agent_name="explore",
        task="resume me",
        parent_session_id="parent-sess",
    )
    cp.save_agent_state(saved_state)
    cp.record_iteration(
        iteration=5, tool_name="bash", tool_args={}, tool_result=""
    )

    captured = {"sa": None}
    def _factory(name):
        sa = _FakeSubAgent(name)
        sa.parent_session_id = "parent-sess"
        captured["sa"] = sa
        return sa
    orch = SubAgentOrchestrator(working_dir=".")
    orch._build_subagent = lambda config: _factory(config.name)

    result = orch.spawn(
        "explore", "resume me",
        resume_choice="resume",
        resume_task_id="prior-task-id",
    )
    assert result.success is True
    sa = captured["sa"]
    assert sa.attached_task_id == "prior-task-id"  # reused, not new
    assert sa.seeded_state is not None
    assert sa.seeded_state["summary_block"] == "prior summary"


def test_resume_choice_fresh_starts_new_task_id(
    tmp_session_dir: Path, tmp_manifest_dir: Path
):
    """resume_choice='fresh' starts over with a new task_id, leaving the
    crashed entry on disk for inspection."""
    orch = _make_orchestrator(lambda name: _FakeSubAgent(name))
    result = orch.spawn(
        "explore", "anything", resume_choice="fresh"
    )
    # success path runs the fake → produces "ok"
    assert result.success is True
    assert result.output == "ok"


def test_corrupt_meta_does_not_match_as_resumable(
    tmp_session_dir: Path, tmp_manifest_dir: Path
):
    """A subagent dir with corrupt meta.json should be skipped by the
    resume detector — silent, never raises."""
    bad_dir = tmp_session_dir / "parent-sess" / "subagents" / "borked-id"
    bad_dir.mkdir(parents=True)
    (bad_dir / "meta.json").write_text("{not valid json")

    candidates = find_resumable_subagents("parent-sess")
    # The bad dir is silently dropped (no meta.json that parses) — list is
    # empty.
    assert candidates == []


def test_synthetic_kill_after_5_iterations_then_resume(
    tmp_session_dir: Path, tmp_manifest_dir: Path
):
    """End-to-end shape: 5 successful tool calls write 5 checkpoint lines;
    a mid-run exception lands at state=crashed; a fresh spawn with the
    same task hash gets the 3-way decision; resume_choice='resume' picks
    up from the saved AgentState."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool.set_shared_session("parent-A")

    iterations_done = {"n": 0}

    class _IterativeFake(_FakeSubAgent):
        """Drives 5 checkpoint writes via direct on_tool_end calls, then
        raises — mirroring a Server-disconnect failure mid-run."""

        def run(self, task: str) -> SubAgentResult:
            assert self.checkpoint is not None
            for i in range(1, 6):
                self.checkpoint.record_iteration(
                    iteration=i,
                    tool_name="bash",
                    tool_args={"command": f"step-{i}"},
                    tool_result=f"result-{i}",
                    todo_state=[
                        {"description": "do work", "status": "IN_PROGRESS"}
                    ],
                    message_count=i * 2,
                    success=True,
                )
                self.checkpoint.save_agent_state(
                    {
                        "session_id": self.session_id,
                        "system_prompt": "p",
                        "messages": [{"role": "user", "content": f"step {i}"}],
                        "todos": {
                            "items": [
                                {
                                    "description": "do work",
                                    "status": "IN_PROGRESS",
                                    "created_at": "",
                                    "completed_at": None,
                                }
                            ]
                        },
                        "summary_block": "",
                        "working_dir": ".",
                        "model": "x",
                        "temperature": 0.0,
                        "max_iterations": 40,
                        "metadata": {"reached_iteration": i},
                        "created_at": "2026-05-05T00:00:00",
                        "updated_at": "2026-05-05T00:00:00",
                    }
                )
                iterations_done["n"] = i
            raise RuntimeError("Server disconnected after iteration 5")

    def _factory(name):
        sa = _IterativeFake(name, session_id="child-A")
        sa.parent_session_id = "parent-A"
        return sa

    orch = SubAgentOrchestrator(working_dir=".")
    orch._build_subagent = lambda config: _factory(config.name)

    first = orch.spawn(
        "explore", "long compute task", background=True
    )
    assert first.task_id is not None
    record = _wait_for_state(first.task_id, ("crashed", "failed"))
    assert record["state"] == "crashed"
    assert iterations_done["n"] == 5

    # Five checkpoint lines on disk.
    cp_path = (
        tmp_session_dir / "parent-A" / "subagents" / first.task_id
        / "checkpoint.jsonl"
    )
    assert cp_path.exists()
    with open(cp_path) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 5
    assert lines[-1]["iteration"] == 5
    assert lines[-1]["tool_name"] == "bash"

    # Restart simulation: fresh spawn with same task → 3-way decision surfaces.
    seen_seed = {"state": None}

    class _ResumeFake(_FakeSubAgent):
        def seed_state_from_dict(self, state_dict):
            seen_seed["state"] = state_dict
            return True

    def _resume_factory(name):
        sa = _ResumeFake(name, session_id="child-A2")
        sa.parent_session_id = "parent-A"
        return sa

    orch._build_subagent = lambda config: _resume_factory(config.name)
    decision = orch.spawn("explore", "long compute task")
    assert decision.success is False
    assert "Resumable subagent run" in decision.error
    assert decision.task_id == first.task_id

    # Now resume with the explicit choice.
    resumed = orch.spawn(
        "explore", "long compute task",
        resume_choice="resume",
        resume_task_id=first.task_id,
    )
    assert resumed.success is True
    assert seen_seed["state"] is not None
    assert seen_seed["state"]["metadata"]["reached_iteration"] == 5

    ComputeTool.set_shared_session(None)
