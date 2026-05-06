---
layout: default
title: Home
nav_order: 1
permalink: /
---

# SciAgent

A terminal-based AI agent for software engineering and scientific computing. SciAgent automates file operations, shell commands, web research, simulations, and cloud compute so you can focus on problem-solving.

{: .note }
**v2.0 is current.** Looking for v1.0 docs? Browse them on the [`release/v1.0` branch on GitHub](https://github.com/sciagent-ai/sciagent-cli/tree/release/v1.0/docs). Highlights of v2.0: cloud compute via SkyPilot, durable provenance log, task orchestration with background subagents and checkpoint/resume. See [What's New in v2.0](whats-new-v2.md).

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY="your-key"
sciagent --project-dir ~/my-project "Create a Python script that fetches weather data"
```

## Documentation

| Guide | Description |
|-------|-------------|
| [What's New in v2.0](whats-new-v2.md) | Migration notes from v1.0 + headline features |
| [Getting Started](getting-started.md) | Install, configure API keys, run your first task |
| [Configuration](configuration.md) | Models, prompts, tools, sub-agents, cloud setup |
| [Tools](tools.md) | Built-in tools reference |
| [Cloud Compute](cloud-compute.md) | SkyPilot integration, cluster lifecycle, workspace bucket |
| [Task Orchestration](task-orchestration.md) | Task index, background subagents, checkpoint & resume |
| [Provenance Log Schema](provenance_log_schema.md) | Durable JSONL audit trail (v1) |
| [Use Cases](use-cases.md) | Examples for coding, research, and scientific computing |
| [Case Studies](case-studies/) | Real-world reproductions of published research |
| [Comparison](comparison.md) | How SciAgent compares to other frameworks |

## For Developers

Building on SciAgent? See the [developer documentation](developers/):

- [Architecture](developers/architecture.md) - Agent loop, context management, internals
- [API Reference](developers/api-reference.md) - Python classes and functions

## Community

SciAgent is open source under the Apache 2.0 License. [GitHub](https://github.com/sciagent-ai/sciagent-cli)
