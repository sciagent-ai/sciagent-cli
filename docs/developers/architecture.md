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
Full-featured tools in `sciagent.tools.atomic`:
- `bash` - Shell execution with timeouts
- `file_ops` - Read/write/replace/list
- `search` - Glob and grep
- `web` - Search and fetch
- `todo` - Task graph management
- `skill` - Load workflow instructions
- `ask_user` - User interaction

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

Sub-agents are isolated agents with their own context and tool set. Defined by `SubAgentConfig`:

```python
SubAgentConfig(
    name="researcher",
    description="Research specialist",
    system_prompt="...",
    model=None,  # Inherits parent model
    max_iterations=20,
    allowed_tools=["file_ops", "search", "web"]
)
```

Built-in sub-agents:
| Name | Purpose | Tools |
|------|---------|-------|
| researcher | Code/web research | file_ops, search, web, bash |
| reviewer | Code review | file_ops, search, bash |
| test_writer | Generate tests | file_ops, search, bash |
| general | General tasks | all |

### Orchestration

`SubAgentOrchestrator` manages spawning and parallel execution:

```python
orch = SubAgentOrchestrator(tools=registry, parent_model="anthropic/claude-sonnet-4-20250514")
result = orch.spawn("researcher", "Find API endpoints")
results = orch.spawn_parallel([
    {"agent_name": "researcher", "task": "..."},
    {"agent_name": "test_writer", "task": "..."}
])
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
