---
layout: default
title: Architecture
nav_order: 5
---

# Architecture

This page offers a deep dive into how SciAgent works internally.  It explains the agent loop, context management, LLM client, tool system, sub‑agents, task orchestrator and service registry.  Developers looking to extend or debug SciAgent will find the structural overview and API descriptions helpful.

## High‑level design

SciAgent follows a **Think → Act → Observe** cycle.  The agent receives a task description, plans a sequence of actions, calls tools to carry out those actions, observes their results, and iterates until the goal is met or a limit is reached.  The main components participating in this cycle are:

* **Agent loop** – runs the reasoning cycle, manages context and state, and interfaces with the LLM and tools.
* **LLM client** – wraps the underlying language model API and handles caching, streaming and message formatting.
* **Tool registry** – registers available tools, loads custom tools and executes them on demand.
* **Context window** – stores system prompts, user instructions, assistant messages and tool results.  Summarises older messages when needed to fit into the model’s token limits.
* **State manager** – persists sessions to disk so you can pause and resume runs.
* **Sub‑agents and orchestrators** – allow large tasks to be broken into smaller, parallelisable units with their own context windows and tool sets.

The following sections detail each part of this architecture.

## Agent loop

The core of SciAgent is implemented in `sciagent.agent.AgentLoop`.  An `AgentConfig` object defines the project directory, model, iteration limits, temperature, state directory and other parameters.  When `AgentLoop.run()` is called with a task description, the following sequence occurs at each iteration:

1. **Context building** – compile a list of messages: the system prompt, the initial task, previous assistant responses, user feedback (if any) and tool results.
2. **LLM invocation** – pass the messages to `LLMClient.chat()`.  The model may respond with plain text, tool calls or a mixture.  Tool calls are encoded as JSON objects describing the function name and arguments.
3. **Tool execution** – for each tool call, the agent fetches the corresponding tool from the registry and invokes its `execute()` method.  Results (including errors) are appended to the context as messages.
4. **Observation** – check whether the tool outputs or model response indicate that the task is complete.  The agent also detects error patterns (e.g. repeated timeouts or syntax errors) and may ask the model for a fix.
5. **Iteration control** – update counters for iterations and tokens.  If the maximum is reached, summarise the context or stop the run with a warning.

The agent automatically saves snapshots of its state under `.agent_states` so that runs can be resumed via the `--resume` flag.  When resuming, the context window and todo list are restored and iteration counters continue from where they left off.

## Context and state

The conversation history lives in a `ContextWindow`, an ordered list of messages.  Messages belong to three roles: `system`, `user` or `assistant`.  Tool results are inserted as assistant messages containing a `tool_result` field.  To remain within the model’s maximum token limit, the `ContextWindow` periodically summarises the oldest messages by calling the LLM with a summarisation prompt.  This ensures that important information is retained while the total token count stays manageable.

A `StateManager` persists the context, todo list and metadata to JSON files.  Each session has a unique ID and timestamped subdirectory.  Resuming a session loads the state from disk and continues the run without starting from scratch.

## LLM client

All communication with the language model passes through `sciagent.llm.LLMClient`.  This wrapper around litellm normalises message formats, supports multiple providers, and exposes a consistent API:

* `chat(messages, tools, **kwargs)` – send a list of messages and an optional list of tool schemas.  Returns a structured response containing assistant messages and any tool calls.  Supports streaming via the `chat_stream` variant.
* `ask(prompt, model, system)` – convenience function for single‑turn completions without the agent loop.
* `configure_cache(backend)` – configure caching backends such as in‑memory, Redis or disk caching.  Caching reduces cost by reusing responses to identical prompts on supported providers.

The client also extracts tool definitions from the registry and presents them in a format that the model understands.  When using Anthropic models, litellm’s prompt caching is automatically enabled.

## Tool system

SciAgent relies on tools to perform external actions.  Tools are Python classes that implement an `execute()` method, declare a JSON‑schema describing their parameters, and return a `ToolResult` with fields `success`, `output`, `error` and `metadata`.  Tools may be atomic (complex operations) or base (simple fallback operations).

### Atomic tools

Atomic tools provide powerful, composable actions that cover file I/O, searching, shell execution, web access, task management and simulation.  They are defined in `sciagent.tools.atomic` and include:

| Tool | Purpose |
|---|---|
| **bash/shell** | Execute shell commands with controlled timeouts and output truncation.  Useful for compiling code, running tests or launching scripts. |
| **file_ops** | Read files (with optional line ranges), write new content, perform string replacements and list directories.  Automatically detects file types. |
| **search** | Perform glob or regex searches across directories.  Configure recursive search, case sensitivity and context lines. |
| **web** | Search the web and fetch page content.  Implements rate‑limiting and categorises results by trustworthiness (peer‑reviewed, preprint, government, etc.). |
| **todo** | Manage a directed acyclic graph of tasks with statuses, types and dependencies.  Supports queries for ready tasks, blocked tasks and execution order. |
| **service** | Run code inside Docker containers for scientific computing.  Services such as SciPy, RDKit, SymPy and OpenFOAM are described in a YAML registry. |
| **ask_user** | Request user input for decisions and clarifications.  Pauses execution to ask questions about service selection, simulation parameters or ambiguous requirements. |

### Base tools

When the atomic tools cannot be loaded (e.g. due to missing dependencies), SciAgent falls back to base tools defined in `sciagent.tools`:

* `bash` – basic shell command execution without advanced features.
* `view` – read files or list directories with line numbers.
* `write_file` – write content to a file.
* `str_replace` – replace occurrences of a string within a file.

### Custom tools

