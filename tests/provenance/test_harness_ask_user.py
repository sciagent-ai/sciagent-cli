"""DATA / EXEC gates route their pauses through the same ask_user path the
LLM uses (instead of the deleted ``_pause_for_user`` helper).

Two properties under test:

1. Provenance: when the DATA gate fires, a synthetic ``tool_call`` event
   with ``tool_name="ask_user"`` lands in the per-session JSONL alongside
   the original external tool's events. Its ``arguments`` carry the
   gate's question, options, and a ``_source`` marker so reviewers can
   distinguish harness-initiated ask_user calls from LLM-initiated ones.

2. Parent context propagation: the user's response enters the agent's
   context window as the tool_result text for the failing external tool.
   This is the bubble-up the deleted helper couldn't deliver — the
   parent loop sees the user's choice in its next turn.
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
from sciagent.tools.atomic.ask_user import AskUserTool
from sciagent.provenance_log import get_provenance_log, reset_provenance_logs


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


class _FailingWebTool:
    """Stand-in for an external data tool that always fails — the shape
    of failure ``AgentLoop`` treats as ``EXTERNAL_TOOLS`` evidence."""

    name = "web"
    description = "stub that always returns 404"

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=False, output=None, error="404 not found")

    def to_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        }


def _isolate_log(agent: AgentLoop, base_dir: Path) -> Path:
    log = get_provenance_log(agent.state.session_id, base_dir=base_dir)
    return log.path


def _read_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _make_agent(tmp_path: Path) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_FailingWebTool())
    registry.register(AskUserTool())

    with patch("sciagent.agent.LLMClient"):
        agent = AgentLoop(
            config=AgentConfig(working_dir=str(tmp_path), verbose=False, model="test-model-x"),
            tools=registry,
        )
    # Trip the DATA gate on the very next failure so we don't have to dispatch
    # three failing tool calls just to set up the test.
    agent._max_consecutive_external_failures = 1
    return agent


def test_data_gate_emits_synthetic_ask_user_tool_call(tmp_path: Path):
    """When the DATA gate fires, a tool_call event with tool_name='ask_user'
    lands in the provenance log alongside the original tool's events. Its
    arguments carry the gate's reason, options, and a _source marker."""
    agent = _make_agent(tmp_path)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    with patch.object(agent, "_prompt_user_for_input", return_value="use n=2.4 directly"):
        agent._execute_tool_calls([
            ToolCall(id="call_web_1", name="web", arguments={"query": "anything"})
        ])

    events = _read_log(log_path)
    kinds = [(e["event_kind"], e["tool_name"]) for e in events]

    # Outer tool_call for the failing 'web' call straddles the inner ask_user
    # call/result pair emitted by the harness gate.
    assert kinds == [
        ("tool_call", "web"),
        ("tool_call", "ask_user"),
        ("tool_result", "ask_user"),
        ("tool_result", "web"),
    ], f"unexpected event order: {kinds}"

    ask_call = next(e for e in events if e["event_kind"] == "tool_call" and e["tool_name"] == "ask_user")
    args = ask_call["arguments"]
    assert args["_source"] == "harness:data_gate"
    assert "External data access failed" in args["question"]
    assert "404 not found" in args["question"]
    assert args["options"] == [
        "Provide alternative data source (I'll specify)",
        "Continue with explicit limitations (document missing data)",
        "Stop task - required data not available",
    ]

    ask_result = next(e for e in events if e["event_kind"] == "tool_result" and e["tool_name"] == "ask_user")
    assert ask_result["success"] is True
    assert ask_result["output_summary"]["user_response"] == "use n=2.4 directly"
    assert ask_result["output_summary"]["_source"] == "harness:data_gate"


def test_user_response_lands_in_parent_context(tmp_path: Path):
    """The user's choice ends up in the agent's context window as the
    tool_result text for the failing external tool — so the parent loop
    sees it on the next LLM turn (the property the old _pause_for_user
    couldn't deliver)."""
    agent = _make_agent(tmp_path)
    _isolate_log(agent, tmp_path / "sciagent")

    with patch.object(agent, "_prompt_user_for_input", return_value="use n=2.4 directly"):
        agent._execute_tool_calls([
            ToolCall(id="call_web_2", name="web", arguments={"query": "anything"})
        ])

    messages = agent.state.context.get_messages()
    tool_result_texts = [
        msg.content
        for msg in messages
        if getattr(msg, "role", None) == "tool"
        and getattr(msg, "tool_call_id", None) == "call_web_2"
    ]
    assert tool_result_texts, "no tool_result was recorded for the failing web call"
    joined = "\n".join(str(t) for t in tool_result_texts)
    assert "use n=2.4 directly" in joined, (
        f"user's response never reached parent context. tool_result text: {tool_result_texts!r}"
    )
