"""
Tool collection package.

Architecture v2: Filesystem-as-Memory Model

This package provides 5 atomic tools that handle 90% of tasks:
- bash (shell.py): Execute shell commands
- file_ops (file_ops.py): Read/write/edit files - THIS IS MEMORY
- search (search.py): Find files (glob) and content (grep)
- web (web.py): Search and fetch web content
- todo (todo.py): Track task progress

Old tools are archived in tools/archive/core_v1/
"""

from .atomic import (
    ShellTool,
    FileOpsTool,
    SearchTool,
    WebTool,
    TodoTool,
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
    # Registry
    "ToolRegistry",
    "ToolResult",
    "BaseTool",
    "FunctionTool",
    "tool",
    "create_atomic_registry",
    "create_default_registry",
]
