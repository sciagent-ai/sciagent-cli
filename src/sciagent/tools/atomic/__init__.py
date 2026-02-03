"""
Atomic tools - minimal set of composable primitives.

These 7 tools handle 90% of scientific/engineering tasks:
- shell: Execute bash commands
- file_ops: Read/write/edit files (filesystem is memory)
- search: Find files (glob) and content (grep)
- web: Search and fetch web content
- todo: Track task progress
- service: Run code in containerized simulation environments
- ask_user: Request user input for decisions/clarifications
"""

from .shell import ShellTool
from .file_ops import FileOpsTool
from .search import SearchTool
from .web import WebTool
from .todo import TodoTool
from .service import ServiceTool
from .ask_user import AskUserTool

__all__ = [
    "ShellTool",
    "FileOpsTool",
    "SearchTool",
    "WebTool",
    "TodoTool",
    "ServiceTool",
    "AskUserTool",
]
