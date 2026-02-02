# SciAgent Documentation

SciAgent is an agent framework for software engineering and scientific computing. It combines a standard agent loop with a **task orchestration layer** that manages dependencies between tasks, passes data between them, and runs independent tasks in parallel.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Getting Started](#getting-started)
3. [Task DAG & Orchestration](#task-dag--orchestration)
4. [Tools Reference](#tools-reference)
5. [Containerized Services](#containerized-services)
6. [When to Use SciAgent](#when-to-use-sciagent)
7. [Configuration](#configuration)
8. [Use Cases & Applications](#use-cases--applications)

---

## Architecture Overview

SciAgent has two layers:

1. **Task Orchestration** - Manages a DAG of tasks with dependencies and data flow
2. **Agent Execution** - Standard think-act-observe loop for executing individual tasks

```
                    ┌─────────────────────────────────┐
                    │      Task DAG (TodoGraph)       │
                    │  ┌─────┐   ┌─────┐   ┌─────┐   │
                    │  │ T1  │──▶│ T3  │──▶│ T5  │   │
                    │  └─────┘   └──┬──┘   └─────┘   │
                    │  ┌─────┐     │                 │
                    │  │ T2  │─────┘                 │
                    │  └─────┘                       │
                    └─────────────────────────────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │    Task Orchestrator      │
                    │  • Topological sort       │
                    │  • Parallel batching      │
                    │  • Result passing         │
                    │  • Artifact validation    │
                    └─────────────┬─────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        ┌──────────┐        ┌──────────┐        ┌──────────┐
        │ SubAgent │        │ SubAgent │        │ SubAgent │
        │(research)│        │  (code)  │        │(validate)│
        └──────────┘        └──────────┘        └──────────┘
```

### Core Capabilities

- **Task Dependencies**: Tasks specify dependencies via `depends_on: ["task_id"]`
- **Data Flow**: Results pass between tasks using `result_key`
- **Parallel Execution**: Independent tasks run concurrently in batches
- **Artifact Validation**: Tasks can declare files they produce; system verifies they exist
- **Target Validation**: Tasks can specify success criteria (e.g., accuracy >= 0.95)
- **Scientific Services**: 14 pre-configured Docker containers for simulation tools

---

## Getting Started

### Installation

```bash
cd sciagent-cli
python3.9 -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY="your-key"
```

### Quick Start

```bash
# Simple task (uses basic agent loop)
sciagent "Create a fibonacci function in Python"

# Complex task (automatically creates task DAG)
sciagent "Research best practices for REST APIs, design an API for a todo app, implement it, and write tests"

# Interactive mode
sciagent --interactive

# With sub-agents for parallel research
sciagent --subagents "Analyze this codebase and refactor the authentication module"
```

---

## Architecture Deep Dive

### The Two-Layer Architecture

**Layer 1: Task Orchestration (TaskOrchestrator + TodoGraph)**

The orchestrator manages a DAG of tasks with:
- **Dependencies**: Tasks declare what they depend on via `depends_on: ["task_id"]`
- **Data Flow**: Results flow between tasks via `result_key`
- **Parallel Batching**: Independent tasks execute concurrently
- **Validation**: Artifacts and targets are verified before marking complete

**Layer 2: Agent Execution (AgentLoop)**

Each task is executed by an agent (or sub-agent) using the standard think-act-observe loop. This layer is intentionally simple - the intelligence is in the orchestration.

### Task Lifecycle

```
1. PENDING     → Task created, waiting for dependencies
2. BLOCKED     → Dependencies not yet satisfied
3. READY       → All dependencies completed, can execute
4. IN_PROGRESS → Currently executing
5. COMPLETED   → Finished, result available for dependents
6. FAILED      → Error or validation failure
```

### Data Flow Example

```python
# Task 1: Research
{
    "id": "research_api",
    "content": "Research REST API best practices",
    "task_type": "research",
    "result_key": "api_patterns"  # Output will be available as "api_patterns"
}

# Task 2: Design (depends on research)
{
    "id": "design_api",
    "content": "Design the API based on research",
    "depends_on": ["research_api"],  # Waits for research
    # Receives: {"api_patterns": <result from research>}
}

# Task 3: Implement (depends on design)
{
    "id": "implement_api",
    "content": "Implement the API",
    "depends_on": ["design_api"],
    "produces": "file:src/api.py",  # Declares artifact
    "target": {"metric": "endpoints", "operator": ">=", "value": 5}
}
```

---

## Task DAG & Orchestration

### Creating Task DAGs

**Method 1: Automatic (Agent creates todos)**

The agent analyzes complex tasks and creates a todo DAG:

```bash
sciagent "Build a REST API with authentication, database, and tests"
```

Agent creates:
```
Phase 1 (parallel):
  ☐ [research_auth] Research auth patterns
  ☐ [research_db] Research database patterns

Phase 2:
  ☐ [design] Design system architecture
    ↳ depends on: research_auth, research_db

Phase 3 (parallel):
  ☐ [impl_auth] Implement authentication
  ☐ [impl_db] Implement database layer
    ↳ depends on: design

Phase 4:
  ☐ [impl_api] Implement API endpoints
    ↳ depends on: impl_auth, impl_db

Phase 5:
  ☐ [test] Write and run tests
    ↳ depends on: impl_api
```

**Method 2: WorkflowBuilder (Programmatic)**

```python
from sciagent.orchestrator import WorkflowBuilder, create_orchestrator

workflow = WorkflowBuilder()

# Phase 1: Research (parallel)
workflow.add("research_api", "Research REST API patterns", task_type="research")
workflow.add("research_auth", "Research authentication", task_type="research")

# Phase 2: Design (depends on all research)
workflow.add("design", "Design architecture",
             depends_on=["research_api", "research_auth"],
             result_key="architecture")

# Phase 3: Implementation
workflow.add("implement", "Implement the API",
             depends_on=["design"],
             produces="file:src/api.py",
             target={"metric": "test_coverage", "operator": ">=", "value": 80})

# Execute
orchestrator, todo = create_orchestrator()
todo = workflow.build()
results = orchestrator.execute_all()
```

### Parallel Execution

The orchestrator batches independent tasks for parallel execution:

```
Batch 1 (parallel): [research_api, research_auth, research_db]
    ↓ all complete
Batch 2 (sequential): [design]
    ↓ complete
Batch 3 (parallel): [impl_auth, impl_db]
    ↓ all complete
Batch 4 (sequential): [impl_api]
    ↓ complete
Batch 5 (sequential): [test]
```

### Artifact Validation

Tasks can declare artifacts they produce:

```python
{
    "id": "generate_data",
    "content": "Generate training dataset",
    "produces": "file:data/training.csv"  # Must exist when task completes
}
```

If the file doesn't exist, the task fails validation.

### Target Validation

Tasks can specify success criteria:

```python
{
    "id": "optimize",
    "content": "Optimize the model",
    "target": {
        "metric": "accuracy",
        "operator": ">=",
        "value": 0.95
    }
}
```

The result must contain `{"accuracy": X}` where X >= 0.95.

---

## Tools Reference

### Core Tools (6)

| Tool | Purpose | Key Features |
|------|---------|--------------|
| `bash` | Shell execution | Smart timeouts, output truncation |
| `file_ops` | File operations | Read/write/edit with validation |
| `search` | Find files/content | Glob patterns, regex grep |
| `web` | Search & fetch | DuckDuckGo, HTML-to-text |
| `todo` | Task DAG management | Dependencies, data flow, validation |
| `service` | Docker containers | 14 scientific computing services |

### todo Tool - The DAG Manager

```python
# Create a task with dependencies and data flow
todo(todos=[
    {
        "id": "task_1",
        "content": "Research algorithms",
        "status": "pending",
        "task_type": "research",
        "result_key": "algorithms",  # Output named "algorithms"
        "priority": "high",
        "can_parallel": True
    },
    {
        "id": "task_2",
        "content": "Implement best algorithm",
        "status": "pending",
        "task_type": "code",
        "depends_on": ["task_1"],  # Waits for task_1
        "produces": "file:src/algorithm.py",  # Validates file exists
        "target": {"metric": "complexity", "operator": "<=", "value": "O(n log n)"}
    }
])

# Query the DAG
todo(query="ready_tasks")       # What can execute now?
todo(query="blocked_tasks")     # What's waiting on dependencies?
todo(query="execution_order")   # Full topological order with batches
todo(query="results")           # Results from completed tasks
```

### service Tool - Scientific Computing

Run code in specialized Docker containers:

```python
# List available services
service(action="list")

# Run RCWA simulation
service(action="run", service="rcwa", code="""
import S4
S = S4.New(Lattice=1, NumBasis=20)
S.SetMaterial('Vacuum', 1)
S.SetMaterial('Si', 12+0.1j)
# ... simulation code
print(S.GetPowerFlux('bottom'))
""")

# Run OpenFOAM CFD
service(action="run", service="openfoam",
        command="blockMesh && icoFoam")
```

---

## Containerized Services

### Available Services (14)

| Category | Service | Description |
|----------|---------|-------------|
| **Electromagnetics** | `rcwa` | S4/RCWA for photonic crystals, gratings |
| | `meep` | FDTD for waveguides, resonators |
| **Chemistry** | `rdkit` | Molecular manipulation, fingerprints |
| | `ase` | Atomic simulations, DFT interfaces |
| | `gromacs` | Molecular dynamics |
| **Mechanics** | `openfoam` | CFD, turbulence modeling |
| | `elmer` | Multiphysics FEM |
| | `gmsh` | Mesh generation |
| **Electronics** | `ngspice` | SPICE circuit simulation |
| | `openroad` | RTL-to-GDS digital design |
| **Math** | `sympy` | Symbolic mathematics |
| | `cvxpy` | Convex optimization |
| | `scipy-base` | NumPy, SciPy, Matplotlib |
| **Julia** | `sciml-julia` | DifferentialEquations.jl |

### Image Resolution

```
1. Check local Docker images
2. Pull from ghcr.io/sciagent-ai/{service}:latest
3. Build from services/{service}/Dockerfile
```

---

## When to Use SciAgent

SciAgent is designed for **multi-phase workflows** where:

- Tasks have dependencies on each other
- Results from one task feed into another
- Independent tasks can run in parallel
- You need to validate outputs (files exist, metrics met)
- You're working with scientific simulations

### Comparison with Other Approaches

| Aspect | SciAgent | LangChain | AutoGPT | Coding Agents |
|--------|----------|-----------|---------|---------------|
| Task structure | DAG with dependencies | Chains/sequences | Goal decomposition | Implicit |
| Data passing | Explicit `result_key` | Tool outputs | Long-term memory | Context |
| Parallelism | Batch execution | Sequential | Sequential | Sequential |
| Validation | Artifact + target checks | Custom | Retry on failure | Test execution |
| Scientific tools | 14 Docker services | External setup | External setup | General purpose |

**Consider SciAgent for:**
- Scientific simulation pipelines
- Multi-phase software projects
- Workflows requiring validation gates
- Tasks with clear dependency structure

**Consider other tools for:**
- Simple sequential tasks (LangChain)
- Open-ended exploration (AutoGPT)
- Pure code generation (Aider, Claude Code)
- RAG applications (LangChain)

---

## Configuration

### CLI Options

```bash
sciagent [OPTIONS] [TASK]

# Model selection
-m, --model MODEL          # Default: anthropic/claude-sonnet-4-20250514

# Execution
-p, --project-dir PATH     # Working directory
-s, --subagents            # Enable parallel sub-agents
--max-iterations N         # Default: 30

# Sessions
--resume SESSION_ID        # Resume previous session
--list-sessions            # List saved sessions

# Custom
-t, --load-tools PATH      # Load custom tools
--system-prompt PATH       # Custom system prompt
```

### Environment Variables

```bash
ANTHROPIC_API_KEY     # Claude models
OPENAI_API_KEY        # GPT models
GEMINI_API_KEY        # Gemini models
```

### Supported Models

```bash
# Anthropic
sciagent -m anthropic/claude-sonnet-4-20250514 "task"
sciagent -m anthropic/claude-opus-4-20250514 "task"

# OpenAI
sciagent -m openai/gpt-4o "task"

# Google
sciagent -m gemini/gemini-pro "task"

# Local (Ollama)
sciagent -m ollama/llama3 "task"
```

---

## Use Cases & Applications

### 1. Scientific Simulation Pipeline

```bash
sciagent "Design a 1D photonic crystal with bandgap at 1550nm.
Use RCWA to simulate, optimize layer thicknesses,
and generate a report with transmission spectrum plots."
```

Creates DAG:
```
research_materials → design_structure → simulate_initial
                                            ↓
                     optimize_layers ← analyze_results
                           ↓
                    final_simulation → generate_report
```

### 2. Multi-Phase Software Project

```bash
sciagent --subagents "Build a REST API for a task management app.
Include JWT authentication, PostgreSQL storage, and comprehensive tests.
Target 80% test coverage."
```

Creates parallel research → design → parallel implementation → integration → testing

### 3. Optimization with Validation

```python
workflow = WorkflowBuilder()
workflow.add("baseline", "Run baseline simulation",
             produces="file:results/baseline.json",
             result_key="baseline_metrics")
workflow.add("optimize", "Optimize parameters",
             depends_on=["baseline"],
             target={"metric": "efficiency", "operator": ">=", "value": 0.90})
workflow.add("validate", "Validate optimized design",
             depends_on=["optimize"],
             produces="file:results/final.json")
```

### 4. Literature Review + Implementation

```bash
sciagent "Research state-of-the-art methods for image segmentation,
implement the best approach using PyTorch, and benchmark on COCO dataset."
```

Automatically parallelizes research, sequences implementation after synthesis.

---

## Extending SciAgent

### Custom Tools

```python
from sciagent.tools import BaseTool, ToolResult

class SimulationTool(BaseTool):
    name = "simulate"
    description = "Run custom simulation"
    parameters = {
        "type": "object",
        "properties": {
            "config": {"type": "string"}
        },
        "required": ["config"]
    }

    def execute(self, config: str) -> ToolResult:
        # Run simulation
        result = run_my_simulation(config)
        return ToolResult(success=True, output=result)

# Load via CLI
sciagent --load-tools ./my_tools.py "Run simulation with config X"
```

### Custom Orchestration

```python
from sciagent.orchestrator import TaskOrchestrator, OrchestratorConfig

config = OrchestratorConfig(
    max_parallel_tasks=8,
    retry_failed_tasks=True,
    max_retries=3,
    timeout_per_task=600.0
)

orchestrator = TaskOrchestrator(
    todo_tool=todo,
    subagent_orchestrator=subagent,
    config=config,
    task_executor=my_custom_executor  # Optional custom execution
)
```

---

## Summary

SciAgent provides:

1. **Task DAG with Data Flow** - Dependencies via `depends_on`, results via `result_key`
2. **Parallel Batch Execution** - Independent tasks run concurrently
3. **Artifact Validation** - Verify files exist when tasks complete
4. **Target Validation** - Check metrics meet success criteria
5. **Scientific Services** - 14 containerized simulation environments

Designed for structured multi-phase workflows, particularly in scientific computing and engineering.
