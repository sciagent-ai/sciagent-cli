"""
Atomic tools - minimal set of composable primitives.

These 6 tools handle 90% of scientific/engineering tasks:
- shell: Execute bash commands
- file_ops: Read/write/edit files (filesystem is memory)
- search: Find files (glob) and content (grep)
- web: Search and fetch web content
- todo: Track task progress
- service: Run code in containerized simulation environments
"""

from .shell import ShellTool
from .file_ops import FileOpsTool
from .search import SearchTool
from .web import WebTool
from .todo import TodoTool
from .service import ServiceTool

__all__ = [
    "ShellTool",
    "FileOpsTool",
    "SearchTool",
    "WebTool",
    "TodoTool",
    "ServiceTool",
]
