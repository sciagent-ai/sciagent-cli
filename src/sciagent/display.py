"""
Display Module - Clean, user-friendly output formatting

Handles all terminal output, warning suppression, and tool message formatting.
"""
import sys
import warnings
from typing import Dict, Any, Optional


# =============================================================================
# Tool Labels - Map raw tool calls to human-readable messages
# =============================================================================

TOOL_LABELS = {
    # Default tools
    "bash": "Running: {command}",
    "view": "Reading {path}",
    "write_file": "Writing {path}",
    "str_replace": "Editing {path}",

    # Common additional tools
    "todo": "Updating task list",
    "git": "Git {command}",
    "web_search": "Searching: {query}",
    "http_request": "{method} {url}",
    "calculate": "Calculating: {expression}",
    "read_url": "Fetching {url}",
    "json_query": "Querying JSON",
    "format_code": "Formatting {language} code",

    # Fallback patterns
    "search": "Searching for {pattern}",
    "grep": "Searching: {pattern}",
    "find": "Finding files: {pattern}",
}

# Status icons
ICONS = {
    "success": "✓",
    "error": "✗",
    "pending": "○",
    "in_progress": "◐",
    "thinking": "💭",
    "tool": "→",
}


# =============================================================================
# Display Class
# =============================================================================

class Display:
    """
    Centralized display manager for clean terminal output.

    Usage:
        display = Display(verbose=True)
        display.setup()
        display.task_start("Create fibonacci script")
        display.tool_start("file_ops", {"command": "write", "path": "fib.py"})
        display.tool_end("file_ops", success=True, message="Wrote 50 lines")
        display.task_complete({"iterations": 5, "tokens": 1200})
    """

    def __init__(self, verbose: bool = True, quiet: bool = False):
        self.verbose = verbose
        self.quiet = quiet
        self._setup_done = False

    def setup(self):
        """Initialize display - suppress warnings, detect terminal."""
        if self._setup_done:
            return

        # Suppress noisy warnings
        warnings.filterwarnings("ignore", module="pydantic")
        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
        warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")

        self._setup_done = True

    # =========================================================================
    # Task Lifecycle
    # =========================================================================

    def task_start(self, task: str, project_dir: Optional[str] = None):
        """Display task header."""
        if self.quiet:
            return

        print()
        if project_dir:
            print(f"Project: {project_dir}")
        print("─" * 60)
        print(f"Task: {self._truncate(task, 100)}")
        print("─" * 60)

    def task_complete(self, stats: Dict[str, Any]):
        """Display task completion summary."""
        if self.quiet:
            return

        iterations = stats.get("iterations", 0)
        tokens = stats.get("tokens", 0)

        print()
        print("─" * 60)
        print(f"{ICONS['success']} Completed in {iterations} iterations | ~{tokens} tokens")
        print("─" * 60)

    # =========================================================================
    # Tool Display
    # =========================================================================

    def tool_start(self, name: str, args: Dict[str, Any]):
        """Display tool execution start."""
        if self.quiet:
            return

        message = self._format_tool_message(name, args)
        print(f"\n{ICONS['tool']} {message}")

    def tool_end(self, name: str, success: bool, message: Optional[str] = None, error: Optional[str] = None):
        """Display tool execution result."""
        if self.quiet:
            return

        icon = ICONS['success'] if success else ICONS['error']

        if error:
            print(f"  {icon} Error: {self._truncate(error, 100)}")
        elif message:
            print(f"  {icon} {self._truncate(message, 100)}")
        else:
            status = "Done" if success else "Failed"
            print(f"  {icon} {status}")

    # =========================================================================
    # Thinking / Response Display
    # =========================================================================

    def thinking(self, text: str):
        """Display LLM thinking/reasoning (dimmed style)."""
        if self.quiet or not self.verbose:
            return

        if not text:
            return

        # Show truncated thinking
        truncated = self._truncate(text, 200)
        print(f"\n{ICONS['thinking']} {truncated}")

    def response(self, text: str):
        """Display final response."""
        if self.quiet:
            return

        print(f"\n{text}")

    # =========================================================================
    # Progress / Status
    # =========================================================================

    def status(self, message: str):
        """Display a status message."""
        if self.quiet:
            return
        print(f"  {message}")

    def progress(self, current: int, total: int, label: str = ""):
        """Display progress indicator."""
        if self.quiet:
            return

        if label:
            print(f"  [{current}/{total}] {label}")
        else:
            print(f"  [{current}/{total}]")

    # =========================================================================
    # Errors and Warnings
    # =========================================================================

    def error(self, message: str):
        """Display error message (always shown)."""
        print(f"\n{ICONS['error']} Error: {message}", file=sys.stderr)

    def warning(self, message: str):
        """Display warning message (user-facing only)."""
        if self.quiet:
            return
        print(f"  Warning: {message}")

    # =========================================================================
    # Todo List Display
    # =========================================================================

    def todo_list(self, todos: list):
        """Display formatted todo list."""
        if self.quiet or not todos:
            return

        print()
        completed = sum(1 for t in todos if t.get("status") == "completed")
        total = len(todos)

        for todo in todos:
            status = todo.get("status", "pending")
            content = todo.get("content", "")

            if status == "completed":
                icon = "☑"
            elif status == "in_progress":
                icon = "◐"
            else:
                icon = "☐"

            print(f"  {icon} {content}")

        print(f"\n  Progress: {completed}/{total}")

    # =========================================================================
    # Helpers
    # =========================================================================

    def _format_tool_message(self, name: str, args: Dict[str, Any]) -> str:
        """Convert tool call to human-readable message."""
        template = TOOL_LABELS.get(name)

        if template is None:
            # Unknown tool - show name and brief args
            return f"{name}({self._summarize_args(args)})"

        if isinstance(template, dict):
            # Sub-command tools like file_ops
            cmd = args.get("command", "")
            template = template.get(cmd, f"{name}.{cmd}")

        try:
            return template.format(**args)
        except KeyError:
            # Missing format key - fall back to simple format
            return f"{name}: {self._summarize_args(args)}"

    def _summarize_args(self, args: Dict[str, Any]) -> str:
        """Create brief summary of tool arguments."""
        if not args:
            return ""

        # Prioritize common keys
        for key in ["path", "command", "content", "pattern", "query"]:
            if key in args:
                value = str(args[key])
                return self._truncate(value, 50)

        # Fall back to first key
        first_key = next(iter(args), None)
        if first_key:
            value = str(args[first_key])
            return f"{first_key}={self._truncate(value, 40)}"

        return ""

    def _truncate(self, text: str, max_len: int) -> str:
        """Truncate text with ellipsis."""
        if not text:
            return ""

        # Remove newlines for display
        text = text.replace("\n", " ").strip()

        if len(text) <= max_len:
            return text

        return text[:max_len - 3] + "..."


# =============================================================================
# Convenience function
# =============================================================================

def create_display(verbose: bool = True, quiet: bool = False) -> Display:
    """Create and setup a Display instance."""
    display = Display(verbose=verbose, quiet=quiet)
    display.setup()
    return display
