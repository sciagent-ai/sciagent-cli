---
layout: default
title: API Reference
nav_order: 6
---

# API Reference

This document covers the Python API for using SciAgent programmatically.

## Quick Start

```python
from sciagent import create_agent, run_task, DEFAULT_MODEL

# Simple one-shot execution
result = run_task("Create a hello world script")

# Configured agent with default model
agent = create_agent(model=DEFAULT_MODEL)
result = agent.run("Analyze this codebase")

# Or use a specific model
agent = create_agent(model="openai/gpt-4o")
result = agent.run("Analyze this codebase")
```

## Constants

### DEFAULT_MODEL

The default LLM model used throughout the framework.

```python
from sciagent import DEFAULT_MODEL

print(DEFAULT_MODEL)  # "anthropic/claude-sonnet-4-20250514"
```

To change the default model globally, edit `src/sciagent/defaults.py`.

## Core Classes

### AgentLoop

The main agent class that implements the think-act-observe loop.

```python
from agent import AgentLoop, AgentConfig

agent = AgentLoop(
    config=AgentConfig(),      # Agent configuration
    tools=None,                # ToolRegistry (default: built-in tools)
    llm=None,                  # LLMClient (default: from config)
    system_prompt=None,        # Override system prompt
    display=None               # Display instance
)
```

#### Methods

**run(task, max_iterations=None) -> str**

Execute a task and return the result.

```python
result = agent.run("Create a Python script")
result = agent.run("Complex task", max_iterations=50)
```

**run_interactive()**

Start an interactive REPL session.

```python
agent.run_interactive()
# Type tasks at the prompt
# Commands: 'status', 'clear', 'exit'
```

**save_session() -> str**

Save current session and return session ID.

```python
session_id = agent.save_session()
```

**load_session(session_id) -> bool**

Load a previous session.

```python
if agent.load_session("abc123"):
    agent.run("Continue from where we left off")
```

**list_sessions() -> List[Dict]**

List all saved sessions.

```python
sessions = agent.list_sessions()
for s in sessions:
    print(f"{s['session_id']}: {s['task_count']} tasks")
```

#### Callbacks

Register callbacks to observe agent behavior:

```python
agent.on_tool_start(lambda name, args: print(f"Starting {name}"))
agent.on_tool_end(lambda name, result: print(f"Finished {name}"))
agent.on_thinking(lambda text: print(f"Thinking: {text[:100]}"))
agent.on_response(lambda text: print(f"Response: {text[:100]}"))
```

---

### AgentConfig

Configuration dataclass for agent behavior.

```python
from sciagent import AgentConfig, DEFAULT_MODEL

config = AgentConfig(
    model=DEFAULT_MODEL,         # LLM model (defaults to DEFAULT_MODEL)
    temperature=0.0,             # Sampling temperature
    max_tokens=16384,            # Max tokens per response
    max_iterations=120,          # Max loop iterations
    working_dir=".",             # Working directory
    verbose=True,                # Verbose output
    auto_save=True,              # Auto-save sessions
    state_dir=".agent_states"    # State storage path
)
```

The `model` parameter defaults to `DEFAULT_MODEL` if not specified.

---

### ToolRegistry

Registry for managing available tools.

```python
from tools import ToolRegistry, create_default_registry

# Create with default tools
registry = create_default_registry(working_dir="./project")

# Or create empty and add tools
registry = ToolRegistry()
registry.register(my_tool)
```

#### Methods

**register(tool)**

Register a tool (BaseTool instance or callable).

```python
registry.register(MyCustomTool())
registry.register(my_function, name="my_func")
```

**unregister(name)**

Remove a tool.

```python
registry.unregister("my_tool")
```

**execute(name, **kwargs) -> ToolResult**

Execute a tool by name.

```python
result = registry.execute("bash", command="ls -la")
```

**list_tools() -> List[str]**

List registered tool names.

```python
print(registry.list_tools())
# ['bash', 'view', 'write_file', ...]
```

**get_schemas() -> List[Dict]**

Get tool schemas for LLM.

```python
schemas = registry.get_schemas()
```

---

### ToolResult

Result from tool execution.

```python
from tools import ToolResult

result = ToolResult(
    success=True,        # Whether execution succeeded
    output="Output",     # Result output (any type)
    error=None           # Error message if failed
)

# Check result
if result.success:
    print(result.output)
else:
    print(f"Error: {result.error}")

# Format for LLM
message = result.to_message()
```

