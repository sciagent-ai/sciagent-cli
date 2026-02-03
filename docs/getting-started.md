---
layout: default
title: Getting Started
nav_order: 2
---

# Getting Started

This guide helps you install SciAgent and run your first task.  Even if you have never used an agent framework before, following the steps below will get you up and running quickly.

## Installation

SciAgent requires Python 3.9 or newer.  The recommended way to set it up is with a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

If SciAgent is published on PyPI you can install it directly with `pip install sciagent`.  Installing from source via the `-e` flag makes development and debugging easier, because changes to the code take effect immediately.

## Running your first task

Once installed, the `sciagent` command becomes available.  You must specify a project directory where generated files will be stored.  The directory is created if it does not exist.  For example:

```bash
sciagent --project-dir ~/my-project "Generate a Python script that fetches weather data and prints tomorrow’s forecast"
```

The agent will read any existing files in the project, plan its work, call tools such as `file_ops`, `bash` and `web` as needed, and write new files into `~/my-project`.  When it finishes, a summary of the task result is printed and you can inspect the generated code.

### Interactive mode

If you prefer to guide the agent step‑by‑step, run it with the `--interactive` flag.  This starts a REPL where you can enter tasks one at a time, view intermediate tool results and provide feedback.  Use `Ctrl+C` to interrupt and choose to continue, stop or give feedback.

## Command‑line options

SciAgent’s CLI accepts several flags to tailor its behaviour.  The most common ones are:

| Option | Purpose |
|---|---|
| `--project-dir PATH` | Directory where the agent reads and writes files.  Relative paths are resolved against your current working directory. |
| `--model MODEL_NAME` | Specify the LLM to use (e.g. `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`). |
| `--interactive` | Start a read–eval–print loop instead of executing a single task. |
| `--load-tools FILE` | Load additional tools from a Python module.  The module should expose a `register_tools(registry)` function or a `TOOLS` list. |
| `--subagents` | Enable sub‑agent spawning so the main agent can delegate tasks to specialised agents like the researcher or reviewer. |
| `--resume SESSION_ID` | Resume a previous session saved in the `.agent_states` directory. |
| `--quiet` / `--verbose` | Suppress or amplify console output.  By default, the agent is verbose. |
| `--max-iterations N` | Limit the number of Think → Act → Observe cycles.  (default: 120).  Decrease for simpler tasks. |
| `--temperature T` | Control the randomness of the model’s responses.  A value of `0` makes the agent deterministic. |
| `--system-prompt FILE` | Provide a custom system prompt from a text file to guide the agent’s behaviour. |

Run `sciagent --help` to see the full set of options and default values.
