---
layout: default
title: Configuration
nav_order: 3
---

# Configuration

This document covers all configuration options for SciAgent.

## Environment Variables

### API Keys

Set the API key for your preferred LLM provider:

| Variable | Provider |
|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic (Claude models) |
| `OPENAI_API_KEY` | OpenAI (GPT models) |
| `GOOGLE_API_KEY` | Google (Gemini models) |
| `AZURE_API_KEY` | Azure OpenAI |

### Optional Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LITELLM_LOG` | Set to "DEBUG" for LLM debugging | - |
| `BRAVE_SEARCH_API_KEY` | Brave Search API for web search (falls back to DuckDuckGo if not set) | - |

**Web Search**: The `web` tool tries Brave Search first (better quality), then falls back to DuckDuckGo (no API key required). Get a free Brave API key at https://brave.com/search/api/

## CLI Arguments

### Required

| Argument | Description |
|----------|-------------|
| `task` | The task to execute (unless using `--interactive` or `--resume`) |

### Commonly Used

| Argument | Short | Description | Default |
|----------|-------|-------------|---------|
| `--project-dir` | `-p` | Working directory for generated code | Current directory |
| `--interactive` | `-i` | Run in REPL mode | False |
| `--model` | `-m` | LLM model to use | `anthropic/claude-sonnet-4-20250514` |
| `--subagents` | `-s` | Enable sub-agent spawning | False |

### Advanced

| Argument | Description | Default |
|----------|-------------|---------|
| `--max-iterations` | Maximum agent loop iterations | 30 |
| `--temperature` | LLM temperature (0.0 = deterministic) | 0.0 |
| `--load-tools` | Path to Python module with custom tools | - |
| `--system-prompt` | Path to custom system prompt file | - |
| `--resume` | Session ID to resume | - |
| `--list-sessions` | List available sessions | - |
| `--verbose` | Verbose output | True |
| `--quiet` | Minimal output | False |

## Model Configuration

### Supported Models

SciAgent uses [LiteLLM](https://github.com/BerriAI/litellm) for multi-model support.

**Anthropic:**
```bash
--model anthropic/claude-sonnet-4-20250514
--model anthropic/claude-opus-4-20250514
--model anthropic/claude-3-haiku-20240307
```

**OpenAI:**
```bash
--model openai/gpt-4o
--model openai/gpt-4-turbo
--model openai/gpt-3.5-turbo
```

**Google:**
```bash
--model google/gemini-pro
--model google/gemini-1.5-pro
```

**Local (Ollama):**
```bash
--model ollama/llama3
--model ollama/codellama
--model ollama/mistral
```

**Azure OpenAI:**
```bash
--model azure/gpt-4
```

### Model Selection Tips

- **Default (Claude Sonnet)**: Best balance of capability and speed
- **Claude Opus**: Most capable, use for complex reasoning tasks
- **GPT-4o**: Good alternative, especially for vision tasks
- **Local models**: For privacy-sensitive work or offline use (may have reduced capability)

## AgentConfig (Python API)

When using the Python API, configure the agent with `AgentConfig`:

```python
from agent import AgentLoop, AgentConfig

config = AgentConfig(
    model="anthropic/claude-sonnet-4-20250514",
    temperature=0.0,           # 0.0 = deterministic
    max_tokens=16384,          # Max tokens per response
    max_iterations=30,         # Max agent loop iterations
    working_dir="./project",   # Working directory
    verbose=True,              # Show output
    auto_save=True,            # Auto-save sessions
    state_dir=".agent_states"  # Where to save state
)

agent = AgentLoop(config=config)
```

### Config Options

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `model` | str | LLM model identifier | `anthropic/claude-sonnet-4-20250514` |
| `temperature` | float | Sampling temperature (0-1) | 0.0 |
| `max_tokens` | int | Max tokens per LLM response | 16384 |
| `max_iterations` | int | Max agent loop iterations | 30 |
| `working_dir` | str | Working directory for file operations | "." |
| `verbose` | bool | Enable verbose output | True |
| `auto_save` | bool | Automatically save session state | True |
| `state_dir` | str | Directory for session state files | ".agent_states" |

## Custom System Prompts

Override the default system prompt:

```bash
python main.py --system-prompt ./my_prompt.txt "Your task"
```

Or in Python:

```python
from agent import AgentLoop, AgentConfig

custom_prompt = """You are a specialized agent for data analysis.
Focus on pandas and numpy operations.
Always validate data before processing."""

agent = AgentLoop(
    config=AgentConfig(),
    system_prompt=custom_prompt
)
```

## State Management

### Session Storage

Sessions are stored in `.agent_states/` by default. Each session includes:
- Conversation history
- Todo list state
- Configuration at time of session

### Listing Sessions

```bash
python main.py --list-sessions
```

### Resuming Sessions

```bash
python main.py --resume <session-id>
```

### Clearing State

Delete the `.agent_states/` directory to clear all saved sessions:

```bash
rm -rf .agent_states/
```