---

### BaseTool

Base class for creating custom tools.

```python
from tools import BaseTool, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "Description shown to LLM"
    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Input parameter"
            }
        },
        "required": ["input"]
    }

    def execute(self, input: str) -> ToolResult:
        try:
            result = process(input)
            return ToolResult(success=True, output=result)
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))
```

---

## Convenience Functions

### run_task

One-shot task execution.

```python
from sciagent import run_task, DEFAULT_MODEL

result = run_task(
    task="Create a script",
    model=DEFAULT_MODEL,     # Defaults to DEFAULT_MODEL
    tools=None,              # Use default tools
    working_dir=".",
    verbose=True
)
```

### create_agent

Create a configured agent instance.

```python
from sciagent import create_agent, DEFAULT_MODEL

agent = create_agent(
    model=DEFAULT_MODEL,     # Defaults to DEFAULT_MODEL
    tools=None,
    working_dir=".",
    system_prompt=None,
    verbose=True
)
```

### create_agent_with_subagents

Create an agent with sub-agent support. Sub-agents inherit the specified model.

```python
from sciagent import create_agent_with_subagents, DEFAULT_MODEL

agent = create_agent_with_subagents(
    model=DEFAULT_MODEL,     # Parent and sub-agents use this model
    working_dir="./project",
    verbose=True
)

# Or use a specific model - sub-agents will inherit it
agent = create_agent_with_subagents(
    model="openai/gpt-4o",   # All sub-agents also use GPT-4o
    working_dir="./project",
    verbose=True
)
```

---

## Sub-Agent Classes

### SubAgentConfig

Configuration for a sub-agent type.

```python
from sciagent import SubAgentConfig, DEFAULT_MODEL

config = SubAgentConfig(
    name="researcher",
    description="Research specialist",
    system_prompt="You are a research specialist...",
    model=DEFAULT_MODEL,                    # Defaults to DEFAULT_MODEL
    max_iterations=20,
    allowed_tools=["file_ops", "search", "web"],
    temperature=0.0
)
```

### SubAgentOrchestrator

Orchestrates sub-agent spawning with model inheritance.

```python
from sciagent.subagent import SubAgentOrchestrator
from sciagent.tools import create_default_registry

orch = SubAgentOrchestrator(
    tools=create_default_registry("./project"),
    working_dir="./project",
    max_workers=4,
    parent_model="openai/gpt-4o"  # Sub-agents inherit this model
)

# Spawned sub-agents use parent_model by default
result = orch.spawn("researcher", "Find API endpoints")
```

When `parent_model` is set, sub-agents spawned from the registry automatically inherit this model. Custom configs with explicit models override inheritance.

### SubAgentResult

Result from sub-agent execution.

```python
from subagent import SubAgentResult

result = SubAgentResult(
    success=True,
    output="Research findings...",
    artifacts=["_outputs/data.json"],
    tokens_used=5000,
    iterations=3
)
```

---

## State Classes

### TodoList

Task tracking with status management.

```python
from state import TodoList, TodoItem, TodoStatus

todos = TodoList()
todos.add(TodoItem(description="First task"))
todos.update_status("task_1", TodoStatus.IN_PROGRESS)

# Query
pending = todos.get_by_status(TodoStatus.PENDING)
print(todos.to_string())
```

### StateManager

Persistence for agent sessions.

```python
from state import StateManager

manager = StateManager(state_dir=".agent_states")

# Save
manager.save(agent.state)

# Load
state = manager.load(session_id)

# List
sessions = manager.list_sessions()
```

---

## LLM Classes

### LLMClient

Wrapper for LiteLLM multi-model support.

```python
from sciagent.llm import LLMClient
from sciagent import DEFAULT_MODEL

client = LLMClient(
    model=DEFAULT_MODEL,     # Defaults to DEFAULT_MODEL
    temperature=0.0,
    max_tokens=16384
)

response = client.chat(
    messages=[{"role": "user", "content": "Hello"}],
    tools=[]  # Tool schemas
)
```

### LLMResponse

Structured response from LLM.

```python
response.content       # Text content
response.tool_calls    # List[ToolCall]
response.has_tool_calls  # bool
response.usage         # Token usage dict
```

### ToolCall

Parsed tool invocation.

```python
tool_call.id          # Unique ID
tool_call.name        # Tool name
tool_call.arguments   # Dict of arguments
```
