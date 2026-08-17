---
layout: default
title: Configuration
nav_order: 3
---

# Configuration

Configure SciAgent via command-line flags or Python.

## Models

### Default Model

SciAgent uses Claude Sonnet as the default. Change it with `--model`:

```bash
sciagent --model openai/gpt-4.1 "Summarize README.md"
sciagent --model gemini/gemini-3-pro-preview "Analyze this diagram"
sciagent --model deepseek/deepseek-reasoner "Solve this physics problem"
```

Supported providers (via [litellm](https://github.com/BerriAI/litellm)): OpenAI, Anthropic, Google, and custom endpoints.

### Model Tiers

SciAgent uses five model tiers for cost-effective operation. Configure in `src/sciagent/defaults.py`:

| Tier | Variable | Purpose |
|------|----------|---------|
| Scientific | `SCIENTIFIC_MODEL` | Main agent, planning |
| Vision | `VISION_MODEL` | Image and multimodal analysis |
| Coding | `CODING_MODEL` | Debug, research, general sub-agents |
| Verification | `VERIFICATION_MODEL` | Independent verifier subagent |
| Fast | `FAST_MODEL` | Explore sub-agent (speed/cost) |

The main agent uses `DEFAULT_MODEL` (set to `SCIENTIFIC_MODEL`). The verification tier powers the independent verifier subagent that validates task outputs. Sub-agents use tier-appropriate models automatically.

### Alternative Models by Provider

SciAgent supports multiple LLM providers via [LiteLLM](https://github.com/BerriAI/litellm). Use `--model provider/model-name` to switch.

> **Note**: Only Anthropic models are tested. Alternatives below are based on comparable capabilities but have NOT been validated. Your mileage may vary.

| Tier | Anthropic (tested) | OpenAI | Google | xAI |
|------|-------------------|--------|--------|-----|
| **Scientific** | `claude-sonnet-4-6` (default), `claude-opus-4-7` | `gpt-5.4`, `gpt-4.1`, `o3`, `o3-pro` | `gemini-3-pro-preview`, `gemini-2.5-pro` | `grok-4-1-fast-reasoning` |
| **Vision** | `claude-opus-4-7` (default) | `gpt-5.4`, `gpt-4.1`, `o3` | `gemini-3.1-pro-preview`, `gemini-3-pro-image-preview` | `grok-4.3`, `grok-2-vision-1212` |
| **Coding** | `claude-sonnet-4-6` (default) | `gpt-5.4-mini`, `gpt-4.1-mini`, `o4-mini` | `gemini-3-flash-preview`, `gemini-2.5-flash` | `grok-code-fast-1` |
| **Verification** | `claude-sonnet-4-6` (default) | `gpt-5.4`, `gpt-4.1-mini`, `o4-mini` | `gemini-3-flash-preview`, `gemini-2.5-flash` | `grok-code-fast-1` |
| **Fast** | `claude-haiku-4-5-20251001` (default) | `gpt-5.4-mini`, `gpt-4.1-nano`, `o4-mini` | `gemini-2.5-flash-lite` | `grok-3-mini` |

**Open-Source alternatives** (via Together AI, Groq, or self-hosted):

| Tier | Models |
|------|--------|
| Scientific | `deepseek/deepseek-reasoner`, `together_ai/Qwen/Qwen3-235B-A22B-Instruct` |
| Vision | `together_ai/Qwen/Qwen2.5-VL-72B-Instruct`, `together_ai/meta-llama/Llama-3.2-90B-Vision-Instruct` |
| Coding | `deepseek/deepseek-chat`, `together_ai/meta-llama/Llama-3.3-70B-Instruct` |
| Verification | `deepseek/deepseek-chat`, `together_ai/meta-llama/Llama-3.3-70B-Instruct` |
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
| `compute` | Coding | Cloud-job orchestration with token-isolated context |
| `analyze` | Coding | Post-job derivation (plots, statistics, light fits) |
| `general` | Coding | Complex multi-step tasks |
| `verifier` | Verification | Independent validation against the provenance log |

Model tiers are defined in `src/sciagent/defaults.py`. See [Sub-agents](developers/architecture.md#sub-agents) for customization.

## Configuration layers

SciAgent has two configuration surfaces, deliberately kept separate:

- **`AgentConfig`** — agent-loop concerns: model, tokens, iterations, compaction. Lives in `sciagent.AgentConfig`.
- **`CloudConfig`** — cloud / compute concerns: cost gate, workspace storage backend, cluster lifecycle defaults. Lives in `sciagent.compute.CloudConfig`.

Both are passed independently to `AgentLoop`:

```python
from sciagent import AgentConfig, AgentLoop
from sciagent.compute import CloudConfig

agent = AgentLoop(
    config=AgentConfig(model="anthropic/claude-sonnet-4-6", max_session_tokens=2_000_000),
    cloud_config=CloudConfig(commit_threshold_usd=10.0),
)
```

### AgentConfig fields

| Field | Default | Purpose |
|-------|---------|---------|
| `model` | from `defaults.py` | LLM identifier (LiteLLM format) |
| `temperature` | `0.0` | Sampling temperature |
| `max_tokens` | `16384` | Per-call output token cap |
| `max_iterations` | `120` | Agent-loop iteration cap |
| `max_session_tokens` | `4_000_000` | Cumulative soft budget; profile / `SCIAGENT_SESSION_SOFT_BUDGET` env override |
| `compact_at_fraction` | `None` | Fraction of context window above which compaction triggers; `None` defers to profile (typically `0.6`); `SCIAGENT_COMPACT_AT_PCT` env wins |
| `working_dir` | `"."` | Project directory |
| `verbose`, `auto_save`, `state_dir`, `reasoning_effort` | various | See `AgentConfig` source |

### CloudConfig fields

For cloud-scale simulations, install with the `cloud*` extras:

```bash
pip install 'sciagent-cli[cloud]'        # AWS
pip install 'sciagent-cli[cloud-gcp]'    # GCP
pip install 'sciagent-cli[cloud-azure]'  # Azure
pip install 'sciagent-cli[cloud-all]'    # All three
```

SciAgent inherits whatever credentials SkyPilot can find. Set up your provider once with the SkyPilot-supported flow (`aws configure`, `gcloud auth application-default login`, `az login`) and `sky check` will confirm.

| Field | Default | Purpose |
|-------|---------|---------|
| `commit_threshold_usd` | `None` (→ $5) | Estimated total ($) above which `compute_run` prompts before launching |
| `workspace_store` | `None` (→ auto-detect) | Cloud provider for the per-session workspace bucket: `s3` / `gcs` / `az` / `r2` / `oci` |
| `default_autostop_minutes` | `None` (→ provider default) | Default `idle_minutes` for cluster autostop |
| `default_timeout_sec` | `None` (→ 3600) | Per-job wall-clock budget. The reaper terminates clusters whose runtime exceeds this. Per-call `compute_run(timeout_sec=...)` wins. Pass 0 to disable. |
| `subagent_warm_resume_seconds` | `None` | Window during which a crashed subagent can be warm-resumed without prompting the parent |

Each field also has an env var and (for some) a `~/.sciagent/config.yaml` key. Precedence per knob: **env > CloudConfig field > yaml > built-in default**.

| Field | Env var | YAML key |
|-------|---------|----------|
| `commit_threshold_usd` | `SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD` | `compute.commit_threshold_usd` |
| `workspace_store` | `SCIAGENT_WORKSPACE_STORE` | — |
| `subagent_warm_resume_seconds` | `SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS` | `subagent.warm_resume_seconds` |

See [Cloud Compute](cloud-compute.md) for the full cloud-compute guide.

## Cost caps

SciAgent tracks cost on three separate axes and gates on the aggregate. Per-axis is the source of truth — never collapsed into one number internally, because bench-style honest comparison needs the split.

### Three-axis rollup

`RunCostTracker` (owned by `TaskOrchestrator` while `execute_all` runs, exposed as a process-level "active tracker" so peripheral layers can update it without a constructor dep):

| Axis | What it counts | Source |
|------|----------------|--------|
| `llm_cost_usd` | Per-LLM-call cost | `response._hidden_params["response_cost"]` via litellm, fed by `LLMClient`'s hook |
| `compute_cost_usd` | Sky-realized cluster cost | `sky.cost_report()`, recomputed each poll (idempotent — replaces, doesn't increment) |
| `storage_cost_usd` | Workspace bucket size × per-region rate | Computed once on session shutdown via `finalize_storage` |

`total_usd = llm + compute + storage` — that's the number the kill switch reads.

### Kill switch: `max_cost_usd`

Hard cap on aggregate cost. Set on `OrchestratorConfig`:

```bash
sciagent --set orchestrator.max_cost_usd=25.0 "…"
```

The orchestrator checks once per iteration; on exceed, the loop halts and session-owned clusters are stopped. Companion: `orchestrator.max_wall_seconds` for a wall-clock cap. Both default to `None` (disabled).

### Per-launch prompt: `commit_threshold_usd`

Different mechanism, different intent. `CloudConfig.commit_threshold_usd` (default `$5.00`) is the estimated total above which `compute_run` prompts the user before launching a cluster. This is a per-launch confirmation gate, not a session-wide cap. Precedence: env `SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD` > `CloudConfig` field > `~/.sciagent/config.yaml` `compute.commit_threshold_usd` > $5 default.

### Session token budget: `session_soft_budget`

`AgentConfig.session_soft_budget` is the cumulative token budget for one agent session; the loop triggers compaction (not termination) as it approaches the limit. Resolution: profile default (Anthropic 1M, OpenAI 2M, xAI 2M) → explicit `--set agent.session_soft_budget=N` → `SCIAGENT_SESSION_SOFT_BUDGET` env. `None` on both profile and override disables the soft cap. Distinct from `max_cost_usd`: this one bounds tokens, not dollars, and triggers a compaction rather than a stop.

### Where cost lands in the log

Each `tool_result` event carries a per-call `cost_usd` when litellm reported one. The `session_end` event (fired at `AgentLoop.run` exit) carries session-level totals — same axes, same aggregate — so post-hoc adapters can read one event per session instead of summing per-call rows. See [Provenance Log Schema](provenance_log_schema.md).

## Image Analysis

SciAgent can analyze images including scientific plots, microscopy, diagrams, and visualisations. Supported formats: PNG, JPG/JPEG, GIF, WebP.

```bash
# Analyze a scientific plot
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
| **Chemistry & Materials** | `rdkit`, `ase`, `dwsim` | Molecular analysis, atomistic simulations, chemical process simulation |
| **Molecular Dynamics** | `gromacs` | Biomolecular simulations, soft matter |
| **Photonics & Optics** | `rcwa`, `meep`, `pyoptools` | RCWA for gratings, FDTD electromagnetics, optical ray tracing |
| **CFD & FEM** | `openfoam`, `gmsh`, `elmer` | Fluid dynamics, mesh generation, multiphysics FEM |
| **Post-processing & Visualisation** | `paraview` | Multi-arch (with EGL) — pairs with the OpenFOAM services |
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
