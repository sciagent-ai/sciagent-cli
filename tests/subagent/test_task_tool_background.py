"""PR4 step 4 — TaskTool exposes background param + emits subagent_* events
from both spawn paths.

Sync path emission was added in M1B but not directly asserted on.
Background path emission is new; the worker thread's on_complete callback
is what closes the subagent_spawned/subagent_completed audit pair.

Tests inject a fake SubAgent via the orchestrator's _build_subagent
factory so no real LLM calls happen.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

from sciagent.compute import task_index
from sciagent import provenance_log
from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
    set_active_session,
)
from sciagent.subagent import (
    SubAgentConfig,
    SubAgentOrchestrator,
    SubAgentResult,
    TaskTool,
)


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = fake_home / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: target)
    return target


@pytest.fixture
def active_session(tmp_path: Path):
    """Set up a fresh provenance log under tmp_path + bind it as the active
    session. Pre-caches the log so get_active_session_log() resolves to our
    tmp-path-rooted log instead of ~/.sciagent/sessions."""
    reset_provenance_logs()
    session_id = "ses-task-tool"
    log = get_provenance_log(session_id, base_dir=tmp_path)
    set_active_session(session_id)
    yield log
    set_active_session(None)
    reset_provenance_logs()


class _FakeSubAgent:
    def __init__(
        self,
        agent_name: str,
        *,
        success: bool = True,
        output: str = "fake output",
        error: Optional[str] = None,
        sleep_seconds: float = 0.0,
    ):
        self.config = SubAgentConfig(
            name=agent_name, description="", system_prompt="x"
        )
        self.session_id = f"child-ses-{agent_name}"
        self._success = success
        self._output = output
        self._error = error
        self._sleep = sleep_seconds

    def run(self, task: str) -> SubAgentResult:
        if self._sleep:
            time.sleep(self._sleep)
        return SubAgentResult(
            agent_name=self.config.name,
            task=task,
            success=self._success,
            output=self._output,
            error=self._error,
            iterations=2,
            tokens_used=33,
            duration_seconds=0.01,
            session_id=self.session_id,
        )


def _make_tool(fake_factory) -> TaskTool:
    orch = SubAgentOrchestrator(working_dir=".")
    orch._build_subagent = lambda config: fake_factory(config.name)
    return TaskTool(orch)


def _read_events(log: ProvenanceLog) -> List[dict]:
    """Slurp all events from the log's JSONL file."""
    path = log.path
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _wait_for_events(log: ProvenanceLog, n: int, timeout: float = 5.0):
    """Poll until at least n events are visible, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = _read_events(log)
        if len(events) >= n:
            return events
        time.sleep(0.02)
    raise AssertionError(
        f"only {len(_read_events(log))} events after {timeout}s, expected ≥{n}"
    )


# ---- schema -----------------------------------------------------------------


def test_task_tool_schema_includes_background_param():
    """The LLM-facing schema must advertise the background parameter; the
    backgrounding feature is invisible to the LLM otherwise."""
    schema = TaskTool(SubAgentOrchestrator()).to_schema()
    props = schema["parameters"]["properties"]
    assert "background" in props
    assert props["background"]["type"] == "boolean"
    # Behavioral guidance lives in the top-level description, NOT in the
    # per-param field (per feedback_concise_param_descriptions.md).
    assert "background" in schema["description"].lower()
    assert "task_wait" in schema["description"]


# ---- sync path emits both events --------------------------------------------


def test_sync_spawn_emits_subagent_spawned_and_completed(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    tool = _make_tool(lambda name: _FakeSubAgent(name, output="sync done"))
    result = tool.execute(agent_name="explore", task="find a thing")
    assert result.success is True
    events = _read_events(active_session)
    kinds = [e["event_kind"] for e in events]
    assert kinds == ["subagent_spawned", "subagent_completed"]
    spawned, completed = events
    assert spawned["subagent_name"] == "explore"
    assert spawned["task_preview"] == "find a thing"
    assert completed["spawn_event_id"] == spawned["event_id"]
    assert completed["success"] is True
    assert completed["child_session_id"] == "child-ses-explore"
    assert completed["iterations"] == 2


def test_sync_spawn_failed_emits_completed_with_error(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    tool = _make_tool(
        lambda name: _FakeSubAgent(
            name, success=False, output="", error="LLM refused"
        )
    )
    result = tool.execute(agent_name="explore", task="anything")
    assert result.success is False
    events = _read_events(active_session)
    kinds = [e["event_kind"] for e in events]
    assert kinds == ["subagent_spawned", "subagent_completed"]
    completed = events[1]
    assert completed["success"] is False
    assert completed["error"] == "LLM refused"


# ---- background path emits both events --------------------------------------


def test_background_spawn_emits_spawned_immediately_and_completed_after_thread(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """spawned event lands at TaskTool.execute time; completed event lands
    after the worker thread finishes — that's what verify_session needs to
    see a closed audit pair for backgrounded subagents."""
    tool = _make_tool(
        lambda name: _FakeSubAgent(name, sleep_seconds=0.1, output="bg done")
    )
    result = tool.execute(
        agent_name="explore", task="find auth", background=True
    )
    assert result.success is True
    assert "backgrounded as task" in result.output

    # Right after execute returns, only subagent_spawned should be visible.
    events_now = _read_events(active_session)
    assert [e["event_kind"] for e in events_now] == ["subagent_spawned"]
    spawn_event_id = events_now[0]["event_id"]

    # Wait for the worker thread to finish — completed event should land.
    events_after = _wait_for_events(active_session, n=2, timeout=5.0)
    kinds = [e["event_kind"] for e in events_after]
    assert kinds == ["subagent_spawned", "subagent_completed"]
    completed = events_after[1]
    assert completed["spawn_event_id"] == spawn_event_id
    assert completed["success"] is True
    assert completed["child_session_id"] == "child-ses-explore"


def test_background_spawn_failed_run_emits_completed_with_error(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    tool = _make_tool(
        lambda name: _FakeSubAgent(
            name,
            success=False,
            output="",
            error="exploded",
            sleep_seconds=0.05,
        )
    )
    result = tool.execute(
        agent_name="explore", task="ignored", background=True
    )
    assert result.success is True  # backgrounding succeeded; the run failed
    events = _wait_for_events(active_session, n=2, timeout=5.0)
    completed = events[1]
    assert completed["success"] is False
    assert completed["error"] == "exploded"


def test_background_completed_event_lands_after_manifest_terminal_state(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """verify_session relies on this ordering: when the completed event is
    visible in the log, the registry already shows the terminal state. The
    on_complete callback fires AFTER _finalize_background writes the
    manifest."""
    tool = _make_tool(
        lambda name: _FakeSubAgent(name, sleep_seconds=0.05, output="ok")
    )
    result = tool.execute(
        agent_name="explore", task="ignored", background=True
    )
    events = _wait_for_events(active_session, n=2, timeout=5.0)
    assert events[1]["event_kind"] == "subagent_completed"
    # By the time the completed event was visible, the manifest state was
    # already terminal — the on_complete callback fires after
    # _finalize_background writes the terminal state.
    rows = task_index.list_tasks(kind="subagent")
    assert len(rows) == 1
    assert rows[0]["state"] in task_index.TERMINAL_STATES
    assert result.success is True


# ---- background interaction with task_wait ---------------------------------


def test_background_spawn_then_task_wait_round_trip(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """End-to-end: TaskTool(background=True) → task_wait(task_id) → terminal
    snapshot in output. This is the LLM workflow the PR enables."""
    from sciagent.tools.atomic.task_tools import TaskWaitTool

    tool = _make_tool(
        lambda name: _FakeSubAgent(
            name, output="all green at line 42", sleep_seconds=0.1
        )
    )
    spawn_result = tool.execute(
        agent_name="explore", task="find auth", background=True
    )
    assert "backgrounded as task" in spawn_result.output

    # Recover the task_id from the registry — only one subagent is in flight.
    rows = task_index.list_tasks(kind="subagent")
    assert len(rows) == 1
    task_id = rows[0]["job_id"]

    wait_result = TaskWaitTool().execute(
        id=task_id, timeout=3.0, poll_interval=0.05
    )
    assert wait_result.success is True
    assert "completed" in wait_result.output.lower()
    assert "all green at line 42" in wait_result.output


# ---- unknown agent: closes audit pair --------------------------------------


def test_background_unknown_agent_closes_audit_pair(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """If spawn rejects the request before launching a thread (unknown
    agent name), the completed event must still fire so the audit pair is
    closed — otherwise verify_session sees a dangling spawned event."""
    # NOTE: agent_name="ghost" is not in the schema enum, but TaskTool
    # passes it straight through; the orchestrator returns the
    # "Unknown agent type" SubAgentResult and TaskTool should close the
    # pair.
    tool = TaskTool(SubAgentOrchestrator())
    result = tool.execute(
        agent_name="ghost", task="anything", background=True
    )
    assert result.success is False
    events = _read_events(active_session)
    kinds = [e["event_kind"] for e in events]
    assert kinds == ["subagent_spawned", "subagent_completed"]
    assert events[1]["success"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
