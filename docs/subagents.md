---
title: Sub‑agents
 nav_order: 7
---

# Sub‑agent system

SciAgent supports **sub‑agents**—specialised agents that operate in isolation from the main agent.  Each sub‑agent has its own context window, system prompt and tool set.  This isolation prevents runaway context growth and lets you delegate tasks to dedicated roles.

## Why use sub‑agents?

* **Encapsulation:** A sub‑agent does not share memory with its parent.  It receives a task description, runs its own agent loop and returns only the final result.  Intermediate reasoning and tool calls remain hidden.
* **Specialisation:** Different tasks require different prompts and tools.  A research agent might need web access, whereas a reviewer should only read code.  Sub‑agent configurations reflect these roles.
* **Parallelism:** Multiple sub‑agents can execute concurrently when orchestrated by a `SubAgentOrchestrator`, enabling parallel research or analysis.

## Built‑in sub‑agents

SciAgent provides a few ready‑made sub‑agents.  Each is defined by a `SubAgentConfig` containing a name, description, system prompt, model, iteration limit and allowed tools.

| Name | Purpose | Allowed tools |
|---|---|---|
| `researcher` | Explore codebases and search the web.  Summarises findings without modifying files. | `file_ops`, `search`, `web`, `bash` |
| `reviewer` | Read code and produce structured reviews with critical issues, warnings and suggestions. | `file_ops`, `search`, `bash` |
| `test_writer` | Write comprehensive unit tests for existing code. | `file_ops`, `search`, `bash` |
| `general` | Versatile assistant for tasks not covered above.  Uses all default tools and has a longer iteration budget. | all default tools |

You can list these sub‑agents from Python via `SubAgentRegistry().list_agents()`.

## Creating and running sub‑agents

To spawn a sub‑agent directly, construct a `SubAgent` and call its `run()` method:

```python
from sciagent.subagent import SubAgent, SubAgentConfig, SubAgentRegistry
from sciagent.tools import create_default_registry

registry = create_default_registry("~/my-project")
config = SubAgentRegistry().get("researcher")
agent = SubAgent(config=config, tools=registry, working_dir="~/my-project")
result = agent.run("Summarise the purpose of this repository")
print(result.output)
```

The returned `SubAgentResult` includes `success`, `output`, `error`, `iterations`, `tokens_used`, `duration_seconds` and a `session_id` for resumption.

## Orchestrating multiple sub‑agents

The `SubAgentOrchestrator` class can run several sub‑agents sequentially or in parallel.  It maintains a registry, tracks active sessions and aggregates results.  To run tasks in parallel:

```python
from sciagent.subagent import SubAgentOrchestrator
from sciagent.tools import create_default_registry

tasks = [
    {"agent_name": "researcher", "task": "Find recent papers on metasurfaces"},
    {"agent_name": "test_writer", "task": "Write tests for the new API"},
]
orch = SubAgentOrchestrator(tools=create_default_registry("~/my-project"))
results = orch.spawn_parallel(tasks)
for r in results:
    print(r.agent_name, r.success)
```

## Custom sub‑agents

You can define your own specialised agents by creating a `SubAgentConfig` and registering it in a `SubAgentRegistry`:

```python
from sciagent.subagent import SubAgentConfig, SubAgentRegistry

registry = SubAgentRegistry()
custom = SubAgentConfig(
    name="data_scientist",
    description="Perform data analysis tasks", 
    system_prompt="""You are a data scientist.  Load data, perform statistics and visualise results.""",
    allowed_tools=["file_ops", "search", "bash"],
)
registry.register(custom)
```

When you run the CLI with `--subagents`, the main agent can call sub‑agents implicitly.  You can instruct the LLM to use a particular sub‑agent by asking it to “delegate research to the researcher sub‑agent” or similar.