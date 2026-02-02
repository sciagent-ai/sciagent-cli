# SciAgent

An agent framework for software engineering and scientific computing. Combines a standard agent loop with **task orchestration** - managing dependencies between tasks, passing data between them, and running independent tasks in parallel.

## Features

- **Task DAG Orchestration** - Dependencies, parallel batching, data flow via `result_key`
- **Artifact & Target Validation** - Verify outputs exist, check metrics meet criteria
- **14 Scientific Services** - Docker containers for RCWA, MEEP, OpenFOAM, GROMACS, RDKit, SymPy, etc.
- **Multi-Model Support** - Claude, GPT-4, Gemini, local models via LiteLLM
- **Sub-Agents** - Specialized agents for research, code review, testing

## Quick Start

```bash
# Install
pip install -e .

# Set API key
export ANTHROPIC_API_KEY="your-key"

# Run a task
sciagent "Create a Python script that calculates fibonacci numbers"

# Interactive mode
sciagent --interactive

# Use a specific model
sciagent -m openai/gpt-4o "Analyze this codebase"

# Enable sub-agents for complex tasks
sciagent --subagents "Research and refactor this module"
```

## Scientific Computing

Run simulations in specialized Docker containers:

```bash
# RCWA electromagnetic simulation
sciagent "Design a photonic crystal with bandgap at 1550nm using rcwa"

# Molecular dynamics
sciagent "Run a GROMACS simulation for a protein in water"

# Convex optimization
sciagent "Solve a portfolio optimization problem using cvxpy"

# Symbolic math
sciagent "Derive equations of motion for a double pendulum using sympy"
```

### Available Services

| Domain | Services |
|--------|----------|
| **Electromagnetics** | `rcwa` (S4/RCWA), `meep` (FDTD) |
| **Chemistry & Materials** | `rdkit`, `ase`, `gromacs` |
| **Fluid Dynamics & FEM** | `openfoam`, `elmer`, `gmsh` |
| **Electronics & EDA** | `ngspice`, `openroad` |
| **Math & Optimization** | `sympy`, `cvxpy`, `scipy-base` |
| **Scientific ML** | `sciml-julia` |

## CLI Reference

```
sciagent [OPTIONS] [TASK]

Options:
  -i, --interactive     Interactive REPL mode
  -m, --model MODEL     LLM model (default: claude-sonnet-4)
  -p, --project-dir     Working directory
  -t, --load-tools      Load custom tools from Python file
  -s, --subagents       Enable sub-agent spawning
  --resume SESSION_ID   Resume previous session
  --list-sessions       List available sessions
  --max-iterations N    Max agent iterations (default: 30)
  -v, --verbose         Verbose output (default)
  -q, --quiet           Minimal output
```

## Python API

```python
from sciagent import create_agent, run_task

# One-shot task
result = run_task("Create a hello world script")

# Custom configuration
agent = create_agent(
    model="anthropic/claude-sonnet-4-20250514",
    working_dir="./my-project"
)
result = agent.run("Analyze this codebase")

# Interactive session
agent.run_interactive()
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                Task Orchestrator                │
│  ┌─────┐   ┌─────┐   ┌─────┐                   │
│  │ T1  │──▶│ T3  │──▶│ T4  │  (Task DAG)       │
│  └─────┘   └──┬──┘   └─────┘                   │
│  ┌─────┐     │       • depends_on              │
│  │ T2  │─────┘       • result_key              │
│  └─────┘             • parallel batching       │
└─────────────────────┬───────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   ┌─────────┐   ┌─────────┐   ┌─────────┐
   │ Agent   │   │ Agent   │   │ Agent   │
   │ (T1)    │   │ (T2)    │   │ (T3)    │
   └────┬────┘   └────┬────┘   └────┬────┘
        │             │             │
        └─────────────┼─────────────┘
                      ▼
┌─────────────────────────────────────────────────┐
│  Tools: bash, file_ops, search, web, todo       │
│  Services: rcwa, meep, openfoam, gromacs, ...   │
└─────────────────────────────────────────────────┘
```

## Documentation

See **[docs/DOCUMENTATION.md](docs/DOCUMENTATION.md)** for:

- Task DAG orchestration details
- Complete tool reference
- All 14 containerized services
- Custom tool development
- Use cases and examples

## Requirements

- Python 3.9+
- Docker (for containerized services)
- API key for your chosen LLM provider

## License

MIT License
