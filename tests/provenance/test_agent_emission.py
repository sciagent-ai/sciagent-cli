"""Verify M1B's tool_call / tool_result emission is wired into the agent loop.

We don't spin up the LLM here — we drive ``AgentLoop._execute_tool_calls``
directly with synthetic ToolCalls against a tiny tool registry, then assert
the per-session JSONL contains the expected event pair.

Scope: log emission only — we do NOT verify any change in the agent's
existing dispatch behavior, since the M1B handoff froze that surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

from sciagent.agent import AgentLoop, AgentConfig
from sciagent.llm import ToolCall
from sciagent.tools import ToolRegistry, ToolResult
from sciagent.provenance_log import get_provenance_log, reset_provenance_logs


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


def _isolate_log(agent: AgentLoop, base_dir: Path) -> Path:
    """Prime the per-session singleton against a tmp base_dir so the test
    never writes to ~/.sciagent. Subsequent ``get_provenance_log(session_id)``
    calls (including the one inside ``_execute_tool_calls``) hit this cache.
    """
    log = get_provenance_log(agent.state.session_id, base_dir=base_dir)
    return log.path


class _StubTool:
    """A trivial tool that records calls and echoes a structured output."""

    name = "echo"
    description = "echo arguments back as the result"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def execute(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        if self.fail:
            return ToolResult(success=False, output=None, error="stub failure")
        return ToolResult(success=True, output={"echoed": kwargs}, error=None)

    def to_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        }


def _make_agent(tmp_path: Path, tool: _StubTool) -> AgentLoop:
    """Build an AgentLoop with a single stub tool, isolated session log."""
    registry = ToolRegistry()
    registry.register(tool)

    # Skip LLM construction — we won't call the LLM in these tests.
    with patch("sciagent.agent.LLMClient"):
        agent = AgentLoop(
            config=AgentConfig(working_dir=str(tmp_path), verbose=False, model="test-model-x"),
            tools=registry,
        )
    return agent


def _read_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_successful_tool_call_emits_call_and_result(tmp_path: Path):
    tool = _StubTool()
    agent = _make_agent(tmp_path, tool)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    tc = ToolCall(id="call_001", name="echo", arguments={"x": 7})
    agent._execute_tool_calls([tc])

    events = _read_log(log_path)
    kinds = [e["event_kind"] for e in events]
    assert kinds == ["tool_call", "tool_result"]

    call_ev, result_ev = events
    assert call_ev["tool_call_id"] == "call_001"
    assert call_ev["tool_name"] == "echo"
    assert call_ev["arguments"] == {"x": 7}
    assert call_ev["actor"] == "test-model-x"
    assert "arguments_sha256" in call_ev

    assert result_ev["tool_call_id"] == "call_001"
    assert result_ev["success"] is True
    assert result_ev["output_summary"] == {"echoed": {"x": 7}}
    assert result_ev["error"] is None
    assert isinstance(result_ev["duration_ms"], int)
    assert result_ev["actor"] == "test-model-x"


def test_failing_tool_emits_result_with_error(tmp_path: Path):
    tool = _StubTool(fail=True)
    agent = _make_agent(tmp_path, tool)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    tc = ToolCall(id="call_fail", name="echo", arguments={"x": 1})
    agent._execute_tool_calls([tc])

    events = _read_log(log_path)
    assert len(events) == 2
    result_ev = events[1]
    assert result_ev["success"] is False
    assert result_ev["error"] == "stub failure"


def test_multiple_calls_in_same_batch_share_session(tmp_path: Path):
    tool = _StubTool()
    agent = _make_agent(tmp_path, tool)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent._execute_tool_calls([
        ToolCall(id="c1", name="echo", arguments={"i": 1}),
        ToolCall(id="c2", name="echo", arguments={"i": 2}),
        ToolCall(id="c3", name="echo", arguments={"i": 3}),
    ])

    events = _read_log(log_path)
    # 3 calls × 2 events each, all in one session, monotonic seq.
    assert len(events) == 6
    assert all(e["session_id"] == agent.state.session_id for e in events)
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6]
    # call/result pairs are interleaved per the dispatch loop
    assert [e["event_kind"] for e in events] == [
        "tool_call", "tool_result",
        "tool_call", "tool_result",
        "tool_call", "tool_result",
    ]


def test_log_write_failure_does_not_break_dispatch(tmp_path: Path):
    """A provenance-log write failure must not bubble out of the tool loop —
    the log is a verification record, not part of the API contract."""
    tool = _StubTool()
    agent = _make_agent(tmp_path, tool)
    _isolate_log(agent, tmp_path / "sciagent")

    with patch("sciagent.agent.get_provenance_log", side_effect=RuntimeError("disk full")):
        # Should NOT raise.
        agent._execute_tool_calls([ToolCall(id="c1", name="echo", arguments={"x": 1})])

    # Tool was still dispatched
    assert len(tool.calls) == 1
