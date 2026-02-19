"""
Shell execution tool.

Execute bash commands with smart timeout handling and output truncation.

Token Optimization:
- Verbose commands (pip, npm, cargo, etc.) have output truncated
- Success: show summary only
- Failure: show last 40 lines + error
- Full logs saved to _logs/ directory

Visual Output:
- Detects generated image files (png, jpg, svg, pdf)
- Automatically opens them for viewing on macOS
"""

from __future__ import annotations

import json
import subprocess
import os
import hashlib
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Set, List
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


# =============================================================================
# EXECUTION LOGGING - Audit trail for command execution
# =============================================================================

class ExecLogger:
    """
    Logs all command executions for external validation.

    This creates an immutable audit trail that the model cannot fabricate.
    The orchestrator can compare these logs against task claims to detect
    when the model claims to have run something it didn't.

    Log format (JSONL):
    {
        "timestamp": "2025-01-15T10:30:00",
        "command": "python simulate.py",
        "exit_code": 0,
        "success": true,
        "duration_seconds": 45.2,
        "stdout_preview": "first 500 chars...",
        "stderr_preview": "first 500 chars...",
        "output_size": 12345,
        "timeout": false,
        "working_dir": "/path/to/dir",
        "error_indicators": []
    }
    """

    _instance = None
    _log_dir: Path = None
    _log_file: Path = None

    # Indicators of execution problems
    ERROR_INDICATORS = [
        "error:",
        "exception:",
        "traceback",
        "failed",
        "fatal:",
        "segmentation fault",
        "killed",
        "oom",
        "out of memory",
        "permission denied",
        "not found",
        "no such file",
        "syntax error",
        "import error",
        "module not found",
    ]

    # Commands that indicate verification/testing
    VERIFICATION_COMMANDS = [
        "pytest", "python -m pytest",
        "npm test", "yarn test",
        "go test",
        "cargo test",
        "make test",
        "unittest",
    ]

    def __new__(cls, log_dir: str = None):
        """Singleton pattern to ensure single log file."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, log_dir: str = None):
        if self._initialized:
            return

        # Default to _logs in current working directory
        if log_dir is None:
            log_dir = os.path.join(os.getcwd(), "_logs")

        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_dir / "exec_log.jsonl"
        self._initialized = True

    def log_execution(
        self,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_seconds: float,
        timeout: bool = False,
        working_dir: str = None,
        error: str = None,
    ) -> Dict[str, Any]:
        """
        Log a command execution with analysis.

        Returns the log entry (useful for immediate validation).
        """
        timestamp = datetime.now().isoformat()

        # Preview first 500 chars of output
        stdout_preview = stdout[:500] if stdout else ""
        stderr_preview = stderr[:500] if stderr else ""

        # Detect error indicators in output
        combined_output = (stdout + stderr).lower()[:5000]
        error_indicators = [
            indicator for indicator in self.ERROR_INDICATORS
            if indicator in combined_output
        ]

        # Check if this was a verification command
        cmd_lower = command.lower()
        is_verification = any(
            v in cmd_lower for v in self.VERIFICATION_COMMANDS
        )

        entry = {
            "timestamp": timestamp,
            "command": command,
            "exit_code": exit_code,
            "success": exit_code == 0 and not timeout,
            "duration_seconds": round(duration_seconds, 2),
            "stdout_preview": stdout_preview,
            "stderr_preview": stderr_preview,
            "output_size": len(stdout) + len(stderr),
            "timeout": timeout,
            "working_dir": working_dir,
            "error_indicators": error_indicators,
            "is_verification": is_verification,
            "error": error,
        }

        # Append to log file
        try:
            with open(self._log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"⚠️ Failed to write exec log: {e}")

        return entry

    def get_log_path(self) -> Path:
        """Return path to the log file."""
        return self._log_file

    def get_recent_executions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Read recent execution entries from log."""
        entries = []
        try:
            if self._log_file.exists():
                with open(self._log_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entries.append(json.loads(line))
        except Exception as e:
            print(f"⚠️ Failed to read exec log: {e}")

        return entries[-limit:] if limit else entries

    def find_execution(self, command_pattern: str) -> List[Dict[str, Any]]:
        """Find executions matching a command pattern."""
        entries = self.get_recent_executions(limit=0)
        pattern_lower = command_pattern.lower()
        return [
            e for e in entries
            if pattern_lower in e.get("command", "").lower()
        ]

    def get_verification_runs(self) -> List[Dict[str, Any]]:
        """Get all verification/test command executions."""
        entries = self.get_recent_executions(limit=0)
        return [e for e in entries if e.get("is_verification", False)]

    def get_failed_executions(self) -> List[Dict[str, Any]]:
        """Get all failed command executions."""
        entries = self.get_recent_executions(limit=0)
        return [e for e in entries if not e.get("success", True)]

    def clear(self):
        """Clear the execution log (for testing)."""
        if self._log_file.exists():
            self._log_file.unlink()


# Global execution logger instance
_exec_logger: Optional[ExecLogger] = None


def get_exec_logger(log_dir: str = None) -> ExecLogger:
    """Get or create the global execution logger."""
    global _exec_logger
    if _exec_logger is None:
        _exec_logger = ExecLogger(log_dir)
    return _exec_logger


class ShellTool:
    """Execute bash commands with smart timeout and output truncation.

    Token Optimization:
    - Detects verbose commands (install, build, etc.)
    - Truncates output to save tokens
    - Logs full output to _logs/ for debugging
    """

    name = "bash"
    description = "Execute bash commands. Use for running scripts, installing packages, executing Python, etc."

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute"
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 120)",
                "default": 120
            }
        },
        "required": ["command"]
    }

    # Commands that produce verbose output (install logs, build logs, etc.)
    VERBOSE_PATTERNS = [
        "pip install", "pip3 install",
        "npm install", "npm ci", "yarn install", "yarn add", "pnpm install",
        "cargo build", "cargo install",
        "apt-get", "apt install", "brew install",
        "make", "cmake", "ninja",
        "docker build", "docker pull", "docker run",
        "go build", "go get",
        "mvn", "gradle",
        "composer install",
        "bundle install",
    ]

    # Max lines to show for different scenarios
    MAX_LINES_SUCCESS = 20      # Success summary
    MAX_LINES_FAILURE = 40      # Failure details
    MAX_LINES_NORMAL = 200      # Non-verbose commands

    # Image file extensions to detect and display
    IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.webp'}

    def __init__(self, working_dir: str = ".", auto_open_images: bool = True):
        self.working_dir = working_dir
        self._logs_dir = Path(working_dir) / "_logs"
        self.auto_open_images = auto_open_images

    def _is_verbose_command(self, command: str) -> bool:
        """Check if command is known to produce verbose output."""
        cmd_lower = command.lower()
        return any(pattern in cmd_lower for pattern in self.VERBOSE_PATTERNS)

    def _ensure_logs_dir(self) -> Path:
        """Create logs directory if it doesn't exist."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        return self._logs_dir

    def _get_log_path(self, command: str) -> Path:
        """Generate a log file path for a command."""
        cmd_hash = hashlib.md5(command.encode()).hexdigest()[:8]
        # Sanitize command for filename
        cmd_short = command[:30].replace("/", "_").replace(" ", "_")
        return self._ensure_logs_dir() / f"{cmd_short}_{cmd_hash}.log"

    def _get_existing_images(self) -> Set[Path]:
        """Get set of existing image files in working directory."""
        images = set()
        work_path = Path(self.working_dir)
        for ext in self.IMAGE_EXTENSIONS:
            images.update(work_path.glob(f"*{ext}"))
            images.update(work_path.glob(f"**/*{ext}"))  # Also check subdirs
        return images

    def _detect_new_images(self, before: Set[Path]) -> List[Path]:
        """Detect newly created image files."""
        after = self._get_existing_images()
        new_images = after - before
        # Sort by modification time (newest first)
        return sorted(new_images, key=lambda p: p.stat().st_mtime, reverse=True)

    def _open_images(self, images: List[Path]) -> List[str]:
        """Open image files with system viewer. Returns list of opened files."""
        if not images:
            return []

        opened = []
        system = platform.system()

        for img_path in images[:5]:  # Limit to 5 images to avoid spam
            try:
                if system == "Darwin":  # macOS
                    subprocess.run(["open", str(img_path)], check=False)
                elif system == "Linux":
                    # Try common Linux viewers
                    for viewer in ["xdg-open", "eog", "feh", "display"]:
                        try:
                            subprocess.run([viewer, str(img_path)], check=False)
                            break
                        except FileNotFoundError:
                            continue
                elif system == "Windows":
                    os.startfile(str(img_path))

                opened.append(str(img_path))
            except Exception:
                pass  # Silently skip if can't open

        return opened

    def _truncate_output(self, output: str, command: str, success: bool) -> str:
        """Truncate output to save tokens.

        Strategy:
        - Verbose + success: minimal summary
        - ANY failure: save full log, show last N lines for debugging
        - Normal + long: head + tail
        - Normal + short: full output
        """
        if not output:
            return "(no output)"

        lines = output.strip().split('\n')
        total_lines = len(lines)
        is_verbose = self._is_verbose_command(command)

        # Verbose command that succeeded - minimal output
        if is_verbose and success:
            # Save full log to file
            log_path = self._get_log_path(command)
            log_path.write_text(output)

            # Return summary only
            last_few = lines[-3:] if len(lines) >= 3 else lines
            return (
                f"✓ Completed ({total_lines} lines)\n"
                f"Last output: {last_few[-1] if last_few else ''}\n"
                f"Full log: {log_path}"
            )

        # ANY failed command - always save full log and show tail for debugging
        if not success:
            log_path = self._get_log_path(command)
            log_path.write_text(output)

            # Show more lines for failed commands (50 lines to capture full tracebacks)
            tail_lines = 50
            tail = lines[-tail_lines:]
            truncated = len(lines) > tail_lines

            result = []
            if truncated:
                result.append(f"... ({total_lines - tail_lines} lines omitted - see full log) ...")
            result.extend(tail)
            result.append(f"\n[Full log saved: {log_path}]")
            result.append(f"[To see complete output: cat {log_path}]")

            return '\n'.join(result)

        # Normal command - truncate if very long
        if total_lines > self.MAX_LINES_NORMAL:
            head = lines[:20]
            tail = lines[-20:]
            omitted = total_lines - 40

            return '\n'.join(head) + f"\n\n... ({omitted} lines omitted) ...\n\n" + '\n'.join(tail)

        # Normal command, reasonable length - return as-is
        return output

    def _adjust_timeout(self, command: str, base_timeout: int) -> int:
        """Adjust timeout based on command type."""
        cmd_lower = command.lower()

        if any(kw in cmd_lower for kw in ["install", "pip", "npm", "apt", "brew"]):
            return min(300, base_timeout * 5)
        elif any(kw in cmd_lower for kw in ["git clone", "wget", "curl", "download"]):
            return min(180, base_timeout * 3)
        elif any(kw in cmd_lower for kw in ["test", "pytest", "npm test"]):
            return min(300, base_timeout * 5)
        elif any(kw in cmd_lower for kw in ["python", "python3"]):
            return min(600, base_timeout * 5)  # Python scripts may run long

        return base_timeout

    def execute(self, command: str = None, timeout: int = 120) -> ToolResult:
        """Execute a bash command with smart output truncation.

        Token Optimization:
        - Verbose commands (pip, npm, etc.) return minimal output on success
        - Failed commands show last 40 lines for debugging
        - Full logs saved to _logs/ directory

        Visual Output:
        - Detects newly generated image files (png, jpg, svg, pdf, etc.)
        - Automatically opens them for viewing

        Execution Logging:
        - All executions logged to _logs/exec_log.jsonl for audit trail
        - Creates external evidence that cannot be fabricated
        """
        if not command or not command.strip():
            return ToolResult(
                success=False,
                output=None,
                error="No command provided. The 'command' argument is required."
            )

        timeout = self._adjust_timeout(command, timeout)
        logger = get_exec_logger(str(self._logs_dir))
        start_time = time.time()

        # Capture existing images before running command
        images_before = self._get_existing_images() if self.auto_open_images else set()

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.working_dir
            )

            duration = time.time() - start_time

            # Combine stdout and stderr
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}" if output else result.stderr

            success = result.returncode == 0

            # Log execution for audit trail (external evidence)
            logger.log_execution(
                command=command,
                exit_code=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                duration_seconds=duration,
                timeout=False,
                working_dir=self.working_dir,
                error=None if success else f"Exit code: {result.returncode}",
            )

            # Truncate output to save tokens
            truncated_output = self._truncate_output(output, command, success)

            # Detect and open any newly created images
            opened_images = []
            if self.auto_open_images and success:
                new_images = self._detect_new_images(images_before)
                if new_images:
                    opened_images = self._open_images(new_images)
                    if opened_images:
                        image_list = "\n".join(f"  📊 {img}" for img in opened_images)
                        truncated_output += f"\n\n[Visual Output - Opened {len(opened_images)} image(s)]\n{image_list}"

            # Build informative error message for failures
            error_msg = None
            if not success:
                error_msg = f"Exit code: {result.returncode}"
                # Include first meaningful line of stderr in error for quick diagnosis
                if result.stderr:
                    stderr_lines = [l.strip() for l in result.stderr.strip().split('\n') if l.strip()]
                    if stderr_lines:
                        # Get first error line (skip empty lines, common prefixes)
                        first_error = stderr_lines[0][:200]
                        error_msg = f"Exit code: {result.returncode}. Error: {first_error}"

            return ToolResult(
                success=success,
                output=truncated_output,
                error=error_msg
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            # Log timeout for audit trail
            logger.log_execution(
                command=command,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=duration,
                timeout=True,
                working_dir=self.working_dir,
                error=f"Command timed out after {timeout}s",
            )
            return ToolResult(success=False, output=None, error=f"Command timed out after {timeout}s")
        except Exception as e:
            duration = time.time() - start_time
            # Log exception for audit trail
            logger.log_execution(
                command=command,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=duration,
                timeout=False,
                working_dir=self.working_dir,
                error=str(e),
            )
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool(working_dir: str = ".", auto_open_images: bool = True) -> ShellTool:
    """Factory function for tool discovery.

    Args:
        working_dir: Directory to execute commands in
        auto_open_images: If True, automatically open generated images (default: True)
    """
    return ShellTool(working_dir, auto_open_images=auto_open_images)
