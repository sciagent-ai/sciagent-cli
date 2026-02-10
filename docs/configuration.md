---
layout: default
title: Configuration
nav_order: 3
---

# Configuration

Configure SciAgent via command-line flags or Python.

## Models

### Default Model

SciAgent uses Claude Opus as the default. Change it with `--model`:

```bash
sciagent --model openai/gpt-4o "Summarize README.md"
sciagent --model anthropic/claude-sonnet-4-20250514 "Fix the bug in main.py"
```

Supported providers (via [litellm](https://github.com/BerriAI/litellm)): OpenAI, Anthropic, Google, and custom endpoints.

### Model Tiers

SciAgent uses three model tiers for cost-effective operation. Configure in `src/sciagent/defaults.py`:

| Tier | Variable | Purpose |
|------|----------|---------|
| Scientific | `SCIENTIFIC_MODEL` | Main agent, planning (best quality) |
| Coding | `CODING_MODEL` | Debug, research, general sub-agents |
| Fast | `FAST_MODEL` | Explore sub-agent (speed/cost) |

The main agent uses `DEFAULT_MODEL` (set to `SCIENTIFIC_MODEL`). Sub-agents use tier-appropriate models automatically.

### Model Parameters

```bash
sciagent --temperature 0.7 "Generate creative function names"  # More random
sciagent --temperature 0 "Refactor this code"                  # Deterministic
sciagent --max-iterations 50 "Quick task"                      # Limit cycles
```

## System Prompts

Override the default behavior with a custom prompt:

```bash
sciagent --system-prompt my_prompt.txt "Translate comments to Spanish"
```

## Custom Tools

Add your own tools by creating a Python module:

```python
# my_tools.py
from sciagent.tools import tool, ToolResult

@tool(name="count_lines", description="Count lines in a file")
def count_lines(path: str) -> ToolResult:
    with open(path) as f:
        return ToolResult(success=True, output=str(sum(1 for _ in f)))

TOOLS = [count_lines]
```

Load it:

```bash
sciagent --load-tools my_tools.py "How many lines in main.py?"
```

## Sub-agents

Enable specialized agents for research, review, and testing:

```bash
sciagent --subagents "Research this codebase and write tests"
```

Built-in sub-agents (each uses a cost-optimised model tier):

| Name | Model Tier | Purpose |
|------|------------|---------|
| `explore` | Fast | Quick codebase searches and file lookups |
| `debug` | Coding | Error investigation with web research |
| `research` | Coding | Web research, documentation lookup |
| `plan` | Scientific | Break down complex problems |
| `general` | Coding | Complex multi-step tasks |

Model tiers are defined in `src/sciagent/defaults.py`. See [Sub-agents](developers/architecture.md#sub-agents) for customization.

## Scientific Services

SciAgent runs simulations in Docker containers. Available services:

| Domain | Services |
|--------|----------|
| Math | scipy-base, sympy, cvxpy |
| Chemistry | rdkit, gromacs, ase |
| Photonics | rcwa, meep |
| CFD | openfoam, gmsh, elmer |
| Circuits | ngspice |
| Quantum | qiskit |
| Bio | biopython, blast |

```bash
sciagent "Run an RCWA simulation for a photonic grating"
```

The agent automatically researches documentation, writes code, and runs it in the appropriate container.

## Python Usage

```python
from sciagent import create_agent, DEFAULT_MODEL

agent = create_agent(model=DEFAULT_MODEL, working_dir="./project")
result = agent.run("Analyze this codebase")
```

For detailed Python API, see [API Reference](developers/api-reference.md).
