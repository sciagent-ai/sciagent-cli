"""
Ask User tool - Request user input during agent execution.

Use this tool when:
- Choosing between simulation services/approaches
- Confirming expensive computation parameters
- Clarifying ambiguous scientific requirements
- Getting user preference on trade-offs

Do NOT use for:
- Trivial decisions you can make yourself
- Things you can verify by reading files/docs
- Every step of execution (stay autonomous)
"""

from __future__ import annotations

from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AskUserTool:
    """Request user input during agent execution."""

    name = "ask_user"
    description = """Ask the user a question and wait for their response.

USE THIS TOOL FOR:
- Choosing between services (e.g., "Should I use MEEP or RCWA for this simulation?")
- Confirming expensive simulation parameters (e.g., "Run 10ns or 100ns MD simulation?")
- Clarifying ambiguous requirements (e.g., "Which convergence criterion: energy or force?")
- Trade-off decisions (e.g., "Faster with coarse mesh or accurate with fine mesh?")

DO NOT USE FOR:
- Decisions you can make yourself based on context
- Things you can verify by reading files or documentation
- Routine progress updates (just proceed autonomously)
- Every step of a workflow (only ask when genuinely uncertain)

The tool will pause execution and display your question to the user.
Their response will be returned to you to continue the task."""

    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user. Be specific and provide context."
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of choices to present (e.g., ['MEEP', 'RCWA', 'Both']). If not provided, user gives free-form response."
            },
            "context": {
                "type": "string",
                "description": "Optional additional context to help user decide (e.g., trade-offs, implications of each choice)."
            },
            "default": {
                "type": "string",
                "description": "Optional default choice if user just presses Enter."
            }
        },
        "required": ["question"]
    }

    # Marker that agent loop checks to trigger user input
    REQUIRES_USER_INPUT = True

    def __init__(self):
        pass

    def execute(
        self,
        question: str,
        options: List[str] = None,
        context: str = None,
        default: str = None
    ) -> ToolResult:
        """
        Validate the question and return a structured request for user input.

        The actual user prompting is handled by the agent loop, not here.
        This tool just validates and structures the request.
        """
        # Validate question
        if not question or not question.strip():
            return ToolResult(
                success=False,
                output=None,
                error="Question cannot be empty"
            )

        # Build the request structure
        request = {
            "question": question.strip(),
            "awaiting_user_input": True,  # Signal to agent loop
        }

        if options:
            # Validate options
            if len(options) < 2:
                return ToolResult(
                    success=False,
                    output=None,
                    error="If providing options, must have at least 2 choices"
                )
            request["options"] = options

        if context:
            request["context"] = context.strip()

        if default:
            # Validate default is in options if options provided
            if options and default not in options:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Default '{default}' must be one of the options: {options}"
                )
            request["default"] = default

        return ToolResult(
            success=True,
            output=request,
            metadata={"requires_user_input": True}
        )

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool() -> AskUserTool:
    """Factory function for tool discovery."""
    return AskUserTool()
