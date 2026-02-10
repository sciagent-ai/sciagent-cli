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
print(DEFAULT_MODEL)  # "anthropic/claude-opus-4-5-20251101"
```

Change globally in `src/sciagent/defaults.py`.

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
    parent_model="anthropic/claude-sonnet-4-20250514"
)

result = orch.spawn("researcher", "Find API endpoints")
```

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
