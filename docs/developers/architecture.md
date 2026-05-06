---
layout: default
title: Architecture
parent: Developer Documentation
nav_order: 1
---

# Architecture

SciAgent follows a **Think → Act → Observe** cycle. This page explains the internal components.

## Components

```
┌─────────────────────────────────────────────────────────┐
│                      AgentLoop                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Context  │  │   LLM    │  │   Tool   │              │
│  │ Window   │  │  Client  │  │ Registry │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                      │                                  │
│              ┌───────┴───────┐                         │
│              │    Skills     │                         │
│              └───────────────┘                         │
│                      │                                  │
│         ┌────────────┴────────────┐                    │
│         │   Sub-Agent Orchestrator │                   │
│         └─────────────────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

## Agent Loop

The core loop in `sciagent.agent.AgentLoop`:

1. **Context building** - Compile messages: system prompt, task, history, tool results
2. **LLM invocation** - Pass to `LLMClient.chat()`, receive text and/or tool calls
3. **Tool execution** - Execute tools, append results to context
4. **Observation** - Check for completion or errors
5. **Iteration control** - Track iterations/tokens, summarize if needed

Sessions auto-save to `.agent_states` for resumption.

## Context Window

`ContextWindow` manages conversation history with three roles: `system`, `user`, `assistant`. Tool results are inserted as assistant messages with `tool_result` fields.

When approaching token limits, older messages are summarized while preserving tool-use integrity:

```python
def _find_safe_cut_point(self, start, forward=True):
    """Find cut points that don't orphan tool_use/tool_result pairs."""
