"""Lite-tier subagent observations: dataclass roundtrip, parser, and
TaskTool bubble formatting.

Source design: ``designdocs_memory/subagent_self_learning.md``.
Plan: ``designdocs_nextsteps/PLAN_OBSERVATIONS_LITE.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

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
from sciagent.subagent_observations import (
    Observation,
    format_observations_for_parent,
    parse_observations_block,
)


# ---- Observation dataclass roundtrip ----------------------------------------


def test_observation_to_dict_roundtrip():
    obs = Observation(
        kind="image_quirk",
        scope=["service:openfoam"],
        trigger="mpirun -np 4 simpleFoam",
        symptom="OPAL ERROR: Initializing as root prohibited",
        fix_shape={"destination": "dockerfile_env",
                   "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1"},
        confidence="high",
        session_id="ses-123",
    )
    d = obs.to_dict()
    assert d["kind"] == "image_quirk"
    assert d["scope"] == ["service:openfoam"]
    assert d["confidence"] == "high"
    assert d["session_id"] == "ses-123"

    restored = Observation.from_dict(d)
    assert restored == obs


def test_subagent_result_carries_observations_default_empty():
    """Default value must be an empty list — to_dict serializes it as []."""
    r = SubAgentResult(agent_name="x", task="t", success=True, output="ok")
    assert r.observations == []
    assert r.to_dict()["observations"] == []


def test_subagent_result_to_dict_serializes_observations():
    obs = Observation(
        kind="backend_quirk",
        scope=["backend:skypilot"],
        trigger="bash /tmp/run.sh",
        symptom="Permission denied (/tmp noexec)",
        fix_shape={"destination": "registry_quirks", "patch": "use bash <path>"},
        confidence="medium",
    )
    r = SubAgentResult(
        agent_name="compute", task="t", success=True, output="ok",
        observations=[obs],
    )
    d = r.to_dict()
    assert len(d["observations"]) == 1
    assert d["observations"][0]["kind"] == "backend_quirk"
    assert d["observations"][0]["confidence"] == "medium"


# ---- Parser: <observations>...</observations> block -------------------------


def test_parser_extracts_block_and_strips_from_output():
    output = """All done.
Files at $OUTPUTS_DIR/result.json.

<observations>
[{"kind": "image_quirk",
  "scope": ["service:openfoam"],
  "trigger": "mpirun -np 4 simpleFoam",
  "symptom": "OPAL ERROR: Initializing as root prohibited",
  "fix_shape": {"destination": "dockerfile_env", "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1"},
  "confidence": "high"}]
</observations>
"""
    obs, stripped = parse_observations_block(output, session_id="ses-abc")
    assert len(obs) == 1
    assert obs[0].kind == "image_quirk"
    assert obs[0].confidence == "high"
    # Session id auto-stamped from caller
    assert obs[0].session_id == "ses-abc"
    # Block removed from output so it doesn't double-render under the header
    assert "<observations>" not in stripped
    assert "All done." in stripped


def test_parser_returns_empty_on_no_block():
    output = "Just a regular subagent output, no observations here."
    obs, stripped = parse_observations_block(output)
    assert obs == []
    assert stripped == output


def test_parser_returns_empty_on_malformed_json():
    """Malformed JSON inside the block is best-effort: returns empty + leaves
    output unchanged so the parent at least sees something."""
    output = "<observations>this is not json</observations>"
    obs, stripped = parse_observations_block(output)
    assert obs == []
    # Output left unchanged on malformed payload
    assert stripped == output


def test_parser_rejects_non_list_payload():
    """A scalar or object payload would let stray <observations>note</observations>
    in narrative text accidentally parse — must be rejected."""
    output = '<observations>{"kind": "image_quirk"}</observations>'
    obs, stripped = parse_observations_block(output)
    assert obs == []


def test_parser_handles_fenced_json_inside_block():
    """LLMs sometimes wrap JSON in a ```json fence. Tolerate it."""
    output = """<observations>
```json
[{"kind": "service_idiom",
  "scope": ["service:lammps"],
  "trigger": "mpirun -np N",
  "symptom": "decompose-before-solve required for >1M atoms",
  "fix_shape": {"destination": "registry_parallel", "patch": "decompose=true"},
  "confidence": "medium"}]
