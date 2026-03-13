"""
Tool collection package.

Architecture v2: Filesystem-as-Memory Model

This package provides atomic tools that handle 90% of tasks:
- bash (shell.py): Execute shell commands (including Docker for services)
- file_ops (file_ops.py): Read/write/edit files - THIS IS MEMORY
- search (search.py): Find files (glob) and content (grep)
- web (web.py): Search and fetch web content
- todo (todo.py): Track task progress with DAG dependencies
- ask_user (ask_user.py): Request user input for decisions/clarifications
- skill (skill.py): Load specialized workflow skills

Background job management:
- bg_status: Check status of background jobs
- bg_output: Get output from a background job
- bg_wait: Wait for a background job to complete
- bg_kill: Terminate a background job

For simulation services (RCWA, MEEP, etc.), use bash to run Docker directly.
Use bash(background=True) for long-running simulations.
See services/registry.yaml for available images.
"""

from .atomic import (
    ShellTool,
    FileOpsTool,
    SearchTool,
    WebTool,
    TodoTool,
    AskUserTool,
    SkillTool,
    BgStatusTool,
    BgOutputTool,
    BgWaitTool,
    BgKillTool,
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
    "SkillTool",
    # Background job tools
    "BgStatusTool",
    "BgOutputTool",
    "BgWaitTool",
    "BgKillTool",
    # Registry
    "ToolRegistry",
    "ToolResult",
    "BaseTool",
    "FunctionTool",
    "tool",
    "create_atomic_registry",
    "create_default_registry",
]
