---
layout: default
title: Home
nav_order: 1
permalink: /
---

# SciAgent

A terminal-based AI agent for software engineering and scientific computing. SciAgent automates file operations, shell commands, web research, and simulations so you can focus on problem-solving.

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY="your-key"
sciagent --project-dir ~/my-project "Create a Python script that fetches weather data"
```

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](getting-started.md) | Install, configure API keys, run your first task |
| [Configuration](configuration.md) | Models, prompts, tools, sub-agents |
| [Tools](tools.md) | Built-in tools reference |
| [Use Cases](use-cases.md) | Examples for coding, research, and scientific computing |
| [Comparison](comparison.md) | How SciAgent compares to other frameworks |

## For Developers

Building on SciAgent? See the [developer documentation](developers/):

- [Architecture](developers/architecture.md) - Agent loop, context management, internals
- [API Reference](developers/api-reference.md) - Python classes and functions

## Community

SciAgent is open source under the MIT License. [GitHub](https://github.com/sciagent-ai/sciagent-cli)
