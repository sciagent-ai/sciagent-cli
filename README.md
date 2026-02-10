# SciAgent

SciAgent is a modular agent framework for software engineering and scientific computing. It combines a standard agent loop with dependency-aware task orchestration, allowing the language model to plan and execute complex workflows by invoking external tools and containerised services.

## Features

- **Skill-based workflows** – Load specialised workflows from SKILL.md files for complex tasks like scientific computing, code review, and service building. Skills auto-trigger based on user input patterns.
- **Image & multimodal analysis** – Analyse scientific plots, microscopy images, diagrams, and data visualisations. Supports PNG, JPG, GIF, and WebP formats.
- **Service isolation** – Run all scientific computations inside isolated Docker containers for reproducibility, security, and portability.
- **Task DAG orchestration** – Define a graph of tasks with dependencies (`depends_on`), batch parallelisable steps and pass data between tasks via `result_key`.
- **Artifact & target validation** – Verify that expected files exist or that computed metrics meet user-defined criteria.
- **Scientific services** – Run simulations inside Docker containers for electromagnetics (RCWA, MEEP), fluid dynamics (OpenFOAM), molecular dynamics (GROMACS), cheminformatics (RDKit), symbolic math (SymPy), optimisation (CVXPY) and more.
- **Multi-model support** – Choose between Anthropic Claude, OpenAI (GPT-4.1, o3, o4-mini), Google Gemini 3, xAI Grok 4, DeepSeek, or open-source models via LiteLLM. Caching reduces cost and latency.
- **Sub-agents** – Spawn specialised agents for exploration, debugging, research, planning and general tasks. Each agent uses a cost-optimised model tier (scientific for planning, coding for implementation, fast for exploration).

## Quick start

### Installation

SciAgent requires Python 3.9 or newer. We recommend installing it inside a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .  # install from source; alternatively use pip install sciagent when available
```

### Set API keys

SciAgent communicates with large-language models and search engines via external APIs. At a minimum you need to export an API key for your chosen LLM provider and for the Brave search tool used by the `web` tool:

```bash
export ANTHROPIC_API_KEY="your-claude-key"      # or OPENAI_API_KEY, GOOGLE_API_KEY, etc.
export BRAVE_SEARCH_API_KEY="your-brave-key"   # required for web search
```

Additional environment variables (e.g. `OPENAI_API_KEY`, `GOOGLE_API_KEY`) can be set as needed depending on the model.

### Run a task

Invoke SciAgent via the `sciagent` CLI and pass a natural-language task description. A project directory is created to store generated code and artifacts:

```bash
sciagent --project-dir ~/my-project "Create a Python script that calculates Fibonacci numbers"
```

Use the `--interactive` flag to enter a REPL for iterative control:

```bash
sciagent --interactive
```

Select a different model or enable sub-agents when needed:

```bash
sciagent -m openai/gpt-4.1 "Analyse this codebase"
sciagent -m gemini/gemini-3-pro-preview "Explain this diagram"
sciagent --subagents "Research and refactor this module"
```

For more details on CLI flags see the [Configuration](docs/configuration.md) guide or run `sciagent --help`.

## Image analysis examples

SciAgent can analyse images including scientific plots, microscopy, diagrams, and data visualisations:

```bash
# Analyse a scientific plot
sciagent "Read and interpret the graph at ./results/figure1.png"

# Examine microscopy images
sciagent "Analyse the cell structure in ./data/microscopy.jpg"

# Interpret simulation output
sciagent "What does the CFD velocity field in ./output/velocity.png show?"

# Review data visualisation
sciagent "Explain the trends in ./plots/timeseries.png and suggest improvements"
```

Supported formats: PNG, JPG/JPEG, GIF, WebP.

## Scientific computing examples

SciAgent can run simulations directly in specialised Docker containers. Some examples:

```bash
# RCWA electromagnetic simulation
sciagent "Design a photonic crystal with bandgap at 1550 nm using rcwa"

# Molecular dynamics (GROMACS)
sciagent "Run a GROMACS simulation for a protein in water"

# Convex optimisation (CVXPY)
sciagent "Solve a portfolio optimisation problem using cvxpy"

# Symbolic math (SymPy)
sciagent "Derive equations of motion for a double pendulum using sympy"
```

See [Available Services](#available-services) below for the full list of containerised environments.

## Available services

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

Services are automatically selected and managed when you request scientific computations. Refer to the [Architecture](docs/developers/architecture.md#service-registry) page for details.

## CLI reference

```text
sciagent [OPTIONS] [TASK]

