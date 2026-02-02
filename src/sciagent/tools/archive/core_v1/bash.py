"""
Shell execution tool.

This module defines a tool that executes arbitrary shell
commands. It includes heuristics for adjusting timeouts based on
the type of command (e.g. installing packages, cloning
repositories, running tests) and captures both stdout and stderr.
When invoked from an agent it will update the agent's state to
record the last successful operation and scan for newly created
files.
"""

from __future__ import annotations

import subprocess
import os
from typing import Dict, Any, Optional

from sciagent.base_tool import BaseTool


class BashTool(BaseTool):
    """Execute bash commands with smart timeout and error recovery."""

    name = "bash"
    description = "Execute bash commands with smart timeout and error recovery"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The bash command to execute"},
            "timeout": {
                "type": "number",
                "description": "Command timeout in seconds",
                "default": 30,
            },
        },
        "required": ["command"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        # Validate required command parameter
        if not tool_input or "command" not in tool_input:
            return {
                "success": False,
                "error": "Missing required 'command' parameter in bash tool input",
                "provided_input": str(tool_input)
            }
        
        command = tool_input.get("command", "")
        if not command.strip():
            return {
                "success": False,
                "error": "Empty command provided to bash tool"
            }
        
        timeout = tool_input.get("timeout", 30)
        # Adjust timeout heuristically for long-running commands
        cmd_lower = command.lower()
        if any(keyword in cmd_lower for keyword in ["install", "pip", "npm", "apt", "yum", "brew"]):
            timeout = min(300, timeout * 10)
        elif any(keyword in cmd_lower for keyword in ["git clone", "wget", "curl", "download"]):
            timeout = min(180, timeout * 6)
        elif any(keyword in cmd_lower for keyword in ["test", "pytest", "npm test", "make test"]):
            timeout = min(120, timeout * 4)
        try:
            # Execute the shell command
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=os.getcwd(),
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            success = result.returncode == 0
            # Record last successful operation and scan for new files
            if success and agent is not None:
                try:
                    agent.state.last_successful_operation = f"bash: {command[:50]}..."
                    # Scan for newly created files if supported
                    if hasattr(agent, "_scan_for_new_files"):
                        agent._scan_for_new_files(command)  # type: ignore[attr-defined]
                except Exception:
                    pass
            # Classify the command type using agent helper if available
            command_type = "general"
            if agent is not None and hasattr(agent, "_classify_command"):
                try:
                    command_type = agent._classify_command(command)  # type: ignore[attr-defined]
                except Exception:
                    command_type = "general"
            return {
                "success": success,
                "output": output or "(No output)",
                "returncode": result.returncode,
                "command_type": command_type,
                "timeout_used": timeout,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    """Return an instance of :class:`BashTool` for registry discovery."""
    return BashTool()