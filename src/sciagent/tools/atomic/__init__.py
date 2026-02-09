"""
Atomic tools - minimal set of composable primitives.

These 7 tools handle 90% of scientific/engineering tasks:
- shell: Execute bash commands (including Docker for simulation services)
- file_ops: Read/write/edit files (filesystem is memory)
- search: Find files (glob) and content (grep)
- web: Search and fetch web content
- todo: Track task progress
- ask_user: Request user input for decisions/clarifications
- skill: Load specialized workflow skills

For simulation services (RCWA, MEEP, etc.), use shell to run Docker directly.
See services/registry.yaml for available images.
"""

from .shell import ShellTool
from .file_ops import FileOpsTool
from .search import SearchTool
from .web import WebTool
from .todo import TodoTool
from .ask_user import AskUserTool
from .skill import SkillTool

__all__ = [
    "ShellTool",
    "FileOpsTool",
    "SearchTool",
    "WebTool",
    "TodoTool",
    "AskUserTool",
    "SkillTool",
]