Options:
  -i, --interactive     Interactive REPL mode
  -m, --model MODEL     LLM model (default: anthropic/claude-opus-4-5-20251101)
  -p, --project-dir     Working directory
  -t, --load-tools      Load custom tools from Python file
  -s, --subagents       Enable sub-agent spawning
  --system-prompt PATH  Custom system prompt file
  --temperature FLOAT   Model temperature (0.0-1.0)
  --resume SESSION_ID   Resume previous session
  --list-sessions       List available sessions
  --max-iterations N    Max agent iterations (default: 120)
  -v, --verbose         Verbose output (default)
  -q, --quiet           Minimal output
```

## Python API

SciAgent can also be embedded in your own Python code. Use the `create_agent()` factory to configure an agent and call `run()` or `run_interactive()`:

```python
from sciagent import create_agent, run_task, DEFAULT_MODEL

# One-shot task
result = run_task("Create a hello world script")

# Custom configuration (uses DEFAULT_MODEL if not specified)
agent = create_agent(
    model=DEFAULT_MODEL,  # Or specify another model like "openai/gpt-4o"
    working_dir="./my-project"
)
result = agent.run("Analyse this codebase")

# Interactive session
agent.run_interactive()
```

To change the default model globally, edit `src/sciagent/defaults.py`.

## Skills

SciAgent uses a skill-based workflow system for complex, multi-phase tasks. Skills are defined in SKILL.md files and auto-trigger based on user input:

| Skill | Purpose |
|-------|---------|
| `sci-compute` | Scientific simulations with research-first approach |
| `build-service` | Build and publish Docker services to GHCR |
| `code-review` | Comprehensive code review with security analysis |

The `sci-compute` skill implements a five-phase workflow: Discovery → Research → Code Generation → Execution → Debug. This ensures correct API usage by researching official documentation before writing simulation code.

## Sub-agents

SciAgent uses a tiered model system for cost-effective sub-agent delegation:

| Agent | Model Tier | Purpose |
|-------|------------|---------|
| `explore` | Fast | Quick codebase searches and file lookups |
| `debug` | Coding | Error investigation with web research |
| `research` | Coding | Web research, documentation, literature review |
| `plan` | Scientific | Break down complex problems (needs deep reasoning) |
| `general` | Coding | Complex multi-step implementation tasks |

Model tiers are defined in `src/sciagent/defaults.py`:
- **Scientific**: Best quality for scientific code and planning
- **Vision**: Image and multimodal analysis (uses Scientific tier)
- **Coding**: Good for implementation, debugging, research
- **Fast**: Quick/cheap for exploration and extraction

## Architecture

SciAgent consists of a **Task Orchestrator** that schedules tasks in a directed acyclic graph and a set of **Agents** that execute those tasks. Each agent follows a Think → Act → Observe loop and can call tools such as `bash`, `file_ops`, `search`, `web`, `todo`, `skill` and `ask_user` to interact with the file system, shell, web, containerised simulations and request user input when needed.

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
│  Tools: bash, file_ops, search, web, todo,      │
│         skill, ask_user                         │
│  Services: rcwa, meep, openfoam, gromacs, ...   │
└─────────────────────────────────────────────────┘
```

## Documentation

Comprehensive documentation is available in the `docs` folder. Start with the following pages:

- **[Getting Started](docs/getting-started.md)** – installation, running your first task and CLI basics.
- **[Configuration](docs/configuration.md)** – customise the model, system prompt, caching, tool registry and sub-agents.
- **[Use Cases](docs/use-cases.md)** – real-world examples of how to apply SciAgent to coding, research and simulation.
- **[Architecture](docs/developers/architecture.md)** – detailed explanation of the agent loop, context management, tools, skills, and sub-agent system.
- **[Comparison](docs/comparison.md)** – what sets SciAgent apart from other agent frameworks.

## Requirements

- Python 3.9+
- Docker (for containerised services)
- API key for your chosen LLM provider (e.g. Anthropic, OpenAI, Google) and `BRAVE_SEARCH_API_KEY` for web search

## License

This project is released under the MIT License.

---

© 2026 SciAgent Team – building an open platform for AI-powered scientific computing and engineering.
