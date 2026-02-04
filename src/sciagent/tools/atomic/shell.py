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

import subprocess
import os
import hashlib
import platform
import glob
from pathlib import Path
from typing import Dict, Any, Optional, Set, List
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

        Visual Output:
        - Detects newly generated image files (png, jpg, svg, pdf, etc.)
        - Automatically opens them for viewing
        """
        if not command or not command.strip():
            return ToolResult(
                success=False,
                output=None,
                error="No command provided. The 'command' argument is required."
            )

        timeout = self._adjust_timeout(command, timeout)

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

            # Combine stdout and stderr
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}" if output else result.stderr

            success = result.returncode == 0

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


def get_tool(working_dir: str = ".", auto_open_images: bool = True) -> ShellTool:
    """Factory function for tool discovery.

    Args:
        working_dir: Directory to execute commands in
        auto_open_images: If True, automatically open generated images (default: True)
    """
    return ShellTool(working_dir, auto_open_images=auto_open_images)
