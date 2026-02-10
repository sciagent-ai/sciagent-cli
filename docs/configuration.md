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

Built-in sub-agents:

| Name | Purpose |
|------|---------|
| `researcher` | Web and code research |
| `reviewer` | Code review |
| `test_writer` | Generate tests |
| `general` | General tasks |

Sub-agents inherit the parent's model. See [Sub-agents](developers/architecture.md#sub-agents) for customization.

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
