---
layout: default
title: Sub Agents
nav_order: 6
---

# Sub-Agents

Sub-agents are specialized agent instances that can be spawned by the main agent to handle specific types of work in parallel.

## Overview

When enabled with `--subagents`, the main agent can delegate work to specialized sub-agents:

- **Researcher**: Read-only access, focused on information gathering
- **Test Writer**: Code generation focused on tests
- **Reviewer**: Analysis and code review
- **General**: Full tool access for miscellaneous tasks

Each sub-agent:
- Has its own context window (isolated from main agent)
- Has restricted tool access based on its role
- Cannot spawn further sub-agents (no recursion)
- Returns results back to the main agent

## Usage

### CLI

```bash
python main.py --subagents "Research best practices and implement a logging module"
```

### Python API

```python
from subagent import create_agent_with_subagents

agent = create_agent_with_subagents(
    model="anthropic/claude-sonnet-4-20250514",
    working_dir="./my-project",
    verbose=True
)

result = agent.run("Research authentication patterns and write unit tests")
```

## Built-in Sub-Agent Types

### researcher

**Purpose**: Information gathering, reading documentation, searching

**Tools Available**: view, search, web

**Best For**:
- Researching best practices
- Reading and understanding existing code
- Finding documentation and examples
- Literature review

**Example Task**: "Research how React handles state management"

---

### test_writer

**Purpose**: Writing tests for existing code

**Tools Available**: view, search, write_file, bash

**Best For**:
- Writing unit tests
- Creating integration tests
- Test file generation

**Example Task**: "Write tests for the authentication module"

---

### reviewer

**Purpose**: Code analysis and review

**Tools Available**: view, search

**Best For**:
- Code review
- Finding bugs and issues
- Security analysis
- Performance analysis

**Example Task**: "Review the API endpoints for security issues"

---

### general

**Purpose**: General-purpose tasks

**Tools Available**: All tools

**Best For**:
- Tasks that don't fit other categories
- Complex tasks requiring multiple capabilities

**Example Task**: "Refactor the database module"

## How Sub-Agents Work

### Task Delegation

The main agent decides when to spawn sub-agents based on the task:

```
Main Agent: "Research best practices and implement logging"
├── Spawns: researcher sub-agent
│   └── Searches web, reads docs, returns findings
├── Uses research results to plan implementation
└── Implements logging module
```

### Context Isolation

Each sub-agent has its own conversation history:
- Doesn't see the main agent's full context
- Receives only the specific task description
- Returns only the final result to the main agent

This keeps context windows manageable for complex tasks.

### Result Passing

Sub-agents return structured results:

```python
SubAgentResult(
    success=True,
    output="Research findings: ...",
    artifacts=["_outputs/research.json"],
    tokens_used=5000,
    iterations=3
)
```

## Custom Sub-Agents

You can define custom sub-agent configurations:

```python
from subagent import SubAgentConfig, SubAgentRegistry, SubAgentOrchestrator

# Define a custom sub-agent type
data_analyst = SubAgentConfig(
    name="data_analyst",
    system_prompt="You are a data analysis specialist. Focus on pandas, numpy, and visualization.",
    allowed_tools=["view", "search", "bash", "write_file"],
    model="anthropic/claude-sonnet-4-20250514"
)

# Register it
registry = SubAgentRegistry()
registry.register(data_analyst)

# Use in orchestrator
orchestrator = SubAgentOrchestrator(
    main_agent=main_agent,
    registry=registry
)
```

## Best Practices

### When to Use Sub-Agents

- **Large codebases**: Delegate exploration to researcher
- **Test coverage**: Use test_writer for systematic test generation
- **Code review**: Use reviewer for focused analysis
- **Research tasks**: Use researcher to gather information first

### When NOT to Use Sub-Agents

- **Simple tasks**: Overhead not worth it for quick tasks
- **Highly interdependent work**: When tasks need constant coordination
- **Small codebases**: Main agent can handle directly

### Effective Prompts

Good:
```
"Research Python logging best practices, then implement a logging module with file rotation"
```

Less effective:
```
"Do some logging stuff"
```

Be specific about what the sub-agent should focus on.