```
</observations>
"""
    obs, stripped = parse_observations_block(output)
    assert len(obs) == 1
    assert obs[0].kind == "service_idiom"


def test_parser_treats_empty_block_as_zero_observations():
    """An empty <observations></observations> is a valid signal — strip from
    output, return no observations."""
    output = "Done.\n<observations>\n</observations>\n"
    obs, stripped = parse_observations_block(output)
    assert obs == []
    assert "<observations>" not in stripped


# ---- format_observations_for_parent: header rendering -----------------------


def test_format_empty_list_renders_nothing():
    """Caller relies on this to skip appending the header altogether when
    no observations were emitted."""
    assert format_observations_for_parent([]) == ""


def test_format_renders_header_with_observation_details():
    obs = Observation(
        kind="image_quirk",
        scope=["service:openfoam"],
        trigger="mpirun -np 4 simpleFoam",
        symptom="OPAL ERROR: Initializing as root prohibited",
        fix_shape={"destination": "dockerfile_env",
                   "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1"},
        confidence="high",
    )
    rendered = format_observations_for_parent([obs])
    assert "## Observations" in rendered
    assert "image_quirk" in rendered
    assert "high" in rendered
    assert "service:openfoam" in rendered
    assert "OPAL ERROR" in rendered
    assert "ENV OMPI_ALLOW_RUN_AS_ROOT=1" in rendered
    assert "never auto-applied" in rendered


# ---- TaskTool bubble: header appears in tool result's output ---------------


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = fake_home / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: target)
    return target


@pytest.fixture
def active_session(tmp_path: Path):
    reset_provenance_logs()
    session_id = "ses-obs-tasktool"
    log = get_provenance_log(session_id, base_dir=tmp_path)
    set_active_session(session_id)
    yield log
    set_active_session(None)
    reset_provenance_logs()


class _FakeSubAgent:
    """Minimal SubAgent stand-in matching test_task_tool_background.py."""

    def __init__(
        self,
        agent_name: str,
        *,
        success: bool = True,
        output: str = "fake output",
        observations: Optional[List[Observation]] = None,
        error: Optional[str] = None,
    ):
        self.config = SubAgentConfig(
            name=agent_name, description="", system_prompt="x"
        )
        self.session_id = f"child-ses-{agent_name}"
        self._success = success
        self._output = output
        self._observations = observations or []
        self._error = error

    def run(self, task: str) -> SubAgentResult:
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
            observations=list(self._observations),
        )


def _make_tool(fake_factory) -> TaskTool:
    orch = SubAgentOrchestrator(working_dir=".")
    orch._build_subagent = lambda config: fake_factory(config.name)
    return TaskTool(orch)


def test_task_tool_bubbles_observations_under_header(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    obs = Observation(
        kind="image_quirk",
        scope=["service:openfoam"],
        trigger="mpirun -np 4 simpleFoam",
        symptom="OPAL ERROR: Initializing as root prohibited",
        fix_shape={"destination": "dockerfile_env",
                   "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1"},
        confidence="high",
    )
    tool = _make_tool(
        lambda name: _FakeSubAgent(
            name, output="Job done; result at $OUTPUTS_DIR/x.json",
            observations=[obs],
        )
    )
    result = tool.execute(agent_name="compute", task="run thing")
    assert result.success is True
    assert "## Observations" in result.output
    assert "image_quirk" in result.output
    assert "ENV OMPI_ALLOW_RUN_AS_ROOT=1" in result.output
    # Narrative output still present
    assert "Job done" in result.output


def test_task_tool_no_header_when_no_observations(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """Empty observations list must not render the header — keeps tool
    results clean for routine sessions where nothing novel surfaced."""
    tool = _make_tool(
        lambda name: _FakeSubAgent(name, output="Job done; nothing novel.")
    )
    result = tool.execute(agent_name="compute", task="run thing")
    assert result.success is True
    assert "## Observations" not in result.output
    assert "Observations" not in result.output  # not even mentioned


def test_task_tool_observations_in_metadata(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """Structured observations also surface via tool metadata so a UI
    layer / aggregator can read them without re-parsing the bubble text."""
    obs = Observation(
        kind="backend_quirk",
        scope=["backend:skypilot"],
        trigger="bash run.sh",
        symptom="/tmp noexec on Sky AWS clusters",
        fix_shape={"destination": "prompt_compute", "patch": "use bash <path>"},
        confidence="medium",
    )
    tool = _make_tool(
        lambda name: _FakeSubAgent(name, output="ok", observations=[obs])
    )
    result = tool.execute(agent_name="compute", task="t")
    assert "subagent_observations" in result.metadata
    md_obs = result.metadata["subagent_observations"]
    assert len(md_obs) == 1
    assert md_obs[0]["kind"] == "backend_quirk"
    assert md_obs[0]["confidence"] == "medium"


def test_task_tool_observations_bubble_on_failure(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """Failed runs can still surface lessons (e.g., 'this image rejects MPI
    as root') — the failure path must include observations under the same
    header, since codifying the lesson is independent of whether the run
    landed artifacts."""
    obs = Observation(
        kind="image_quirk",
        scope=["service:openfoam"],
        trigger="mpirun -np 4",
        symptom="OPAL ERROR: Initializing as root prohibited",
        fix_shape={"destination": "dockerfile_env",
                   "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1"},
        confidence="high",
    )
    tool = _make_tool(
        lambda name: _FakeSubAgent(
            name, success=False, output="", error="job FAILED",
            observations=[obs],
        )
    )
    result = tool.execute(agent_name="compute", task="t")
    assert result.success is False
    assert "## Observations" in (result.error or "")
    assert "image_quirk" in (result.error or "")


# ---- Provenance: subagent_observation events emitted -----------------------


def _read_events(log: ProvenanceLog) -> List[dict]:
    if not log.path.exists():
        return []
    out = []
    for line in log.path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def test_observation_emits_provenance_event(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """Mirrors emit_produces_validation_*: one subagent_observation event
    per Observation, actor=subagent:<name>, body carries the observation
    dict so a verifier reading the log can reconstruct candidate findings
    after the fact."""
    obs = Observation(
        kind="image_quirk",
        scope=["service:openfoam"],
        trigger="mpirun",
        symptom="OPAL ERROR: Initializing as root prohibited",
        fix_shape={"destination": "dockerfile_env",
                   "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1"},
        confidence="high",
    )
    tool = _make_tool(
        lambda name: _FakeSubAgent(name, output="ok", observations=[obs])
    )
    tool.execute(agent_name="compute", task="run")

    events = _read_events(active_session)
    kinds = [e["event_kind"] for e in events]
    assert "subagent_observation" in kinds
    obs_event = next(e for e in events if e["event_kind"] == "subagent_observation")
    assert obs_event["subagent_name"] == "compute"
    assert obs_event["actor"] == "subagent:compute"
    assert obs_event["observation"]["kind"] == "image_quirk"
    assert obs_event["observation"]["confidence"] == "high"


def test_no_observation_event_when_list_empty(
    tmp_manifest_dir: Path, active_session: ProvenanceLog
):
    """Lite-tier promise: empty list is fine, no provenance noise."""
    tool = _make_tool(lambda name: _FakeSubAgent(name, output="ok"))
    tool.execute(agent_name="compute", task="run")
    events = _read_events(active_session)
    kinds = [e["event_kind"] for e in events]
    assert "subagent_observation" not in kinds


# ---- End-to-end: SubAgent.run parses observations off real LLM output ------


def test_subagent_run_parses_observations_from_agent_output(monkeypatch):
    """SubAgent.run hands the AgentLoop's terminal text through
    parse_observations_block. Mock the AgentLoop so the test runs without
    an LLM but still exercises the SubAgent.run wiring."""
    from sciagent.subagent import SubAgent, SubAgentConfig

    config = SubAgentConfig(
        name="compute", description="", system_prompt="x"
    )

    sub_agent = SubAgent.__new__(SubAgent)
    sub_agent.config = config
    sub_agent.working_dir = "."
    sub_agent.is_nested = False
    sub_agent.parent_interrupt_event = None
    sub_agent.session_id = "ses-test-run"
    sub_agent.parent_session_id = None

    class _FakeAgent:
        iteration_count = 1
        total_tokens = 10

        def run(self, task):
            return (
                "Done.\n\n"
                "<observations>\n"
                '[{"kind":"image_quirk","scope":["service:openfoam"],'
                '"trigger":"mpirun","symptom":"OPAL ERROR",'
                '"fix_shape":{"destination":"dockerfile_env",'
                '"patch":"ENV OMPI_ALLOW_RUN_AS_ROOT=1"},'
                '"confidence":"high"}]\n'
                "</observations>"
            )

    sub_agent.agent = _FakeAgent()
    result = sub_agent.run("any task")
    assert result.success is True
    assert len(result.observations) == 1
    assert result.observations[0].kind == "image_quirk"
    assert result.observations[0].session_id == "ses-test-run"
    # Block stripped from the narrative output
    assert "<observations>" not in result.output
    assert "Done." in result.output


# ---- Integration: full TaskTool → orchestrator → SubAgent path -------------


def test_integration_real_subagent_path_surfaces_openfoam_ompi_quirk(
    tmp_manifest_dir: Path, active_session: ProvenanceLog, monkeypatch
):
    """Synthetic 'real subagent run' covering the full path the plan asks
    for: TaskTool.execute → SubAgentOrchestrator.spawn → real SubAgent
    construction (with the registered compute config + system prompt) →
    SubAgent.run → AgentLoop.run (patched to return a realistic compute
    subagent terminal reply that includes the canonical OpenFOAM /
    OMPI_ALLOW_RUN_AS_ROOT quirk) → parse_observations_block →
    SubAgentResult.observations → TaskTool bubble + provenance event.

    Verifies the Observation surfaces in the parent's tool_result.output,
    parses with the right fields, lands in metadata, and emits a
    subagent_observation provenance event.
    """
    realistic_terminal_reply = """\
status: SUCCESS
job_id: cl-openfoam-1
files: ./_outputs/cl-openfoam-1/postProcessing/lift.dat (412 bytes)
duration: 7m12s

The first mpirun under the opencfd OpenFOAM image hit
`OPAL ERROR: Initializing as root prohibited` because the image runs as
root and OpenMPI 4+ refuses without explicit opt-in. Setting
OMPI_ALLOW_RUN_AS_ROOT=1 + OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 unblocked it.

<observations>
[{
  "kind": "image_quirk",
  "scope": ["service:openfoam"],
  "trigger": "mpirun -np 4 simpleFoam -parallel",
  "symptom": "OPAL ERROR: Initializing as root prohibited (OpenMPI 4+ refuses to launch as root without explicit env-var opt-in; opencfd's OpenFOAM image runs as root)",
  "fix_shape": {"destination": "dockerfile_env",
                "patch": "ENV OMPI_ALLOW_RUN_AS_ROOT=1\\nENV OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1"},
  "confidence": "high"
}]
</observations>
"""

    # Patch AgentLoop.run so we exercise the real SubAgent + orchestrator
    # paths without an LLM call. iteration_count + total_tokens are real
    # attributes on the AgentLoop instance — the fake bumps them so
    # SubAgentResult records something plausible.
    from sciagent.agent import AgentLoop

    def _fake_run(self, task: str) -> str:
        self.iteration_count = 4
        self.total_tokens = 1234
        return realistic_terminal_reply

    monkeypatch.setattr(AgentLoop, "run", _fake_run)

    # Build the real path: TaskTool over a real SubAgentOrchestrator (which
    # uses the registered compute config + its full system prompt).
    orch = SubAgentOrchestrator(working_dir=str(tmp_manifest_dir.parent))
    tool = TaskTool(orch)
    result = tool.execute(
        agent_name="compute",
        task="Run a parallel OpenFOAM simpleFoam case on Sky",
    )

    # 1. Bubbled to the parent's tool result under the Observations header
    assert result.success is True
    assert "## Observations" in result.output
    assert "image_quirk" in result.output
    assert "OPAL ERROR" in result.output
    assert "OMPI_ALLOW_RUN_AS_ROOT" in result.output
    # The narrative is also visible (header is appended out of band)
    assert "job_id: cl-openfoam-1" in result.output
    # And the raw <observations> block was stripped from the narrative so
    # the LLM only sees one rendering of the lesson
    assert "<observations>" not in result.output

    # 2. Structured form available via metadata for non-LLM consumers
    md_obs = result.metadata["subagent_observations"]
    assert len(md_obs) == 1
    assert md_obs[0]["kind"] == "image_quirk"
    assert md_obs[0]["scope"] == ["service:openfoam"]
    assert md_obs[0]["confidence"] == "high"
    assert md_obs[0]["fix_shape"]["destination"] == "dockerfile_env"
    assert "OMPI_ALLOW_RUN_AS_ROOT" in md_obs[0]["fix_shape"]["patch"]

    # 3. Provenance event landed for cross-session aggregation in Full
    events = _read_events(active_session)
    obs_events = [e for e in events if e["event_kind"] == "subagent_observation"]
    assert len(obs_events) == 1
    assert obs_events[0]["subagent_name"] == "compute"
    assert obs_events[0]["actor"] == "subagent:compute"
    assert obs_events[0]["observation"]["kind"] == "image_quirk"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
