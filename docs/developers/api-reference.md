---
layout: default
title: API Reference
parent: Developer Documentation
nav_order: 2
---

# API Reference

Python API for using SciAgent programmatically.

## Quick Start

```python
from sciagent import create_agent, run_task, DEFAULT_MODEL

# One-shot execution
result = run_task("Create a hello world script")

# Configured agent
agent = create_agent(model=DEFAULT_MODEL, working_dir="./project")
result = agent.run("Analyze this codebase")
```

## Constants

### DEFAULT_MODEL

```python
from sciagent import DEFAULT_MODEL
print(DEFAULT_MODEL)  # "anthropic/claude-sonnet-4-6"
```

Resolves to the `SCIENTIFIC_MODEL` tier. Change globally in `src/sciagent/defaults.py`. The full tier set: `SCIENTIFIC_MODEL`, `CODING_MODEL`, `FAST_MODEL`, `VISION_MODEL`, `VERIFICATION_MODEL`.

## Core Classes

### AgentLoop

```python
from sciagent.agent import AgentLoop, AgentConfig

agent = AgentLoop(config=AgentConfig())
result = agent.run("Create a Python script")
```

**Methods:**
- `run(task, max_iterations=None)` - Execute task, return result
- `run_interactive()` - Start REPL session
- `save_session()` - Save and return session ID
- `load_session(session_id)` - Load previous session

**Callbacks:**
```python
agent.on_tool_start(lambda name, args: print(f"Starting {name}"))
agent.on_tool_end(lambda name, result: print(f"Finished {name}"))
```

### AgentConfig

```python
from sciagent import AgentConfig, DEFAULT_MODEL

config = AgentConfig(
    model=DEFAULT_MODEL,
    temperature=0.0,
    max_tokens=16384,
    max_iterations=120,
    working_dir=".",
    verbose=True,
    auto_save=True
)
```

### ToolRegistry

```python
from sciagent.tools import ToolRegistry, create_default_registry

registry = create_default_registry(working_dir="./project")
registry.register(my_tool)
registry.unregister("web")
result = registry.execute("bash", command="ls")
schemas = registry.get_schemas()  # For LLM
```

### ToolResult

```python
from sciagent.tools import ToolResult

result = ToolResult(success=True, output="data", error=None)
if result.success:
    print(result.output)
```

### BaseTool

```python
from sciagent.tools import BaseTool, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "Description for LLM"
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input"}
        },
        "required": ["input"]
    }

    def execute(self, input: str) -> ToolResult:
        return ToolResult(success=True, output=process(input))
```

## Convenience Functions

### run_task

```python
from sciagent import run_task

result = run_task(
    task="Create a script",
    model=DEFAULT_MODEL,
    working_dir=".",
    verbose=True
)
```

### create_agent

```python
from sciagent import create_agent

agent = create_agent(
    model=DEFAULT_MODEL,
    working_dir=".",
    system_prompt=None,
    verbose=True
)
```

### create_agent_with_subagents

```python
from sciagent import create_agent_with_subagents

agent = create_agent_with_subagents(
    model=DEFAULT_MODEL,  # Sub-agents inherit this
    working_dir="./project"
)
```

## Sub-agent Classes

### SubAgentConfig

```python
from sciagent.subagent import SubAgentConfig

config = SubAgentConfig(
    name="researcher",
    description="Research specialist",
    system_prompt="You are a researcher...",
    model=None,  # Inherits parent model
    max_iterations=20,
    allowed_tools=["file_ops", "search", "web"]
)
```

### SubAgentOrchestrator

```python
from sciagent.subagent import SubAgentOrchestrator

orch = SubAgentOrchestrator(
    tools=registry,
    working_dir="./project",
    parent_model="anthropic/claude-sonnet-4-6"
)

# Foreground (synchronous) — returns SubAgentResult
result = orch.spawn("researcher", "Find API endpoints")

# Background — registers in task_index, returns task_id
task_id = orch.spawn(
    agent_name="analyze",
    task="KDE plot of T field at z=0.1m",
    background=True,
    produces_uris=["./_outputs/kde_z01.png"],   # validated post-return
    resume_task_id=None,                         # set to a prior task_id to resume
)

# Parallel
results = orch.spawn_parallel([
    {"agent_name": "research", "task": "Find S4 docs"},
    {"agent_name": "debug", "task": "Investigate build error"},
])
```

When `produces_uris=` is declared, the orchestrator validates that each pattern resolves to at least one file with size ≥ `produces_min_bytes` (default 100). Failure transitions the task to state `blocked_produce_missing`.

## Compute

Cloud and local container job orchestration. See [Cloud Compute](../cloud-compute.md) for the user-facing guide.

### Tools

