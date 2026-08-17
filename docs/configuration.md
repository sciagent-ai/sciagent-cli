---
layout: default
title: Configuration
nav_order: 3
---

# Configuration

SciAgent has two configuration surfaces:

- **CLI / YAML configuration** for normal `sciagent run` usage
- **Python dataclasses** for embedding SciAgent inside another program

The most important thing to know is that the current CLI uses subcommands:

```bash
sciagent run ...
sciagent config ...
```

## Configuration layers

### CLI configuration

### Resolution order

When you run `sciagent run`, config is merged in this order, with later layers winning:

1. Built-in dataclass defaults
2. `~/.sciagent/config.yaml`
3. `<project>/.sciagent.yaml`
4. `--config PATH`
5. `--set KEY=VALUE`

This is implemented in [`src/sciagent/config.py`](../src/sciagent/config.py).

### Inspect the effective config

```bash
sciagent config keys
sciagent config show --project-dir ~/my-project
sciagent config show --config ./run.yaml --set agent.max_iterations=40
```

### Run with overrides

```bash
sciagent run \
  --project-dir ~/my-project \
  --config ./run.yaml \
  --set agent.max_iterations=40 \
  --set orchestrator.max_cost_usd=25.0 \
  --task "Reproduce the analysis and summarize the result"
```

`--set` values are parsed as YAML scalars, so `true`, `false`, `null`, integers, and floats work as expected.

## Common CLI keys

These are the knobs most users will reach for first.

### `agent.*`

| Key | Purpose | Default |
|-----|---------|---------|
| `agent.model` | Main model id | `anthropic/claude-sonnet-4-6` |
| `agent.temperature` | Sampling temperature | `0.0` |
| `agent.max_iterations` | Agent-loop cap | `120` |
| `agent.max_tokens` | Per-call output token cap | `16384` |
| `agent.session_soft_budget` | Cumulative session token budget before compaction pressure | provider/profile dependent |
| `agent.reasoning_effort` | Model reasoning setting | `medium` |

### `orchestrator.*`

| Key | Purpose | Default |
|-----|---------|---------|
| `orchestrator.enable_data_gate` | Validate acquired data before downstream analysis | `true` |
| `orchestrator.enable_exec_gate` | Validate that claimed commands actually ran | `true` |
| `orchestrator.enable_verification` | Run the fresh-context verifier at the end | `true` |
| `orchestrator.verification_threshold` | Minimum confidence for a passing verifier verdict | `0.7` |
| `orchestrator.verifier_model` | Override the verifier model | unset |
| `orchestrator.scientific_model` | Override the planning/scientific tier | unset |
| `orchestrator.coding_model` | Override the coding tier for subagents | unset |
| `orchestrator.fast_model` | Override the fast tier | unset |
| `orchestrator.vision_model` | Override the vision tier | unset |
| `orchestrator.verifier_include_child_sessions` | Let the verifier inspect child session logs | `true` |
| `orchestrator.max_wall_seconds` | Hard wall-clock cap | unset |
| `orchestrator.max_cost_usd` | Hard aggregate cost cap | unset |

Run `sciagent config keys` to see the complete supported set straight from the code.

## Model selection

### Alternative models by provider

Use `--model` to swap the main agent model:

```bash
sciagent run --model openai/gpt-4.1 --task "Summarize README.md"
sciagent run --model gemini/gemini-3-pro-preview --task "Analyze this diagram"
sciagent run --model deepseek/deepseek-reasoner --task "Solve this physics problem"
```

If you want different models for different roles, use `--set` on the orchestrator role overrides:

```bash
sciagent run \
  --set orchestrator.verifier_model=openai/gpt-5.4 \
  --set orchestrator.fast_model=anthropic/claude-haiku-4-5-20251001 \
  --task "Run the workflow and verify it with a different model"
```

SciAgent's default role mapping lives in [`src/sciagent/defaults.py`](../src/sciagent/defaults.py).

## Prompts and custom tools

### Custom system prompt

```bash
sciagent run --system-prompt ./my_prompt.txt --task "Translate comments to Spanish"
```

### Custom tools

#### Extra tools

```python
# my_tools.py
from sciagent.tools import tool, ToolResult

@tool(name="count_lines", description="Count lines in a file")
def count_lines(path: str) -> ToolResult:
    with open(path) as f:
        return ToolResult(success=True, output=str(sum(1 for _ in f)))

TOOLS = [count_lines]
```

```bash
sciagent run --load-tools ./my_tools.py --task "How many lines are in main.py?"
```

### Custom skills

```bash
sciagent run --skills-dir ./skills --task "Use our local review workflow"
```

## Cost caps

SciAgent has two different control planes here:

- **Per-launch cloud confirmation**: the compute layer can prompt before an expensive cluster launch
- **Session-wide orchestrator kill switches**: hard caps checked while the workflow runs

### Session-wide hard caps

```bash
sciagent run \
  --set orchestrator.max_wall_seconds=3600 \
  --set orchestrator.max_cost_usd=25.0 \
  --task "Run the full benchmark"
```

### Verification tuning

```bash
sciagent run \
  --set orchestrator.enable_verification=false \
  --task "Run without the end-of-session verifier"

sciagent run \
  --set orchestrator.verifier_include_child_sessions=false \
  --task "Run the no-recursion verifier ablation"
```

## Python embedding

For programmatic use, the relevant dataclasses are:

- [`AgentConfig`](../src/sciagent/agent.py) for the agent loop
- [`OrchestratorConfig`](../src/sciagent/orchestrator.py) for verification and workflow policy
- [`CloudConfig`](../src/sciagent/compute/__init__.py) for cloud-compute defaults

```python
from sciagent import AgentConfig, AgentLoop, OrchestratorConfig
from sciagent.compute import CloudConfig

agent = AgentLoop(
    config=AgentConfig(
        model="anthropic/claude-sonnet-4-6",
        max_iterations=80,
    ),
    orchestrator_config=OrchestratorConfig(
        enable_verification=True,
        verifier_model="openai/gpt-5.4",
        max_cost_usd=25.0,
    ),
    cloud_config=CloudConfig(
        commit_threshold_usd=10.0,
        default_timeout_sec=7200,
    ),
)
```

`CloudConfig` is mainly for Python embedding. In the CLI, the authoritative surface is the layered config described above plus environment variables and per-tool arguments.

## Related guides

- [Getting Started](getting-started.md)
- [Cloud Compute](cloud-compute.md)
- [Task Orchestration](task-orchestration.md)
- [Tools](tools.md)
