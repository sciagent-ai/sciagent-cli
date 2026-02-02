"""
Intelligent summarisation tool.

Generates a summary of recent progress using key accomplishments,
current focus and next steps. It records the summary into the
agent's conversation history if an agent context is provided.
This mirrors the behaviour of the agent's ``create_summary``
implementation.
"""

from __future__ import annotations

import datetime
from typing import Dict, Any, Optional, List

from sciagent.base_tool import BaseTool

try:
    from sciagent.state import ConversationSummary  # type: ignore
except Exception:
    ConversationSummary = None  # type: ignore


class CreateSummaryTool(BaseTool):
    """Create intelligent summary of recent progress."""

    name = "create_summary"
    description = "Create intelligent summary of recent progress"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why creating summary"},
            "key_accomplishments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Major things accomplished",
            },
            "current_focus": {"type": "string", "description": "What we're currently working on"},
            "next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Planned next steps",
            },
        },
        "required": ["reason", "key_accomplishments", "current_focus"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        if agent is None or ConversationSummary is None:
            return {"success": False, "error": "Summarisation requires an agent context"}
        try:
            reason = tool_input.get("reason", "")
            key_accomplishments = tool_input.get("key_accomplishments", [])
            current_focus = tool_input.get("current_focus", "")
            next_steps = tool_input.get("next_steps", [])
            # Determine summary id
            summary_id = f"professional_{len(agent.state.conversation_summaries) + 1}"
            conversation_summary = ConversationSummary(
                summary_id=summary_id,
                iterations_covered=(max(0, agent.state.iteration_count - 8), agent.state.iteration_count),
                key_accomplishments=key_accomplishments,
                current_focus=current_focus,
                next_steps=next_steps,
                files_created_modified=list(agent.state.files_tracking.keys()),
                errors_resolved=[err.get("error", "")[:50] for err in agent.state.error_history[-3:]],
                timestamp=datetime.datetime.now().isoformat(),
            )
            agent.state.conversation_summaries.append(conversation_summary)
            return {
                "success": True,
                "output": (
                    f"📋 Comprehensive Summary Created\nReason: {reason}\n"
                    f"Accomplishments: {len(key_accomplishments)}\n"
                    f"Files tracked: {len(agent.state.files_tracking)}\n"
                    f"Sub-agents used: {len(agent.state.sub_agent_results)}"
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return CreateSummaryTool()