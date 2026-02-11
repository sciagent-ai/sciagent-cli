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
sciagent --model openai/gpt-4.1 "Summarize README.md"
sciagent --model gemini/gemini-3-pro-preview "Analyze this diagram"
sciagent --model deepseek/deepseek-reasoner "Solve this physics problem"
```

Supported providers (via [litellm](https://github.com/BerriAI/litellm)): OpenAI, Anthropic, Google, and custom endpoints.

### Model Tiers

SciAgent uses four model tiers for cost-effective operation. Configure in `src/sciagent/defaults.py`:

| Tier | Variable | Purpose |
|------|----------|---------|
| Scientific | `SCIENTIFIC_MODEL` | Main agent, planning (best quality) |
| Vision | `VISION_MODEL` | Image and multimodal analysis |
| Coding | `CODING_MODEL` | Debug, research, general sub-agents |
| Fast | `FAST_MODEL` | Explore sub-agent (speed/cost) |

The main agent uses `DEFAULT_MODEL` (set to `SCIENTIFIC_MODEL`). The vision tier uses the same model as scientific for high-quality image analysis. Sub-agents use tier-appropriate models automatically.

### Alternative Models by Provider

SciAgent supports multiple LLM providers via [LiteLLM](https://github.com/BerriAI/litellm). Use `--model provider/model-name` to switch.

> **Note**: Only Anthropic models are tested. Alternatives below are based on comparable capabilities but have NOT been validated. Your mileage may vary.

| Tier | Anthropic (tested) | OpenAI | Google | xAI |
|------|-------------------|--------|--------|-----|
| **Scientific** | `claude-opus-4-5-20251101` | `gpt-4.1`, `o3`, `o3-pro` | `gemini-3-pro-preview`, `gemini-2.5-pro` | `grok-4-1-fast-reasoning` |
| **Vision** | `claude-opus-4-5-20251101` | `gpt-4.1`, `o3` | `gemini-3-pro-preview` | `grok-4-1-fast-reasoning`, `grok-2-vision-1212` |
| **Coding** | `claude-sonnet-4-20250514` | `gpt-4.1-mini`, `o4-mini` | `gemini-3-flash-preview`, `gemini-2.5-flash` | `grok-code-fast-1` |
| **Fast** | `claude-3-haiku-20240307` | `gpt-4.1-nano`, `o4-mini` | `gemini-2.5-flash-lite` | `grok-3-mini` |

**Open-Source alternatives** (via Together AI, Groq, or self-hosted):

| Tier | Models |
|------|--------|
| Scientific | `deepseek/deepseek-reasoner`, `together_ai/Qwen/Qwen3-235B-A22B-Instruct` |
| Vision | `together_ai/Qwen/Qwen2.5-VL-72B-Instruct`, `together_ai/meta-llama/Llama-3.2-90B-Vision-Instruct` |
| Coding | `deepseek/deepseek-chat`, `together_ai/meta-llama/Llama-3.3-70B-Instruct` |
| Fast | `groq/llama-3.3-70b-versatile`, `together_ai/Qwen/Qwen2.5-7B-Instruct` |

See `src/sciagent/defaults.py` for the full list with notes.

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

## Image Analysis

SciAgent can analyse images including scientific plots, microscopy, diagrams, and visualisations. Supported formats: PNG, JPG/JPEG, GIF, WebP.

```bash
# Analyse a scientific plot
sciagent "Interpret the results in ./output/graph.png"

# Review simulation output
sciagent "What does the velocity field in ./cfd/velocity.png show?"
```

The agent reads images via the `file_ops` tool and passes them to the LLM for visual analysis. This uses the `VISION_MODEL` tier.

## Scientific Services

SciAgent runs simulations in Docker containers. Available services:

| Domain | Services | Capabilities |
|--------|----------|--------------|
| **Math & Optimisation** | `scipy-base`, `sympy`, `cvxpy`, `optuna` | Numerical computing, symbolic math, convex optimisation, hyperparameter tuning |
| **Chemistry & Materials** | `rdkit`, `ase`, `lammps`, `dwsim` | Molecular analysis, atomistic simulations, MD, chemical process simulation |
| **Molecular Dynamics** | `gromacs`, `lammps` | Biomolecular simulations, soft matter, solid-state materials |
| **Photonics & Optics** | `rcwa`, `meep`, `pyoptools` | RCWA for gratings, FDTD electromagnetics, optical ray tracing |
| **CFD & FEM** | `openfoam`, `gmsh`, `elmer` | Fluid dynamics, mesh generation, multiphysics FEM |
| **Circuits & EDA** | `ngspice`, `openroad`, `iic-osic-tools` | SPICE simulation, RTL-to-GDS flow, 80+ IC design tools |
| **Quantum Computing** | `qiskit` | Quantum circuits, gates, algorithms (Grover, VQE, QAOA) |
| **Bioinformatics** | `biopython`, `blast` | Sequence analysis, BLAST searching, phylogenetics |
| **Network Analysis** | `networkx` | Graph algorithms, centrality, community detection |
| **Scientific ML** | `sciml-julia` | Julia ODE/SDE solving, symbolic modelling, neural DEs |

The agent automatically researches documentation, writes code, and runs it in the appropriate container. You can also ask the agent to build a service and add to the registry.

```bash
sciagent "Build a Docker service for the XYZ library and publish to GHCR"                           
```                                                                                                
This triggers the build-service skill which automates the entire workflow: researches the package, creates the Dockerfile, updates registry.yaml, and builds/pushes the image. 

The full documentation is in src/sciagent/skills/build-service/SKILL.md.   

## Python Usage

```python
from sciagent import create_agent, DEFAULT_MODEL

agent = create_agent(model=DEFAULT_MODEL, working_dir="./project")
result = agent.run("Analyze this codebase")
```

For detailed Python API, see [API Reference](developers/api-reference.md).