```python
# Tools live in sciagent.tools.atomic and are registered automatically by
# create_atomic_registry(). Direct Python use:
from sciagent.tools.atomic.compute import ComputeTool
from sciagent.tools.atomic.compute_exec import ComputeExecTool
from sciagent.tools.atomic.compute_cluster import ComputeClusterTool
from sciagent.tools.atomic.materialize import MaterializeTool
from sciagent.tools.atomic.materialize_workspace import MaterializeWorkspaceTool

run = ComputeTool(working_dir=".")
result = run.execute(
    service="openfoam",
    command="bash Allrun",
    mode="cluster",
    backend="skypilot",
    cluster_name="cfd-run-1",
    cpus=4,
    memory_gb=32,
)
```

### Compute router & job model

```python
from sciagent.compute.router import ComputeRouter
from sciagent.compute.job import Job, JobResult, JobStatus, ComputeRequirements

requirements = ComputeRequirements(cpus=4, memory_gb=32, gpus=0, gpu_type=None)
job = Job(image="ghcr.io/sciagent-ai/openfoam", command="bash Allrun",
          requirements=requirements)

router = ComputeRouter()
result: JobResult = router.run(job)
```

### Task index

```python
from sciagent.compute.task_index import (
    read_task, write_task, get_task, list_tasks, update_task_state,
    delete_task, kind_of, manifest_dir, manifest_path,
    KNOWN_KINDS, VALID_STATES, TERMINAL_STATES, RESUMABLE_STATES,
)

# Query
running_jobs = list_tasks(kind="compute_job", state="running")
record = get_task("sciagent-abc123")              # on-disk shape
kind = kind_of("sciagent-abc123")                  # "compute_job" | "subagent" | "local"

# Lifecycle
update_task_state("sciagent-abc123", state="completed",
                  result_summary="500 SIMPLE iterations")
```

`KNOWN_KINDS = ("compute_job", "subagent")`. `TERMINAL_STATES = ("completed", "failed", "cancelled", "blocked_produce_missing")`. `RESUMABLE_STATES = ("crashed", "blocked_resume")` — subagent-only.

## Checkpoint

```python
from sciagent.checkpoint import SubagentCheckpoint

# Subagent runs write checkpoints automatically when spawned via
# SubAgentOrchestrator.spawn(...). The on-disk shape:
#   ~/.sciagent/sessions/<session_id>/subagents/<task_id>/
#       checkpoint.jsonl   # per-iteration events
#       agent_state.json   # full state snapshot

# To resume a crashed task, pass its task_id back into spawn:
result = orch.spawn(
    agent_name="analyze",
    task="<same description as the crashed run>",
    resume_task_id="<prior task_id>",
)
```

When a fresh `spawn(...)` matches a prior `crashed` / `blocked_resume` entry by description hash, the orchestrator prompts the user with a 3-way choice (`skip` / `use_prior` / `retry`).

## Provenance Log

```python
from sciagent.provenance_log import ProvenanceLog, get_provenance_log

# Get-or-create the per-session log
log = get_provenance_log(session_id="abc12345")

# Emit events (called automatically by AgentLoop, ComputeTool, verify_session)
log.append({
    "event_kind": "tool_call",
    "tool_name": "compute_run",
    "arguments": {...},
    "session_id": "abc12345",
})

# Snapshot read
events = log.read_events()
```

Schema version `1`. Per-line cap 16 KB; per-field cap 4 KB. Thread-safe via `fcntl.flock`. See [Provenance Log Schema](../provenance_log_schema.md).

### verify_session

```python
from sciagent.tools.atomic.verify import verify_session

report = verify_session(session_id="abc12345")
# {
#   "session_id": "abc12345",
#   "events_by_kind": {...},
#   "compute_jobs": [...],
#   "artifacts": [...],
#   "verifications": [...],
#   "summary_issues": [...],
# }
```

Snapshot, non-blocking, one-shot. A second invocation on the same session produces a fresh snapshot.

## LLM Classes

### LLMClient

```python
from sciagent.llm import LLMClient

client = LLMClient(model=DEFAULT_MODEL, temperature=0.0)
response = client.chat(
    messages=[{"role": "user", "content": "Hello"}],
    tools=[]
)

# Response structure
response.content      # Text
response.tool_calls   # List[ToolCall]
response.usage        # Token usage
```

### configure_cache

```python
from sciagent.llm import configure_cache

configure_cache(cache_type="local")      # In-memory
configure_cache(cache_type="redis")      # Redis
configure_cache(enabled=False)           # Disable
```

## State Classes

### TodoList

```python
from sciagent.state import TodoList, TodoItem, TodoStatus

todos = TodoList()
todos.add(TodoItem(description="First task"))
todos.update_status("task_1", TodoStatus.IN_PROGRESS)
pending = todos.get_by_status(TodoStatus.PENDING)
```

### StateManager

```python
from sciagent.state import StateManager

manager = StateManager(state_dir=".agent_states")
manager.save(agent.state)
state = manager.load(session_id)
sessions = manager.list_sessions()
```
