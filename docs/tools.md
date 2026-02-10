---
layout: default
title: Tools
nav_order: 4
---

# Tools

SciAgent uses tools to interact with files, run commands, and search the web. The agent automatically selects the right tool for each task.

## File Operations

### view
Read file contents or list directory entries.

```
view(path="src/main.py")
view(path="src/main.py", start_line=10, end_line=50)
view(path="./src")  # Lists directory
```

### write_file
Create or overwrite files.

```
write_file(path="hello.py", content="print('Hello!')")
```

### str_replace
Replace text in a file (string must appear exactly once).

```
str_replace(path="config.py", old_str="DEBUG = False", new_str="DEBUG = True")
```

## Shell

### bash
Execute shell commands with automatic timeout handling.

```
bash(command="ls -la")
bash(command="npm install", timeout=180)
bash(command="python script.py")
```

Default timeouts: scaffolding tools (300s), package installs (180s), builds (120s), general (30s).

## Search

### search
Find files or search content.

**Glob** - find files by pattern:
```
search(command="glob", pattern="**/*.py")
search(command="glob", pattern="src/**/*.ts")
```

**Grep** - search file contents:
```
search(command="grep", pattern="def main", include="*.py")
search(command="grep", pattern="TODO", path="./src")
```

## Web

### web
Search the web or fetch page content.

```
web(command="search", query="Python FastAPI tutorial 2024")
web(command="fetch", url="https://fastapi.tiangolo.com/tutorial/")
```

## Task Management

### todo
Track tasks with status.

```
todo(command="add", content="Implement authentication")
todo(command="update", task_id="task_1", status="in_progress")
todo(command="list")
```

## User Interaction

### ask_user
Request user input for decisions.

```
ask_user(
    question="Which solver should I use?",
    options=["MEEP (FDTD)", "RCWA (faster for periodic)"],
    context="MEEP is general but slower."
)
```

Use for:
- Choosing between approaches
- Confirming expensive operations
- Clarifying ambiguous requirements

## Skills

### skill
Load specialized workflows for complex tasks.

```
skill(name="sci-compute")  # Scientific simulations
skill(name="code-review")  # Code review workflow
skill(name="build-service")  # Docker service building
```

Skills auto-trigger based on your task description. For example, asking about simulations automatically loads the `sci-compute` skill.

## Creating Custom Tools

See [Configuration - Custom Tools](configuration.md#custom-tools) for adding your own tools.
