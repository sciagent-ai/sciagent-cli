"""
Core tools for the SCI Agent.

This subpackage contains implementations of general purpose
capabilities such as executing shell commands, manipulating
files, searching directories, managing tasks and progress,
fetching web content, and editing Jupyter notebooks. These
tools are loaded by default and are suitable for most
scientific computing and engineering workflows.

Enhanced tools added for Claude Code parity:
- multi_edit: Atomic batch file editing with rollback
- git_operations: Smart git workflows with auto-commit
- advanced_file_ops: Enhanced file operations with analysis
- performance_monitor: Real-time performance monitoring

To extend the agent with additional core tools, create a new
module in this directory and define a subclass of
:class:`~sciagent.base_tool.BaseTool` or expose a
``get_tool()`` function returning an instance.
"""

__all__ = [
    "str_replace_editor",
    "bash", 
    "glob_search",
    "grep_search",
    "list_directory",
    "notebook_edit",
    "task_agent",
    "todo_write",
    "create_summary", 
    "update_progress_md",
    "web_fetch",
    "web_search",
    "ask_user_step",
    # Memory system tools
    "save_memory",
    "recall_memory",
    # Reflection and learning tools
    "reflect",
    # Enhanced tools for Claude Code parity
    "multi_edit",
    "git_operations", 
    "advanced_file_ops",
    "performance_monitor"
]