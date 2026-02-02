"""
User interaction tool.

Represents a request for human guidance on a particular step. The
tool itself simply acknowledges the request; it is up to the
calling agent to implement prompting the end user and feeding
their response back into the conversation. This separation
allows the agent to reason about when to solicit human input
without tying the behaviour to the tool implementation.
"""

from __future__ import annotations

from typing import Dict, Any, Optional

from sciagent.base_tool import BaseTool


class AskUserStepTool(BaseTool):
    """Ask user for guidance on specific step completion or error recovery."""

    name = "ask_user_step"
    description = "Ask user for guidance on specific step completion or error recovery"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "step_description": {"type": "string", "description": "What step was attempted"},
            "status": {
                "type": "string",
                "enum": ["completed", "failed", "needs_guidance"],
                "description": "Status of the step",
            },
            "error_details": {"type": "string", "description": "Error details if step failed"},
            "suggested_next_action": {"type": "string", "description": "Suggested recovery action"},
        },
        "required": ["step_description", "status"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        # Simply acknowledge the need for user input; actual prompting is handled by the agent
        return {"success": True, "output": "User guidance requested"}


def get_tool() -> BaseTool:
    return AskUserStepTool()