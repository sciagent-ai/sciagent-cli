"""
Display Module - Clean, user-friendly output formatting

Handles all terminal output, warning suppression, and tool message formatting.
"""
import sys
import warnings
import threading
import time
from typing import Dict, Any, Optional


# =============================================================================
# ANSI Color Codes
# =============================================================================

class Colors:
    """ANSI escape codes for terminal colors."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    # Foreground
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    # Bright variants
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


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

# Status icons (with colors)
C = Colors

ICONS = {
    "success": f"{C.BRIGHT_GREEN}✓{C.RESET}",
    "error": f"{C.RED}✗{C.RESET}",
    "pending": f"{C.DIM}○{C.RESET}",
    "in_progress": f"{C.BRIGHT_CYAN}◐{C.RESET}",
    "thinking": f"{C.DIM}💭{C.RESET}",
    "tool": f"{C.CYAN}→{C.RESET}",
}

# Plain icons (no color) for reference
ICONS_PLAIN = {
    "success": "✓",
    "error": "✗",
    "pending": "○",
    "in_progress": "◐",
    "thinking": "💭",
    "tool": "→",
}


# =============================================================================
# Spinner - Visual feedback for long-running operations
# =============================================================================

class Spinner:
    """
    Animated spinner for long-running operations.

    Usage:
        with Spinner("Thinking"):
            result = slow_operation()

        # Or with custom spinner style:
        with Spinner("Processing", style="dots"):
            result = slow_operation()

        # With delay (only show spinner if operation takes longer than delay):
        with Spinner("Working", delay=0.5):
            result = maybe_slow_operation()  # No spinner if < 500ms
    """

    STYLES = {
        "dots": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
        "braille": ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"],
        "arrows": ["←", "↖", "↑", "↗", "→", "↘", "↓", "↙"],
        "simple": ["-", "\\", "|", "/"],
    }

    def __init__(
        self,
        message: str = "Working",
        style: str = "dots",
        quiet: bool = False,
        delay: float = 0.0,
        show_hint: bool = True,
        interrupt_event: Optional[threading.Event] = None
    ):
        """
        Args:
            message: Text to display next to spinner
            style: Animation style (dots, braille, arrows, simple)
            quiet: If True, suppress all output
            delay: Seconds to wait before showing spinner. If operation completes
                   before delay, no spinner is shown. Default 0 (immediate).
            show_hint: If True, show "ctrl+c to interrupt" hint and elapsed time
            interrupt_event: Optional threading.Event that signals an interrupt.
                   When set, the spinner stops immediately and clears its line.
        """
        self.message = message
        self.frames = self.STYLES.get(style, self.STYLES["dots"])
        self.quiet = quiet
        self.delay = delay
        self.show_hint = show_hint
        self._stop = threading.Event()
        self._interrupt_event = interrupt_event
        self._thread: Optional[threading.Thread] = None
        self._last_line_len = 0
        self._started_display = False  # Track if we've shown anything
        self._start_time = None  # Track elapsed time

    def _format_elapsed(self, seconds: float) -> str:
        """Format elapsed time as human-readable string."""
        if seconds < 60:
            return f"{int(seconds)}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"

    def _should_stop(self) -> bool:
        """Check if spinner should stop (internal stop or external interrupt)."""
        if self._stop.is_set():
            return True
        if self._interrupt_event and self._interrupt_event.is_set():
            return True
        return False

    def _spin(self):
        """Animation loop running in background thread."""
        # Wait for delay before starting animation
        if self.delay > 0:
            # Use small increments so we can respond to stop signal quickly
            elapsed = 0.0
            while elapsed < self.delay and not self._should_stop():
                time.sleep(0.05)
                elapsed += 0.05
            if self._should_stop():
                return  # Operation finished before delay - show nothing

        self._started_display = True
        self._start_time = time.time()
        i = 0
        while not self._should_stop():
            frame = self.frames[i % len(self.frames)]

            # Build status line with optional hint
            if self.show_hint:
                elapsed = time.time() - self._start_time
                elapsed_str = self._format_elapsed(elapsed)
                hint = f"{Colors.DIM}(ctrl+c to interrupt · {elapsed_str}){Colors.RESET}"
                line = f"\r{Colors.CYAN}{frame}{Colors.RESET} {self.message}... {hint}"
            else:
                line = f"\r{Colors.CYAN}{frame}{Colors.RESET} {Colors.DIM}{self.message}...{Colors.RESET}"

            sys.stdout.write(line)
            sys.stdout.flush()
            self._last_line_len = len(line) + 10  # Account for ANSI codes
            i += 1
            time.sleep(0.1)

        # Clear line immediately when stopped by interrupt
        if self._interrupt_event and self._interrupt_event.is_set():
            self._clear_line()

    def _clear_line(self):
        """Clear the spinner line."""
        if self._started_display:
            sys.stdout.write("\r" + " " * self._last_line_len + "\r")
            sys.stdout.flush()

    def update(self, message: str):
        """Update the spinner message while running."""
        self.message = message

    def __enter__(self):
        if self.quiet:
            return self
        # Only animate spinner in interactive terminals (not when output is piped/captured)
        if not sys.stdout.isatty():
            # Non-TTY: show simple static message (but respect delay)
            if self.delay == 0:
                print(f"{Colors.DIM}{self.message}...{Colors.RESET}")
                self._started_display = True
            # For delayed spinners in non-TTY, we skip the message entirely
            # since we can't clear it if the operation finishes quickly
            return self
        self._stop.clear()
        self._started_display = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        if self.quiet or self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        self._clear_line()


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

        # Suppress noisy warnings from pydantic and litellm
        # These occur when litellm's pydantic models serialize LLM responses
        # with fields that don't match schema (e.g., thinking_blocks for Claude)

        # Filter by message content - catches the actual warning text
        warnings.filterwarnings("ignore", message=".*Pydantic serializer warnings.*")
        warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
        warnings.filterwarnings("ignore", message=".*Expected.*fields but got.*")
        warnings.filterwarnings("ignore", message=".*serialized value may not be as expected.*")

        # Filter by category and module - broad catch for pydantic warnings
        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")
        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.*")

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
            print(f"{C.DIM}Project: {project_dir}{C.RESET}")
        print(f"{C.DIM}{'─' * 60}{C.RESET}")
        print(f"{C.BOLD}Task:{C.RESET} {self._truncate(task, 100)}")
        print(f"{C.DIM}{'─' * 60}{C.RESET}")

    def task_complete(self, stats: Dict[str, Any]):
        """Display task completion summary."""
        if self.quiet:
            return

        iterations = stats.get("iterations", 0)
        tokens = stats.get("tokens", 0)

        print()
        print(f"{C.DIM}{'─' * 60}{C.RESET}")
        print(f"{ICONS['success']} {C.BRIGHT_GREEN}Completed{C.RESET} in {C.BOLD}{iterations}{C.RESET} iterations | ~{tokens} tokens")
        print(f"{C.DIM}{'─' * 60}{C.RESET}")

    # =========================================================================
    # Tool Display
    # =========================================================================

    def tool_start(self, name: str, args: Dict[str, Any]):
        """Display tool execution start."""
        if self.quiet:
            return

        message = self._format_tool_message(name, args)
        print(f"\n{ICONS['tool']} {C.CYAN}{name}{C.RESET}{C.DIM}({self._summarize_args(args)}){C.RESET}")

    def tool_end(self, name: str, success: bool, message: Optional[str] = None, error: Optional[str] = None):
        """Display tool execution result."""
        if self.quiet:
            return

        icon = ICONS['success'] if success else ICONS['error']

        if error:
            print(f"  {icon} {C.RED}Error: {self._truncate(error, 100)}{C.RESET}")
        elif message:
            print(f"  {icon} {C.DIM}{self._truncate(message, 100)}{C.RESET}")
        else:
            status = f"{C.GREEN}Done{C.RESET}" if success else f"{C.RED}Failed{C.RESET}"
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

        # Show truncated thinking in dim italic style
        truncated = self._truncate(text, 200)
        print(f"\n{ICONS['thinking']} {C.DIM}{C.ITALIC}{truncated}{C.RESET}")

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
        print(f"\n{ICONS['error']} {C.RED}{C.BOLD}Error:{C.RESET} {C.RED}{message}{C.RESET}", file=sys.stderr)

    def warning(self, message: str):
        """Display warning message (user-facing only)."""
        if self.quiet:
            return
        print(f"  {C.YELLOW}⚠ Warning: {message}{C.RESET}")

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

        # Status colors
        STATUS_COLOR = {
            "completed": C.BRIGHT_GREEN,
            "in_progress": C.BRIGHT_CYAN,
            "pending": C.WHITE,
            "failed": C.RED,
        }

        for todo in todos:
            status = todo.get("status", "pending")
            content = todo.get("content", "")
            color = STATUS_COLOR.get(status, C.WHITE)

            if status == "completed":
                icon = f"{C.BRIGHT_GREEN}☑{C.RESET}"
            elif status == "in_progress":
                icon = f"{C.BRIGHT_CYAN}◐{C.RESET}"
            else:
                icon = f"{C.DIM}☐{C.RESET}"

            print(f"  {icon} {color}{content}{C.RESET}")

        print(f"\n  {C.DIM}Progress:{C.RESET} {C.BRIGHT_GREEN}{completed}{C.RESET}/{total}")

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
