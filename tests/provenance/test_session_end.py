"""Verify AgentLoop emits a `session_end` event on every run() exit
(DESIGN_BENCH.md §5.4.a) and dispatches the single-task LLM verification
gate when wired in (§5.4.b / DESIGN_HARNESS.md §3.7).

The session_end event is what closes the "tool-free runs land zero rows
in provenance" gap the bench's E3 adapter would otherwise hit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from sciagent.agent import AgentLoop, AgentConfig
from sciagent.llm import LLMResponse, ToolCall
from sciagent.orchestrator import OrchestratorConfig
from sciagent.provenance_log import (
    get_provenance_log,
    reset_provenance_logs,
    set_active_session,
)
from sciagent.tools import ToolRegistry, ToolResult


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    set_active_session(None)
    yield
    reset_provenance_logs()
    set_active_session(None)


class _StubLLM:
    """LLMClient stand-in that returns a queue of LLMResponses on chat().

    Carries an ``_last_usage`` dict so AgentLoop._single_step's per-call
    cost rollup picks up tokens_in / tokens_out / cost_usd — the same
    code path litellm's _capture_last_usage exercises in production.
    """

    def __init__(self, responses: List[LLMResponse], last_usage: Optional[Dict] = None):
        self._queue = list(responses)
        self._last_usage = last_usage or {
            "tokens_in": 100,
            "tokens_out": 25,
            "cost_usd": 0.001,
            "model": "test-model-x",
        }
        self.calls: List[Dict] = []

    def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if not self._queue:
            return LLMResponse(content="(no more queued)", usage={"prompt_tokens": 1, "completion_tokens": 1})
        return self._queue.pop(0)


class _EchoTool:
    name = "echo"
    description = "echo args back"

    def __init__(self):
        self.calls: List[Dict] = []

    def execute(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(success=True, output={"echoed": kwargs}, error=None)

    def to_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        }


def _make_agent(
    tmp_path: Path,
    llm: _StubLLM,
    tools: Optional[ToolRegistry] = None,
    orchestrator_config: Optional[OrchestratorConfig] = None,
    subagent_orchestrator=None,
) -> AgentLoop:
    registry = tools or ToolRegistry()
    return AgentLoop(
        config=AgentConfig(
            working_dir=str(tmp_path),
            verbose=False,
            model="test-model-x",
            state_dir=str(tmp_path / "states"),
            auto_save=False,
        ),
        tools=registry,
        llm=llm,
        orchestrator_config=orchestrator_config,
        subagent_orchestrator=subagent_orchestrator,
    )


def _isolate_log(agent: AgentLoop, base_dir: Path) -> Path:
    log = get_provenance_log(agent.state.session_id, base_dir=base_dir)
    return log.path


def _read_log(log_path: Path) -> List[Dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Prereq A — session_end emission
# ---------------------------------------------------------------------------


def test_session_end_emitted_for_tool_free_run(tmp_path: Path):
    """The bench's adapter needs a session-level cost / token row even
    when the agent answered without calling a tool. session_end is that row."""
    llm = _StubLLM(responses=[
        LLMResponse(
            content="hello world",
            tool_calls=[],
            usage={"prompt_tokens": 100, "completion_tokens": 25},
        ),
    ], last_usage={
        "tokens_in": 100,
        "tokens_out": 25,
        "cost_usd": 0.0042,
        "model": "test-model-x",
    })

    agent = _make_agent(tmp_path, llm=llm)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("say hi")

    events = _read_log(log_path)
    # Tool-free run — only event should be session_end.
    session_end = [e for e in events if e["event_kind"] == "session_end"]
    assert len(session_end) == 1, (
        f"expected exactly one session_end, got events: {[e['event_kind'] for e in events]}"
    )
    ev = session_end[0]
    assert ev["model"] == "test-model-x"
    assert ev["iterations"] == 1
    assert ev["tokens_in"] == 100
    assert ev["tokens_out"] == 25
    assert ev["cost_usd"] == pytest.approx(0.0042)
    assert ev["wall_seconds"] >= 0.0
    assert ev["exit_reason"] == "done"
    # Schema-compat sanity: no schema bump.
    assert ev["schema_version"] == "2"


def test_session_end_emitted_with_zero_tool_call_events(tmp_path: Path):
    """Explicitly assert no tool_call events land when the agent answers
    directly — proves session_end is the only signal for tool-free runs."""
    llm = _StubLLM(responses=[
        LLMResponse(
            content="direct answer",
            tool_calls=[],
            usage={"prompt_tokens": 50, "completion_tokens": 10},
        ),
    ])
    agent = _make_agent(tmp_path, llm=llm)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("trivial prompt")

    events = _read_log(log_path)
    kinds = [e["event_kind"] for e in events]
    assert "tool_call" not in kinds
    assert "tool_result" not in kinds
    assert kinds.count("session_end") == 1


def test_session_end_emitted_after_tool_using_run(tmp_path: Path):
    """For runs that DID use tools, session_end still fires — it carries
    a session-level total adapters can cross-check against per-tool_result
    H3 cost rows."""
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _StubLLM(responses=[
        LLMResponse(
            content="thinking",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})],
            usage={"prompt_tokens": 40, "completion_tokens": 8},
        ),
        LLMResponse(
            content="done",
            tool_calls=[],
            usage={"prompt_tokens": 60, "completion_tokens": 5},
        ),
    ])
    agent = _make_agent(tmp_path, llm=llm, tools=registry)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("call echo then answer")

    events = _read_log(log_path)
    kinds = [e["event_kind"] for e in events]
    # tool_call + tool_result pair, then session_end at the close.
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("session_end") == 1
    se = [e for e in events if e["event_kind"] == "session_end"][0]
    assert se["iterations"] == 2
    # Per-call rollup is the same stub usage each step, so tokens_in is
    # the stub value × 2 iterations.
    assert se["tokens_in"] == 200
    assert se["tokens_out"] == 50


def test_truncated_turn_continues_instead_of_exiting_done(tmp_path: Path):
    """finish_reason="length" (max_tokens cap) used to silently exit the
    loop as ``done`` with empty content — see the photonics run on
    2026-06-07. The fix: detect the truncation, retain the partial as the
    assistant turn, inject a continuation cue, and keep iterating.

    Provider-agnostic because litellm normalizes Anthropic max_tokens,
    OpenAI length, and Gemini MAX_TOKENS all to ``"length"``."""
    llm = _StubLLM(responses=[
        LLMResponse(
            content="",  # truncated mid-tool-use → no usable text
            tool_calls=[],
            finish_reason="length",
            usage={"prompt_tokens": 100, "completion_tokens": 16273},
        ),
        LLMResponse(
            content="final answer",
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 200, "completion_tokens": 12},
        ),
    ])

    agent = _make_agent(tmp_path, llm=llm)
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("do the thing")

    events = _read_log(log_path)
    se = [e for e in events if e["event_kind"] == "session_end"][0]

    # Did NOT terminate on the truncated turn — looped to a second LLM call.
    assert se["iterations"] == 2
    assert se["exit_reason"] == "done"
    # Two LLM calls means the continuation cue actually went out.
    assert len(llm.calls) == 2
    second_call_messages = llm.calls[1]["messages"]
    def _text(m):
        c = m.content if hasattr(m, "content") else m.get("content")
        return c if isinstance(c, str) else ""
    cue_present = any(
        "truncated at the max_tokens cap" in _text(m)
        for m in second_call_messages
    )
    assert cue_present, "continuation cue should be visible in the next LLM round"


def test_session_end_exit_reason_error_on_llm_exception(tmp_path: Path):
    """An exception inside _single_step routes to exit_reason="error"."""
    class _ExplodingLLM(_StubLLM):
        def chat(self, *_a, **_kw):
            raise RuntimeError("simulated provider 500")

    agent = _make_agent(tmp_path, llm=_ExplodingLLM(responses=[]))
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("trigger error")

    events = _read_log(log_path)
    se = [e for e in events if e["event_kind"] == "session_end"]
    assert len(se) == 1
    assert se[0]["exit_reason"] == "error"


def test_session_end_cost_usd_is_none_when_no_per_call_cost(tmp_path: Path):
    """When the LLM provider doesn't surface response_cost (older models,
    self-hosted), cost_usd lands as None — not 0.0 — so adapters can
    distinguish "missing cost data" from "this run was free"."""
    llm = _StubLLM(responses=[
        LLMResponse(content="ok", tool_calls=[], usage={"prompt_tokens": 5, "completion_tokens": 1}),
    ], last_usage={"tokens_in": 5, "tokens_out": 1, "cost_usd": None, "model": "test-model-x"})

    agent = _make_agent(tmp_path, llm=llm)
    log_path = _isolate_log(agent, tmp_path / "sciagent")
    agent.run("hi")

    events = _read_log(log_path)
    se = next(e for e in events if e["event_kind"] == "session_end")
    assert se["cost_usd"] is None
    assert se["tokens_in"] == 5
    assert se["tokens_out"] == 1


# ---------------------------------------------------------------------------
# Prereq B — single-task verification gate (DESIGN_BENCH.md §5.4.b)
# ---------------------------------------------------------------------------


class _FakeSubagent:
    """Stand-in for SubAgentOrchestrator.spawn() — same pattern as
    tests/provenance/test_verification_emission.py:_FakeSubagent.

    NOT a litellm mock: this stubs the orchestrator's spawn interface,
    one layer above LLM. The verification gate calls .spawn() and reads
    .llm.model for the verifier identity.
    """

    def __init__(self, verdict_json: str, model: str = "test-verifier"):
        self.verdict_json = verdict_json
        self.llm = MagicMock(model=model)
        self.calls: List[Dict] = []
        self.tools = MagicMock()

    def spawn(self, agent_name: str, task: str, **_kwargs):
        self.calls.append({"agent_name": agent_name, "task": task})
        return MagicMock(
            success=True,
            output=self.verdict_json,
            error=None,
            iterations=1,
        )


def test_verification_gate_fires_on_single_task_when_enabled(tmp_path: Path):
    """With enable_verification=True, a clean tool-free run dispatches the
    LLM verifier subagent and lands a verification_result event."""
    llm = _StubLLM(responses=[
        LLMResponse(content="42", tool_calls=[], usage={"prompt_tokens": 8, "completion_tokens": 1}),
    ])
    subagent = _FakeSubagent(verdict_json=json.dumps({
        "verdict": "refuted",
        "confidence": 0.9,
        "issues": [{
            "severity": "error",
            "category": "no_evidence",
            "message": "claim not grounded in any retrieved data",
        }],
        "reasoning": "deliberately incorrect output",
    }), model="cross-family-verifier")

    agent = _make_agent(
        tmp_path,
        llm=llm,
        orchestrator_config=OrchestratorConfig(verbose=False, enable_verification=True),
        subagent_orchestrator=subagent,
    )
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("what is the answer?")

    events = _read_log(log_path)
    verif = [e for e in events if e["event_kind"] == "verification_result" and e.get("gate") == "llm"]
    assert len(verif) == 1, (
        f"expected one llm verification_result, saw kinds={[e['event_kind'] for e in events]}"
    )
    ev = verif[0]
    assert ev["verdict"] == "refuted"
    assert ev["verifier"] == "cross-family-verifier"
    assert ev["claim"]["kind"] == "task_outcome"
    # The verifier was actually dispatched.
    assert len(subagent.calls) == 1
    assert subagent.calls[0]["agent_name"] == "verifier"
    # session_end still fires alongside the gate result.
    assert any(e["event_kind"] == "session_end" for e in events)


def test_verification_gate_skipped_when_disabled(tmp_path: Path):
    """Backward-compat: enable_verification=False → no gate, no event."""
    llm = _StubLLM(responses=[
        LLMResponse(content="ok", tool_calls=[], usage={"prompt_tokens": 5, "completion_tokens": 1}),
    ])
    subagent = _FakeSubagent(verdict_json='{"verdict":"verified","confidence":1.0,"issues":[]}')

    agent = _make_agent(
        tmp_path,
        llm=llm,
        orchestrator_config=OrchestratorConfig(verbose=False, enable_verification=False),
        subagent_orchestrator=subagent,
    )
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("trivial")

    events = _read_log(log_path)
    verif = [e for e in events if e["event_kind"] == "verification_result"]
    assert verif == []
    # Verifier subagent was never spawned.
    assert subagent.calls == []
    # session_end still emits — it's unconditional.
    assert any(e["event_kind"] == "session_end" for e in events)


def test_verification_gate_skipped_without_orchestrator_handles(tmp_path: Path):
    """When AgentLoop is constructed without orchestrator_config /
    subagent_orchestrator (the pre-§5.4.b construction shape), the gate
    is a no-op. Preserves the legacy AgentLoop entry point."""
    llm = _StubLLM(responses=[
        LLMResponse(content="ok", tool_calls=[], usage={"prompt_tokens": 5, "completion_tokens": 1}),
    ])
    agent = _make_agent(tmp_path, llm=llm)  # no orchestrator kwargs
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("trivial")

    events = _read_log(log_path)
    assert [e for e in events if e["event_kind"] == "verification_result"] == []
    assert any(e["event_kind"] == "session_end" for e in events)


def test_verification_gate_skipped_on_errored_response(tmp_path: Path):
    """Don't ask the verifier to audit a "(Stopped by user)" or
    "(Error: ...)" sentinel — the agent didn't produce a real claim."""
    class _ExplodingLLM(_StubLLM):
        def chat(self, *_a, **_kw):
            raise RuntimeError("provider 500")

    subagent = _FakeSubagent(verdict_json='{"verdict":"verified","confidence":1.0,"issues":[]}')
    agent = _make_agent(
        tmp_path,
        llm=_ExplodingLLM(responses=[]),
        orchestrator_config=OrchestratorConfig(verbose=False, enable_verification=True),
        subagent_orchestrator=subagent,
    )
    log_path = _isolate_log(agent, tmp_path / "sciagent")

    agent.run("anything")

    events = _read_log(log_path)
    assert subagent.calls == []
    assert [e for e in events if e["event_kind"] == "verification_result"] == []
    # session_end emits with exit_reason=error.
    se = next(e for e in events if e["event_kind"] == "session_end")
    assert se["exit_reason"] == "error"