```

## LLM Client

`sciagent.llm.LLMClient` wraps litellm for multi-provider support:

- `chat(messages, tools)` - Send messages with tool schemas
- `chat_stream()` - Streaming variant
- `configure_cache(backend)` - Enable caching (local, redis, disabled)

## Tool System

Tools extend `BaseTool` with `name`, `description`, `parameters` (JSON schema), and `execute()`.

### Atomic Tools
Full-featured tools in `sciagent.tools.atomic`. Grouped by purpose:

**Core** — `bash`, `file_ops`, `search`, `web`, `todo`, `ask_user`, `skill`.

**Compute** — `compute_run`, `compute_exec`, `compute_cluster`, `materialize`, `materialize_workspace`. Filtered out of the main agent's registry; reachable via the `compute` and `analyze` subagents only.

**Task & background** — `task_list`, `task_get`, `task_wait` (kind-agnostic registry); `bg_status`, `bg_output`, `bg_wait`, `bg_kill` (cloud-job runtime).

**Service discovery** — `service_search`, `service_detail`.

**Monitoring** — `monitor`, `monitor_stop` (push-style stdout-line events delivered as `<system-reminder>` on the next agent turn — no per-event LLM round-trip).

**Verification** — `verify` / `verify_session` (snapshot read of the durable provenance log).

See [Tools reference](../tools.md) for full signatures.

### Tool Registry
`ToolRegistry` handles registration, lookup, and execution:

```python
registry = create_default_registry(working_dir="./project")
registry.register(my_tool)
registry.execute("bash", command="ls")
```

## Skills

Skills are loadable workflows in `src/sciagent/skills/*/SKILL.md`:

```yaml
---
name: sci-compute
triggers:
  - "simulat(e|ion)"
  - "run.*(meep|gromacs)"
---
# Workflow instructions...
```

When user input matches triggers, skill instructions inject into context.

Built-in skills:
- `sci-compute` - Scientific simulations with research-first workflow
- `build-service` - Docker service building
- `code-review` - Comprehensive code review

## Sub-agents

Sub-agents are isolated agents with their own context and tool set. Each uses a cost-optimised model tier defined in `src/sciagent/defaults.py`:

- **Scientific (SCIENTIFIC_MODEL)**: Best quality for scientific code and deep reasoning
- **Coding (CODING_MODEL)**: Good for implementation, debugging, research
- **Fast (FAST_MODEL)**: Quick/cheap for exploration and extraction

Defined by `SubAgentConfig`:

```python
SubAgentConfig(
    name="explore",
    description="Fast codebase exploration",
    system_prompt="...",
    model=FAST_MODEL,  # Uses tiered model
    max_iterations=15,
    allowed_tools=["file_ops", "search", "bash"]
)
```

Built-in sub-agents (registered in `SubAgentRegistry._register_defaults`, `src/sciagent/subagent.py`):

| Name | Model Tier | Purpose | Tools |
|------|------------|---------|-------|
| explore | Fast | Quick codebase searches | file_ops, search, bash |
| debug | Coding | Error investigation | file_ops, search, bash, web, skill |
| research | Coding | Web/doc research | web, file_ops, search |
| plan | Scientific | Break down problems | file_ops, search, bash, web, skill, todo |
| compute | Coding | Cloud-job orchestration with token-isolated context | compute_run, compute_exec, compute_cluster, materialize, materialize_workspace, service_search, service_detail, bg_*, monitor, web, ask_user, todo, file_ops, search, bash |
| analyze | Coding | Post-job derivation (plots, statistics, light fits, DSE) | materialize, compute_run/exec/cluster, service_search, file_ops, bash, search, web, monitor, bg_*, ask_user |
| general | Coding | Multi-step implementation tasks | all |
| verifier | Verification | Independent validation against the provenance log | file_ops, search, bash |

**Why two compute-related kinds?** `compute` produces primary data (a simulation, a training run, a heavy fit). `analyze` consumes that data and produces derived artifacts (plots, comparisons, regressions). Same data tier, different prompts, different idioms — and they routinely run independently (re-analyze without re-simulating, or analyze across many sim runs for design-space exploration).

The `compute` and `analyze` subagents see the cloud-compute tool surface; the **main agent does not**. This keeps cloud chatter (status polls, log tails, manifest writes) inside a per-subagent context bubble — the main agent sees only the subagent's bounded summary. Tool filtering happens via `ToolRegistry.clone(exclude={...})`.

### Orchestration

`SubAgentOrchestrator` manages spawning, parallel execution, and background runs:

```python
orch = SubAgentOrchestrator(tools=registry, working_dir=".")

# Foreground (synchronous)
result = orch.spawn("explore", "Find API endpoints")

# Parallel
results = orch.spawn_parallel([
    {"agent_name": "research", "task": "Find documentation for S4 library"},
    {"agent_name": "debug", "task": "Investigate build error in logs"}
])

# Background — returns a task_id, registers in task_index
task_id = orch.spawn(
    agent_name="analyze",
    task="KDE plot of T field at z=0.1m",
    background=True,
    produces_uris=["./_outputs/kde_z01.png"],
)
```

When `produces_uris=` is declared, the orchestrator validates after the subagent returns: each pattern must resolve to at least one file with size ≥ `produces_min_bytes` (default 100). Failure lands the task in `blocked_produce_missing` state, so the parent can detect missing artifacts even when the subagent reported success.

## SkyPilot Integration & Cluster Lifecycle

The compute layer (`src/sciagent/compute/`) routes scientific simulations between two backends — local Docker for small jobs, [SkyPilot](https://skypilot.readthedocs.io/) for cloud-scale work. Both produce the same `JobResult` shape so downstream tooling doesn't branch on backend.

The router (`compute/router.py`) selects SkyPilot when GPUs are requested, memory > 16 GB, CPUs > 8, or `backend="skypilot"` is explicit. Two execution modes:

- **Managed jobs** (`mode="job"`): Sky launches a transient cluster, runs the command, tears the cluster down on completion. One-shot.
- **Cluster mode** (`mode="cluster"`): Sky launches a persistent cluster the agent can iterate against (`compute_exec` for follow-up commands, `compute_cluster(action="refresh_mounts")` to point it at new inputs).

### Stop, not down

The end-of-task lifecycle action is `stop` (preserves the disk and identity, restartable in seconds), **not** `down` (destructive). The agent prompt enforces this rule. `down` is reserved for explicit cleanup or quota-driven teardown.

### Session workspace bucket

Every cluster job auto-mounts a per-session durable bucket at `/workspace/`:

```
<cloud>://sciagent-workspace-<session_id>/
```

Where `<cloud>` is whichever provider the job runs on (`s3`, `gs`, `az`, `r2`, `oci`). The bucket survives cluster teardown — outputs persist beyond the cluster. This is the data tier that `compute` → `analyze` → `verifier` all share. `materialize_workspace(subpath=..., dest=...)` pulls (a slice of) it back to local; `materialize(uri=...)` is the cloud-agnostic equivalent for arbitrary URIs.

For the user-facing guide see [Cloud Compute](../cloud-compute.md).

## Task Index & State Surfaces

Long-running work — cloud compute jobs and background subagents — is tracked in a single registry, the **task index**, at `~/.sciagent/tasks/<task_id>.json`. Two kinds today:

| Kind | Tracks |
|------|--------|
| `compute_job` | Cloud job launched via `compute_run` |
| `subagent` | Subagent run (background or foreground) |

Future kinds (`watch`, `scheduled`) land additively. The state machine:

```
pending → running → {completed | failed | cancelled | blocked_produce_missing}
                  → {crashed | blocked_resume}      ← resumable, subagent-only
```

The kind-agnostic `task_list` / `task_get` / `task_wait` tools query the registry across kinds. Cloud-job-specific operations (Sky status, logs, kill) stay on the `bg_*` tools.

### Why a single registry

Pre-consolidation, sciagent had separate stores for compute jobs, background bash, in-flight subagents, todo state, etc. — six overlapping state surfaces that drifted. The task index is the long-term consolidation target: one on-disk format, one set of query tools, one state machine. The provenance log (next section) handles the audit trail. The task index handles the runtime registry. The two layers do not duplicate each other.

For the user-facing guide see [Task Orchestration](../task-orchestration.md).

## Checkpoint & Resume

Background subagents checkpoint per-iteration to:

```
~/.sciagent/sessions/<session_id>/subagents/<task_id>/
├── checkpoint.jsonl   # per-iteration events (tool calls, hashes, previews)
└── agent_state.json   # full state snapshot
```

Schema version: `1`. On crash before terminal state, the registry entry's `state` is set to `crashed` and the checkpoint persists. A subsequent `spawn(...)` for a subagent-kind task hashes the task description and checks for a prior `crashed` or `blocked_resume` entry with the same hash — on match, the orchestrator prompts the parent for a 3-way choice (`skip` / `use_prior` / `retry`).

In `blocked_resume`, the subagent itself decides the work can't finish in the current process (token budget, mid-pipeline pause) and asks to be picked up later. The same resume flow applies.

## Provenance Log

`src/sciagent/provenance_log.py` writes an append-only JSONL log per session at `~/.sciagent/sessions/<session_id>/provenance.jsonl`. Schema version `1`. Event kinds:

| Event | Emitted by |
|-------|------------|
| `tool_call` | Agent before tool execution |
| `tool_result` | Agent after tool completion |
| `compute_job_launched` | `compute_run` |
| `compute_job_status_changed` | `bg_status` polling |
| `artifact_produced` | File observation |
| `verification_result` | `verify` / `verify_session` |
| `correction` | Manual override |

Per-line cap 16 KB; per-field cap 4 KB (oversize fields are replaced with a truncation stub carrying a SHA-256 + preview). Thread-safe via `fcntl.flock` so concurrent writes from main thread + orchestrator threads + verify probes never interleave.

The companion module `src/sciagent/provenance_lineage.py` builds a graph from these events — compute job → artifact → verification — for end-to-end traceability across sessions.

For the schema see [Provenance Log Schema](../provenance_log_schema.md).

## Verification System

SciAgent implements a **three-tier verification architecture** to prevent fabricated data and ensure scientific integrity.

### Verification Gates

```
┌─────────────────────────────────────────────────────────┐
│                   Task Execution                         │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  GATE 1: DATA GATE                                       │
│  • Verify HTTP fetches succeeded (status 200)            │
│  • Detect HTML/error pages in data files                 │
│  • Validate CSV structure and row counts                 │
│  • Prevents analysis on fabricated/invalid data          │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  GATE 2: EXEC GATE                                       │
│  • Verify commands actually ran                          │
│  • Check exit codes (success = 0)                        │
│  • Ensure verification tasks completed                   │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  GATE 3: LLM VERIFICATION                                │
│  • Independent verifier subagent (fresh context)         │
│  • Skeptical auditor with no conversation history        │
│  • Returns verdict: verified | refuted | insufficient    │
│  • Detects fabrication indicators                        │
└─────────────────────────────────────────────────────────┘
```

### Verifier Subagent

The LLM verification gate spawns an independent `verifier` subagent:

```python
SubAgentConfig(
    name="verifier",
    description="Independent verification of claims",
    model=VERIFICATION_MODEL,  # Sonnet by default
    temperature=0.0,           # Deterministic
    allowed_tools=["file_ops", "search", "bash"]
)
```

Key properties:
- **Fresh context**: No conversation history (prevents bias)
- **Adversarial**: Defaults to "insufficient" verdict
- **Read-only**: Can read files and run verification commands but not modify

### Configuration

Configure gates in `OrchestratorConfig`:

```python
OrchestratorConfig(
    enable_data_gate=True,       # Verify data provenance
    data_gate_strict=True,       # Block on failure (vs warn)
    enable_exec_gate=True,       # Verify execution
    exec_gate_strict=True,
    enable_verification=True,    # LLM verification
    verification_strict=True,
    verification_threshold=0.7   # Confidence threshold
)
```

### Content Validation

`ContentValidator` in `tools/atomic/todo.py` detects fabrication patterns:
- HTML in data files (downloaded error page instead of data)
- Placeholder values (suspiciously round numbers)
- Error messages in output (404, access denied, stack traces)
- Invalid CSV structure

### TodoItem Verification

Tasks can request verification via the `verify` flag:

```python
TodoItem(
    content="Analyze protein fitness data",
    produces="file:results.csv:csv:100",  # Expect CSV with 100 rows
    verify=True  # Run LLM verification on completion
)
```

## Service Registry

Scientific services in `src/sciagent/services/registry.yaml`:

```yaml
- name: rcwa
  image: ghcr.io/sciagent-ai/rcwa
  capabilities: ["RCWA simulation", "photonic crystals"]
  timeout: 300
```

Resolution order: local image → pull from GHCR → build from Dockerfile

Services run in Docker with workspace mounted:
```bash
docker run --rm -v "$(pwd)":/workspace -w /workspace ghcr.io/sciagent-ai/rcwa python3 script.py
```
