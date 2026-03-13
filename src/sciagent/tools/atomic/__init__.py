"""
Atomic tools - minimal set of composable primitives.

Core tools handle 90% of scientific/engineering tasks:
- shell: Execute bash commands (including Docker for simulation services)
- file_ops: Read/write/edit files (filesystem is memory)
- search: Find files (glob) and content (grep)
- web: Search and fetch web content
- todo: Track task progress with DAG dependencies
- ask_user: Request user input for decisions/clarifications
- skill: Load specialized workflow skills

Background job management:
- bg_status: Check status of background jobs
- bg_output: Get output from a background job
- bg_wait: Wait for a background job to complete
- bg_kill: Terminate a background job

For simulation services (RCWA, MEEP, etc.), use shell to run Docker directly.
Use shell(background=True) for long-running simulations.
See services/registry.yaml for available images.
"""

from .shell import ShellTool, ExecLogger, get_exec_logger
from .file_ops import FileOpsTool
from .search import SearchTool
from .web import WebTool, FetchLogger, get_fetch_logger
from .todo import TodoTool, ContentValidator
from .ask_user import AskUserTool
from .skill import SkillTool
from .bg_tools import BgStatusTool, BgOutputTool, BgWaitTool, BgKillTool

__all__ = [
    "ShellTool",
    "ExecLogger",
    "get_exec_logger",
    "FileOpsTool",
    "SearchTool",
    "WebTool",
    "FetchLogger",
    "get_fetch_logger",
    "TodoTool",
    "ContentValidator",
    "AskUserTool",
    "SkillTool",
    "BgStatusTool",
    "BgOutputTool",
    "BgWaitTool",
    "BgKillTool",
]
