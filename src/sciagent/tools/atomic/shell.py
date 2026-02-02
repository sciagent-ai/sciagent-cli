"""
Shell execution tool.

Execute bash commands with smart timeout handling and output truncation.

Token Optimization:
- Verbose commands (pip, npm, cargo, etc.) have output truncated
- Success: show summary only
- Failure: show last 40 lines + error
- Full logs saved to _logs/ directory
"""

from __future__ import annotations

import subprocess
import os
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


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
        "docker build", "docker pull",
        "go build", "go get",
        "mvn", "gradle",
        "composer install",
        "bundle install",
    ]

    # Max lines to show for different scenarios
    MAX_LINES_SUCCESS = 20      # Success summary
    MAX_LINES_FAILURE = 40      # Failure details
    MAX_LINES_NORMAL = 200      # Non-verbose commands

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir
        self._logs_dir = Path(working_dir) / "_logs"

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

    def _truncate_output(self, output: str, command: str, success: bool) -> str:
        """Truncate output to save tokens.

        Strategy:
        - Verbose + success: minimal summary
        - Verbose + failure: last N lines with context
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

        # Verbose command that failed - show tail for debugging
        if is_verbose and not success:
            log_path = self._get_log_path(command)
            log_path.write_text(output)

            tail = lines[-self.MAX_LINES_FAILURE:]
            truncated = len(lines) > self.MAX_LINES_FAILURE

            result = []
            if truncated:
                result.append(f"... ({total_lines - self.MAX_LINES_FAILURE} lines omitted) ...")
            result.extend(tail)
            result.append(f"\nFull log: {log_path}")

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
        """
        if not command or not command.strip():
            return ToolResult(
                success=False,
                output=None,
                error="No command provided. The 'command' argument is required."
            )

        timeout = self._adjust_timeout(command, timeout)

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.working_dir
            )

            # Combine stdout and stderr
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}" if output else result.stderr

            success = result.returncode == 0

            # Truncate output to save tokens
            truncated_output = self._truncate_output(output, command, success)

            return ToolResult(
                success=success,
                output=truncated_output,
                error=None if success else f"Exit code: {result.returncode}"
            )

        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output=None, error=f"Command timed out after {timeout}s")
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool(working_dir: str = ".") -> ShellTool:
    """Factory function for tool discovery."""
    return ShellTool(working_dir)
