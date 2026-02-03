---
layout: default
title: Getting Started
nav_order: 2
---

# Getting Started

This guide walks you through installing and running SciAgent for the first time.

## Prerequisites

- Python 3.9 or higher
- An API key from one of: Anthropic, OpenAI, Google, or a local model setup

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/sciagent-ai/sciagent-1.git
cd sciagent-1
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

Or create a virtual environment first:

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Set Up API Keys

Set your preferred LLM provider's API key as an environment variable:

```bash
# Anthropic (default)
export ANTHROPIC_API_KEY="sk-ant-..."

# OpenAI
export OPENAI_API_KEY="sk-..."

# Google
export GOOGLE_API_KEY="..."
```

You can also add this to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.) for persistence.

## Your First Task

### Basic Usage

Run a simple task:

```bash
python main.py --project-dir ~/my-project "Create a Python script that prints hello world"
```

The `--project-dir` flag specifies where generated files will be created. This is required to prevent the agent from modifying its own source code.

### Interactive Mode

For multiple tasks in a session:

```bash
python main.py --project-dir ~/my-project --interactive
```

In interactive mode:
- Type your task and press Enter
- Type `status` to see current state and todos
- Type `clear` to reset the conversation
- Type `exit` to quit

### Using Different Models

```bash
# OpenAI GPT-4
python main.py --model openai/gpt-4o "Your task"

# Google Gemini
python main.py --model google/gemini-pro "Your task"

# Local Ollama
python main.py --model ollama/llama3 "Your task"
```

## Example Tasks

### Code Generation

```bash
python main.py --project-dir ~/projects/demo "Create a FastAPI server with a /health endpoint"
```

### Code Analysis

```bash
python main.py --project-dir ~/my-existing-project "Explain the architecture of this codebase"
```

### Research + Implementation

```bash
python main.py --project-dir ~/projects/demo --subagents \
    "Research best practices for Python logging and implement a logging module"
```

### Multi-Step Tasks

The agent automatically creates a todo list for complex tasks:

```bash
python main.py --project-dir ~/projects/demo \
    "Create a CLI tool with: 1) argument parsing 2) config file support 3) logging"
```

## Session Management

### Save and Resume

Sessions are automatically saved. To resume:

```bash
# List available sessions
python main.py --list-sessions

# Resume a specific session
python main.py --resume abc123def456
```

## Troubleshooting

### API Key Not Found

If you see "API key not found" errors:
1. Verify the environment variable is set: `echo $ANTHROPIC_API_KEY`
2. Make sure you're using the correct variable name for your provider
3. Restart your terminal if you just added it to your shell profile

### Timeout Errors

For long-running tasks, increase the max iterations:

```bash
python main.py --max-iterations 50 "Complex task here"
```

### Permission Errors

The agent won't write to its own directory. Always use `--project-dir` to specify a different location.

## Next Steps

- Read [Tools Reference](tools.md) to understand available tools
- See [Configuration](configuration.md) for all options
- Learn about [Sub-Agents](subagents.md) for complex tasks
- Check [API Reference](api-reference.md) for programmatic usage
