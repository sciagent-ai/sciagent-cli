---
layout: default
title: Getting Started
nav_order: 2
---

# Getting Started

Install SciAgent and run your first task in minutes.

> **New in v2.0**: cloud compute via SkyPilot, durable provenance log, background subagents with checkpoint/resume. See [What's New in v2.0](whats-new-v2.md) for the full list.

## Installation

Requires Python 3.9+.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .                # base install (local Docker compute)
pip install -e '.[cloud]'       # optional: SkyPilot + AWS extras
pip install -e '.[cloud-all]'   # optional: SkyPilot + AWS, GCP, Azure
```

*PyPI package coming soon—for now, install from source.*

## API Keys

**Required** - Set your LLM provider key (default model is Claude):

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

Get a key at [console.anthropic.com](https://console.anthropic.com/settings/keys).

**Recommended** - Add Brave Search for better web results:

```bash
export BRAVE_SEARCH_API_KEY="your-key-here"
```

Get a free key at [brave.com/search/api](https://brave.com/search/api/). Without this, web search falls back to DuckDuckGo.

## Running your first task

```bash
sciagent --project-dir ~/my-project "Create a hello world Python script"
```

The agent reads/writes files in `~/my-project`, runs shell commands, searches the web, and tracks progress with a todo list. When finished, you'll see a summary and can inspect the generated code.

### Interactive mode

For multi-turn conversations:

```bash
sciagent --project-dir ~/my-project --interactive
```

Press `Ctrl+C` anytime to pause and choose to continue, stop, or redirect.

### Scientific computing

For simulations, SciAgent uses containerized services (SciPy, RCWA, MEEP, etc.):

```bash
sciagent "Run an RCWA simulation for a photonic crystal grating"
```

The agent researches documentation, writes code, and runs it in Docker automatically.

## Command-line options

Common options (defaults from `AgentConfig`):

| Option | Purpose | Default |
|--------|---------|---------|
| `--project-dir PATH` | Directory for reading/writing files | Required |
| `--model NAME` | LLM to use (e.g. `openai/gpt-4.1`) | `anthropic/claude-sonnet-4-6` |
| Fast model | Used for `explore` subagent and content processing. See `defaults.py` | `anthropic/claude-haiku-4-5-20251001` |
| `--interactive` | Multi-turn conversation mode | Off |
| `--subagents` | Enable WorkflowTool for full DAG execution | Off |
| `--max-iterations N` | Max agent loop cycles | 120 |
| `--temperature T` | LLM randomness (0 = deterministic) | 0.0 |
| `--resume ID` | Continue a previous session | — |
| `--list-sessions` | Show available resumable sessions | — |
| `--quiet` | Minimal output | Off |

Run `sciagent --help` for all options.
