"""
Tool collection package.

Architecture v2: Filesystem-as-Memory Model

This package provides 6 atomic tools that handle 90% of tasks:
- bash (shell.py): Execute shell commands (including Docker for services)
- file_ops (file_ops.py): Read/write/edit files - THIS IS MEMORY
- search (search.py): Find files (glob) and content (grep)
- web (web.py): Search and fetch web content
- todo (todo.py): Track task progress
- ask_user (ask_user.py): Request user input for decisions/clarifications

For simulation services (RCWA, MEEP, etc.), use bash to run Docker directly.
See services/registry.yaml for available images.
"""

from .atomic import (
    ShellTool,
    FileOpsTool,
    SearchTool,
    WebTool,
    TodoTool,
    AskUserTool,
)

from .registry import (
    ToolRegistry,
    ToolResult,
    BaseTool,
    FunctionTool,
    tool,
    create_atomic_registry,
    create_default_registry,
)

__all__ = [
    # Tool classes
    "ShellTool",
    "FileOpsTool",
    "SearchTool",
    "WebTool",
    "TodoTool",
    "AskUserTool",
    # Registry
    "ToolRegistry",
    "ToolResult",
    "BaseTool",
    "FunctionTool",
    "tool",
    "create_atomic_registry",
    "create_default_registry",
]
