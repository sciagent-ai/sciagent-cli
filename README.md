# SciAgent

SciAgent is a modular agent framework for software engineering and scientific computing. It combines a standard agent loop with dependency-aware task orchestration, allowing the language model to plan and execute complex workflows by invoking external tools, containerised services, and cloud compute.

> **v2.0 is current.** Highlights: cloud compute via SkyPilot, durable provenance log, task orchestration with background subagents and checkpoint/resume. See [What's New in v2.0](docs/whats-new-v2.md). v1.0 is preserved on branch `release/v1.0` and tag `v1.0`.

## Features

- **Cloud compute** – Run scientific simulations on cloud clusters via SkyPilot, with a local Docker fallback for small jobs. Per-session workspace bucket persists outputs across cluster lifecycle. See [Cloud Compute](docs/cloud-compute.md).

- **Durable provenance log** – Every tool call, compute job, artifact, and verification result lands in an append-only JSONL log per session — cross-LLM verifiable. See [Provenance Log Schema](docs/provenance_log_schema.md).

- **Task orchestration** – Unified registry for in-flight work (`task_index`) covering cloud jobs and background subagents. Background subagents support checkpoint and 3-way resume. See [Task Orchestration](docs/task-orchestration.md).

- **Skill-based workflows** – Load specialised workflows from SKILL.md files for complex tasks like service building and code review. Skills auto-trigger based on user input patterns.

- **Image & multimodal analysis** – Analyse scientific plots, microscopy images, diagrams, and data visualisations. Supports PNG, JPG, GIF, and WebP formats.

- **Service isolation** – Run all scientific computations inside isolated Docker containers for reproducibility, security, and portability.

- **Task DAG orchestration** – Define a graph of tasks with dependencies (`depends_on`), batch parallelisable steps and pass data between tasks via `result_key`.

- **Artifact & target validation** – Verify that expected files exist or that computed metrics meet user-defined criteria; `produces_uris` validation on subagent outputs.

- **Scientific services** – Run simulations inside Docker containers for electromagnetics (RCWA, MEEP), fluid dynamics (OpenFOAM + swak4foam), molecular dynamics (GROMACS), cheminformatics (RDKit), symbolic math (SymPy), optimisation (CVXPY), post-processing (ParaView), digital IC (OpenROAD, iic-osic-tools) and more.

- **Multi-model support** – Choose between Anthropic Claude, OpenAI (GPT-4.1, o3, o4-mini), Google Gemini 3, xAI Grok 4, DeepSeek, or open-source models via LiteLLM. Caching reduces cost and latency.

- **Sub-agents** – Spawn specialised agents for exploration, debugging, research, planning, cloud compute, post-job analysis, general implementation, and verification. Each agent uses a cost-optimised model tier (scientific for planning, coding for implementation, fast for exploration).

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

SciAgent can analyze images including scientific plots, microscopy, diagrams, and data visualisations:

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


## Skills

SciAgent uses a skill-based workflow system for complex, multi-phase tasks. Skills are defined in SKILL.md files and auto-trigger based on user input:

| Skill | Purpose |
|-------|---------|
| `use-service` | Look up a registered scientific service and run a simulation |
| `build-service` | Build and publish Docker services to GHCR |
| `code-review` | Comprehensive code review with security analysis |

The `use-service` skill implements a research-first workflow: discover the right service, read its docs, write the simulation code, run it in the container, debug. This ensures correct API usage by researching official documentation before writing simulation code.

## Sub-agents

SciAgent uses a tiered model system for cost-effective sub-agent delegation:

| Agent | Model Tier | Purpose |
|-------|------------|---------|
| `explore` | Fast | Quick codebase searches and file lookups |
| `debug` | Coding | Error investigation with web research |
| `research` | Coding | Web research, documentation, literature review |
| `plan` | Scientific | Break down complex problems (needs deep reasoning) |
| `compute` | Coding | Cloud-job orchestration with token-isolated context |
| `analyze` | Coding | Post-job derivation (plots, statistics, light fits, DSE) |
| `general` | Coding | Complex multi-step implementation tasks |
| `verifier` | Verification | Independent validation against the provenance log |

Model tiers are defined in `src/sciagent/defaults.py`:
- **Scientific**: Main agent, planning 
- **Vision**: Image and multimodal analysis 
- **Coding**: Implementation, debugging, research 
- **Verification**: Independent verifier subagent
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

The v2.0 cloud + audit layer sits underneath:

```
┌──────────────────────────────────────────────────────┐
│ Compute subagent                                     │
│  compute_run / compute_exec / compute_cluster        │
│  materialize / materialize_workspace                 │
└──────────────┬───────────────────────────────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
  ┌──────────┐    ┌──────────────┐
  │  Local   │    │  SkyPilot    │     ┌──────────────────────┐
  │  Docker  │    │  managed     │◀────│ Workspace bucket     │
  │          │    │  / cluster   │     │ <cloud>://...-<sid>/ │
  └──────────┘    └──────┬───────┘     └──────────────────────┘
                         │
                         ▼
              ┌─────────────────────────┐
              │ Task index              │
              │ ~/.sciagent/tasks/*.json│
              │ kind=compute_job|sub…   │
              └─────────────────────────┘
                         │
                         ▼
              ┌─────────────────────────┐
              │ Provenance log (JSONL)  │
              │ tool_call/result        │
              │ compute_job_*           │
              │ artifact_produced       │
              │ verification_result     │
              └─────────────────────────┘
```

## Documentation

Comprehensive documentation is available in the `docs` folder. Start with the following pages:

- **[What's New in v2.0](docs/whats-new-v2.md)** – migration notes from v1.0, headline features, link to v1.0 archive.
- **[Getting Started](docs/getting-started.md)** – installation, running your first task and CLI basics.
- **[Configuration](docs/configuration.md)** – customise the model, system prompt, caching, tool registry, sub-agents, and cloud setup.
- **[Cloud Compute](docs/cloud-compute.md)** – SkyPilot integration, cluster lifecycle, workspace bucket, materialize.
- **[Task Orchestration](docs/task-orchestration.md)** – task index, background subagents, checkpoint and resume.
- **[Use Cases](docs/use-cases.md)** – real-world examples of how to apply SciAgent to coding, research and simulation.
- **[Architecture](docs/developers/architecture.md)** – detailed explanation of the agent loop, context management, tools, skills, sub-agents, SkyPilot integration, and the provenance log.
- **[Comparison](docs/comparison.md)** – what sets SciAgent apart from other agent frameworks.

## Requirements

- Python 3.9+
- Docker (for containerised services)
- API key for your chosen LLM provider (e.g. Anthropic, OpenAI, Google) and `BRAVE_SEARCH_API_KEY` for web search

## License

This project is released under the Apache 2.0 License.

---

© 2026 SciAgent Team – building an open platform for AI-powered scientific computing and engineering.
