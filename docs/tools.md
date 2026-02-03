---
layout: default
title: Tools
nav_order: 4
---

# Tools Reference

SciAgent uses tools to interact with the filesystem, execute commands, and perform research. This document describes all built-in tools.

## Core Tools

### bash

Execute shell commands with automatic timeout handling and retry.

**Parameters:**
- `command` (required): The bash command to execute
- `timeout` (optional): Initial timeout in seconds (default: auto-detected based on command)

**Behavior:**
- Auto-detects appropriate timeout based on command type
- Retries up to 3 times with exponential backoff on timeout
- Captures both stdout and stderr

**Examples:**
```
bash(command="ls -la")
bash(command="npm install", timeout=180)
bash(command="python script.py")
```

**Timeout Defaults:**
- Scaffolding tools (create-react-app, etc.): 300s
- Package installs: 180s
- Builds and tests: 120s
- General commands: 30s

---

### view

Read file contents or list directory entries.

**Parameters:**
- `path` (required): Path to file or directory
- `start_line` (optional): Start line number (1-indexed)
- `end_line` (optional): End line number (-1 for end of file)

**Behavior:**
- For files: Shows content with line numbers
- For directories: Lists contents with file/folder icons

**Examples:**
```
view(path="src/main.py")
view(path="src/main.py", start_line=10, end_line=50)
view(path="./src")
```

---

### write_file

Create or overwrite a file with content.

**Parameters:**
- `path` (required): Path to the file
- `content` (required): Content to write

**Behavior:**
- Creates parent directories if needed
- Overwrites existing files
- Won't write to the agent's own directory (protected)

**Examples:**
```
write_file(path="hello.py", content="print('Hello, World!')")
write_file(path="src/utils/helper.py", content="def add(a, b):\n    return a + b")
```

---

### str_replace

Replace a unique string in a file.

**Parameters:**
- `path` (required): Path to the file
- `old_str` (required): Exact string to replace (must appear exactly once)
- `new_str` (required): Replacement string

**Behavior:**
- Fails if the string appears 0 or more than 1 time
- Won't edit files in the agent's own directory

**Examples:**
```
str_replace(path="config.py", old_str="DEBUG = False", new_str="DEBUG = True")
str_replace(path="src/app.py", old_str="def old_name():", new_str="def new_name():")
```

---

### todo

Manage task list with status tracking.

**Parameters:**
- `command` (required): One of "add", "update", "list", "clear"
- `content` (for add): Task description
- `task_id` (for update): ID of task to update
- `status` (for update): New status (pending, in_progress, completed, failed)

**Behavior:**
- Tracks tasks with unique IDs
- Supports dependencies between tasks
- Shows task list with status indicators

**Examples:**
```
todo(command="add", content="Implement authentication")
todo(command="update", task_id="task_1", status="in_progress")
todo(command="list")
```

---

### web

Search the web or fetch URL content.

**Parameters:**
- `command` (required): "search" or "fetch"
- `query` (for search): Search query string
- `url` (for fetch): URL to fetch

**Behavior:**
- Search: Uses DuckDuckGo, returns list of results with titles, URLs, and snippets
- Fetch: Downloads page content and converts to markdown

**Examples:**
```
web(command="search", query="Python FastAPI tutorial 2024")
web(command="fetch", url="https://fastapi.tiangolo.com/tutorial/")
```

---

### search

Find files and search content with glob and grep.

**Parameters:**
- `command` (required): "glob" or "grep"
- `pattern` (required): File pattern (for glob) or regex (for grep)
- `path` (optional): Directory to search in (default: working directory)
- `include` (for grep, optional): File pattern to include

**Behavior:**
- Glob: Find files matching pattern (e.g., `**/*.py`)
- Grep: Search file contents for regex pattern

**Examples:**
```
search(command="glob", pattern="**/*.py")
search(command="grep", pattern="def main", include="*.py")
search(command="grep", pattern="TODO", path="./src")
```

---

### ask_user

Request user input during agent execution for decisions and clarifications.

**Parameters:**
- `question` (required): The question to ask the user
- `options` (optional): List of choices to present (minimum 2 if provided)
- `context` (optional): Additional context to help the user decide
- `default` (optional): Default choice if user presses Enter

**Behavior:**
- Pauses agent execution and displays the question
- If options provided, shows numbered choices
- Returns the user's response to the agent
- Supports free-form responses when no options given

**When to use:**
- Choosing between simulation services (e.g., MEEP vs RCWA)
- Confirming expensive computation parameters (e.g., simulation time, mesh resolution)
- Clarifying ambiguous scientific requirements
- Trade-off decisions where user preference matters

**When NOT to use:**
- Decisions you can make based on available context
- Routine steps that don't need user input
- Every step of execution (stay autonomous)

**Examples:**
```
ask_user(
    question="Which electromagnetic solver should I use?",
    options=["MEEP (FDTD, broadband)", "RCWA (faster for periodic)", "Both and compare"],
    context="MEEP is more general but slower. RCWA is optimized for layered structures.",
    default="RCWA (faster for periodic)"
)

ask_user(
    question="How long should the molecular dynamics simulation run?",
    options=["10 ns (quick test)", "100 ns (production)", "1 μs (extended)"],
    context="Longer runs give better statistics but take more time."
)

ask_user(question="What convergence criterion should I use for the optimization?")
```

---

## Custom Tools

You can create and load custom tools. See [Custom Tools](../README.md#custom-tools) in the README.

### Creating a Custom Tool

```python
from tools import BaseTool, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "Description for the LLM"
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input parameter"}
        },
        "required": ["input"]
    }

    def execute(self, input: str) -> ToolResult:
        # Your implementation
        return ToolResult(success=True, output="Result")
```

### Loading Custom Tools

```bash
python main.py --load-tools ./my_tools.py "Use my custom tool"
```

The module should either:
- Define a `register_tools(registry)` function
- Export a `TOOLS` list of tool instances
- Use the `@tool` decorator on functions