Developers can register their own tools by decorating a function with `@tool` or creating a subclass of `BaseTool`.  The `ToolRegistry` handles registration, lookup and execution.  A module loaded via `--load-tools` should implement a `register_tools(registry)` function or expose a `TOOLS` list.  When the agent starts, these tools become available to the language model.

## Sub‑agents

For large or multi‑stage tasks, SciAgent can spawn sub‑agents.  Each sub‑agent has its own `SubAgentConfig` specifying the model, iteration limit and allowed tools.  Sub‑agents run the same Think → Act → Observe loop but cannot spawn further sub‑agents, preventing runaway recursion.  They communicate their results back to the parent agent via `SubAgentResult` objects.

Built‑in sub‑agents include:

| Name | Role | Allowed tools |
|---|---|---|
| **researcher** | Performs file and web research, summarises findings and gathers data. | `file_ops`, `search`, `web`, `bash` |
| **reviewer** | Reviews code for correctness, style and potential improvements. | `file_ops`, `search`, `bash` |
| **test_writer** | Generates unit tests based on existing code. | `file_ops`, `search`, `bash` |
| **general** | A catch‑all assistant for miscellaneous tasks not covered by other sub‑agents. | all default tools |

To enable sub‑agent spawning, pass the `--subagents` flag.  You can also define custom sub‑agents by registering `SubAgentConfig` objects with `SubAgentRegistry`.

## Task orchestration

The `todo` tool and `TaskOrchestrator` enable complex, dependency‑aware workflows.  Define a list of tasks where each item can depend on the results of others.  The orchestrator analyses the dependency graph, determines which tasks can run in parallel, and schedules execution.  Each task can specify a `type` (e.g. research, code, validate, review) and optionally assign a sub‑agent.  Results produced by one task are passed as input to dependent tasks via the `produces` and `target` fields.

The orchestrator provides methods to execute tasks sequentially (`execute_next`) or in parallel batches (`execute_ready_parallel`).  It returns a summary of results, including success status and output for each task.  Use this mechanism to automate multi‑stage pipelines such as research → code generation → testing → optimisation → review.

## Service registry

The `service` tool integrates containerised scientific environments.  Each service is described in `src/sciagent/services/registry.yaml` with fields such as `name`, `image`, `license`, `capabilities`, `files`, `run_examples` and `timeout`.  SciAgent can list available services, check whether they are installed, pull/build images and run code inside them.  Some notable services are:

| Service | Short description |
|---|---|
| `scipy-base` | General scientific Python stack with NumPy, SciPy, Matplotlib and Pandas. |
| `rdkit` | Cheminformatics library for molecule manipulation and fingerprinting. |
| `sympy` | Symbolic mathematics for algebra, calculus and equation solving. |
| `cvxpy` | Convex optimisation modeling language. |
| `rcwa` | Rigorous coupled‑wave analysis for photonic simulations. |
| `meep` | Finite‑difference time‑domain simulation of electromagnetic systems. |
| `openfoam` | Computational fluid dynamics solver for incompressible/compressible flows. |
| `ngspice` | Circuit simulator with PySpice integration for analog and digital circuits. |

To run a service, call the `service` tool with `action="run"` and provide Python `code` or a shell `command`.  You can also mount files into the container and specify a timeout.  The agent handles pulling and building images on demand.

## API reference

Below is a summary of key classes and functions exposed by the package.  For detailed signatures and implementation details, refer to the source code in `src/sciagent`.

### Core classes and structures

* **AgentConfig** – holds configuration parameters such as model name, project directory, iteration limits and verbosity.  Accepts optional `tool_registry`, `subagent_registry` and `state_dir` arguments.
* **AgentLoop** – implements the main loop with methods `run()`, `run_interactive()` and hooks for tool execution and reasoning.
* **ContextWindow** – manages the ordered list of messages and performs summarisation when required.
* **StateManager** – serialises and deserialises session objects to JSON files under `.agent_states`.
* **TodoItem** / **TodoList** – represent tasks in the todo graph, with statuses, dependencies and results.
* **TaskOrchestrator** – analyses the todo graph and executes tasks in order or batches, optionally using sub‑agents.

### LLM client

* **LLMClient.chat()** – send messages and tool definitions to the model and receive a response containing assistant text and tool calls.
* **LLMClient.chat_stream()** – stream responses incrementally, suitable for interactive UIs.
* **ask()** – simple wrapper for single‑turn completions.
* **configure_cache()** – enable caching backends (local, Redis or disabled).

### Tools system

* **BaseTool** – abstract base class specifying `name`, `description`, `parameters` and `execute()` signature.
* **FunctionTool** – converts a Python function into a tool, inferring its parameter schema from type annotations.
* **ToolRegistry** – register/unregister tools, load them from modules and call them by name.
* **tool decorator** – annotate a function so that it is auto‑registered when the module is loaded.
* **create_default_registry()** – construct a registry preloaded with atomic tools (or base tools if dependencies are missing).

### Sub‑agents and orchestration

* **SubAgentConfig** – defines a sub‑agent’s name, description, system prompt, model, iteration budget and allowed tools.
* **SubAgent** – runs an isolated agent instance with its own context window and registry.  Returns a `SubAgentResult` after completion.
* **SubAgentRegistry** – stores built‑in and custom sub‑agents.  Provides lookup and registration methods.
* **SubAgentOrchestrator** – launches sub‑agents sequentially or concurrently and aggregates their results.
* **TaskOrchestrator** – executes tasks defined in a `TodoList`, respecting dependencies and passing results between tasks.

---

Understanding this architecture will help you tailor SciAgent to your own projects, troubleshoot unexpected behaviour and contribute improvements back to the community.
