---
layout: default
title: Configuration
nav_order: 3
---

# Configuration

SciAgent is highly configurable.  You can fine‑tune the language model, system prompts, caching behaviour, tool registry and even introduce custom sub‑agents.  This page explains the main configuration options available via the command‑line and Python APIs.

## Default model

SciAgent uses a single source of truth for the default model, defined in `sciagent.defaults.DEFAULT_MODEL`.  This makes it easy to change the default across the entire codebase by editing one constant:

```python
from sciagent import DEFAULT_MODEL
print(DEFAULT_MODEL)  # "anthropic/claude-sonnet-4-20250514"
```

To use a different default model globally, modify `src/sciagent/defaults.py`:

```python
# defaults.py
DEFAULT_MODEL = "openai/gpt-4o"  # Change this to update the default everywhere
```

## Choosing a language model

The `--model` flag controls which large‑language model is used.  SciAgent leverages the [litellm](https://github.com/BerriAI/litellm) library and therefore supports providers such as OpenAI, Anthropic, Google and open models served via custom endpoints.  For example:

```bash
sciagent --project-dir ./foo --model openai/gpt-4o "Summarise the contents of README.md"
```

When embedding SciAgent within Python, create an `AgentConfig` and supply the model name:

```python
from sciagent.agent import AgentConfig, AgentLoop
from sciagent import DEFAULT_MODEL

# Use the default model
config = AgentConfig(model=DEFAULT_MODEL, working_dir="./project")
agent = AgentLoop(config=config)
agent.run("Describe the main function in src/main.py")

# Or specify a different model
config = AgentConfig(model="openai/gpt-4o", working_dir="./project")
```

### Temperature and iteration limits

Use `--temperature` to control the randomness of the model’s responses.  A lower value (e.g. `0`) yields deterministic output, whereas higher values (up to `1`) encourage creativity.  `--max-iterations` sets a hard cap on the number of Think → Act → Observe cycles the agent will perform.  (default: 120).  Decrease it for simpler tasks to save tokens, or increase further for very complex multi‑step workflows.

Note that sub‑agents use a lower default of 20 iterations since they handle focused, isolated tasks.

### System prompts

The **system prompt** sets the tone and instructions for the language model.  SciAgent ships with a default system prompt that encourages safety, careful reasoning and use of tools.  You can override it by passing `--system-prompt PATH` pointing to a text file:

```bash
sciagent --project-dir ./bar --system-prompt custom_prompt.txt "Translate code comments to Spanish"
```

The file should contain plain text.  For complex behaviour you may include guidelines, style rules or domain knowledge.  In Python, supply a custom prompt string via the `system_prompt` attribute of `AgentConfig`.

## Caching and API keys

SciAgent caches LLM responses to avoid redundant API calls.  Caching is enabled by default for Anthropic models through litellm’s prompt‑caching.  To configure caching manually, call `configure_cache()` from `sciagent.llm` before constructing your agent:

```python
from sciagent.llm import configure_cache

# Enable in‑memory caching
configure_cache("local")

# Or use Redis (assuming a local Redis server running)
configure_cache("redis")

# Clear cached prompts when needed
configure_cache(None)
```

Authentication credentials for different providers should be exported as environment variables according to litellm’s conventions (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).  When using self‑hosted models, specify `base_url` and `api_key` within `AgentConfig`.

## Managing the tool registry

The default registry includes atomic tools such as `bash`, `file_ops`, `search`, `web`, `todo`, `skill` and `ask_user`. You can load additional tools at runtime by writing a Python module that defines a `register_tools(registry)` function or exposes a `TOOLS` list.  Each tool must subclass `BaseTool` or be decorated with `@tool` from `sciagent.tools`.

Example of a custom tool module (`my_tools.py`):

```python
from sciagent.tools import tool
from sciagent.tools import ToolResult

@tool(name="count_lines", description="Count lines in a file")
def count_lines(path: str) -> ToolResult:
    with open(path) as f:
        num = sum(1 for _ in f)
    return ToolResult(success=True, output=str(num))

TOOLS = [count_lines]
```

Load it via the CLI:

```bash
sciagent --project-dir ./baz --load-tools my_tools.py "How many lines are in src/main.py?"
```

In Python, you can create a registry manually and register tools before instantiating the agent:

```python
from sciagent.tools import create_default_registry
from my_tools import count_lines

registry = create_default_registry(working_dir="./project")
registry.register(count_lines)

config = AgentConfig(project_dir="./project", tool_registry=registry)
agent = AgentLoop(config=config)
```

### Disabling or replacing tools

If you wish to restrict the agent’s capabilities, you can unregister tools from the registry or replace them with your own implementations.  For example, remove web access when working offline:

```python
registry = create_default_registry(working_dir=".")
registry.unregister("web")
```

## Configuring sub‑agents

Sub‑agents are specialised agents with their own context window and tool set.  Use the `--subagents` flag to enable sub‑agent spawning from the CLI.  To add custom sub‑agents, import `SubAgentConfig` and register them in `SubAgentRegistry`.  Each configuration specifies a name, description, system prompt, model, iteration budget and list of allowed tools.

### Model inheritance

By default, sub‑agents **inherit the parent agent's model**.  This means if you run the CLI with `--model openai/gpt-4o`, all spawned sub‑agents will also use GPT‑4o rather than the hardcoded default.  This ensures consistent model usage across your entire agent hierarchy.

```bash
# Parent and all sub-agents will use GPT-4o
sciagent --subagents --model openai/gpt-4o "Research and analyze this codebase"
```

### Custom sub‑agent models

You can override model inheritance by providing a custom `SubAgentConfig` with an explicit model:

```python
from sciagent.subagent import SubAgentConfig, SubAgentRegistry

# This sub-agent will always use GPT-4o, regardless of parent's model
my_agent = SubAgentConfig(
    name="matlab_helper",
    description="Assists with MATLAB simulation tasks",
    system_prompt="You are an expert MATLAB engineer.",
    model="openai/gpt-4o",  # Explicit model overrides inheritance
    max_iterations=20,
    allowed_tools=["bash", "file_ops", "service"]
)

registry = SubAgentRegistry()
registry.register(my_agent)
```

Built‑in sub‑agents (researcher, reviewer, test_writer, general) use the parent's model by default.  Custom sub‑agents with explicit model settings will use their configured model.

Enable sub‑agents in the main agent by passing `--subagents` and referencing your sub‑agent's name when constructing tasks in the todo graph.

## Services and environments

The `service` tool interacts with containerised scientific environments (e.g. SciPy, RCWA, OpenFOAM).  A YAML registry (`src/sciagent/services/registry.yaml`) defines each service’s image, dependencies and capabilities.  To add new services, extend this file and rebuild the container images.  When using the CLI, specify `action="run"` along with the service name and code to execute.

```bash
sciagent --project-dir ./opt --service-run "service=rcwa; code=simulate_grating.py"
```

See the [service registry](architecture.md#service-registry) section for details on available services and how to use them.
