"""
Sub-agent spawning tool.

This tool launches specialised sub‑agents to tackle complex
subtasks. It mirrors the behaviour of the original ``task_agent``
functionality by instantiating a new ``SCIAgent`` with
limited capabilities and delegating the provided prompt. When
running, the tool respects maximum sub‑agent limits and filters
available tools based on the requested ``agent_type``.
"""

from __future__ import annotations

import datetime
import time
from typing import Dict, Any, Optional, List

from sciagent.base_tool import BaseTool

try:
    # Import here to avoid circular dependencies at module import time
    from sciagent.config import Config  # type: ignore  # noqa: F401
    from sciagent.agent import SCIAgent  # type: ignore  # noqa: F401
except Exception:
    Config = None  # type: ignore
    SCIAgent = None  # type: ignore


class TaskAgentTool(BaseTool):
    """Launch specialized sub‑agents for complex analysis or tasks."""

    name = "task_agent"
    description = "Launch specialized sub-agents for complex analysis or tasks"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Brief description of the task"},
            "prompt": {"type": "string", "description": "Detailed prompt for the sub-agent"},
            "agent_type": {
                "type": "string",
                "enum": ["search", "analysis", "coding", "debugging", "general"],
                "description": "Type of specialized agent",
                "default": "general",
            },
        },
        "required": ["description", "prompt"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        if agent is None or SCIAgent is None or Config is None:
            return {"success": False, "error": "Sub-agent spawning requires a running agent context"}
        try:
            description = tool_input.get("description", "")
            prompt = tool_input.get("prompt", "")
            agent_type = tool_input.get("agent_type", "general")
            # Enforce sub-agent limit
            if hasattr(agent, "active_sub_agents") and hasattr(agent, "config"):
                max_sub_agents = getattr(agent.config, "max_sub_agents", 1)
                if agent.active_sub_agents >= max_sub_agents:
                    return {
                        "success": False,
                        "error": f"Maximum sub-agents ({max_sub_agents}) already active",
                    }
            else:
                return {
                    "success": False,
                    "error": "Agent context missing sub-agent tracking information",
                }
            # Increment counter
            agent.active_sub_agents += 1
            try:
                # Configure sub-agent
                sub_config = Config(
                    api_key=agent.config.api_key,
                    model=agent.config.model,
                    working_dir=agent.config.working_dir,
                    max_iterations=8,
                    debug_mode=False,
                    enable_web=agent_type in ["research", "general"],
                    enable_notebooks=agent_type in ["analysis", "general"],
                )
                sub_agent = SCIAgent(
                    config=sub_config,
                    progress_callback=getattr(agent, "_handle_sub_agent_progress", None),
                    indent_level=getattr(agent, "indent_level", 0) + 1,
                )
                # Filter tools based on type
                allowed_names: List[str]
                if agent_type == "search":
                    allowed_names = ["glob_search", "grep_search", "list_directory", "str_replace_editor"]
                elif agent_type == "analysis":
                    allowed_names = ["str_replace_editor", "grep_search", "bash", "create_summary"]
                elif agent_type == "coding":
                    allowed_names = ["str_replace_editor", "bash", "glob_search", "grep_search"]
                elif agent_type == "debugging":
                    allowed_names = ["str_replace_editor", "bash", "grep_search", "list_directory"]
                else:
                    # general: limited default subset
                    allowed_names = [name for name in agent.registry.tools.keys()][:6]
                # Update sub-agent registry to only include allowed tools
                # Keep the existing tool instances but filter by name
                sub_agent.registry._tools = {name: t for name, t in sub_agent.registry.tools.items() if name in allowed_names}
                # Update the tool schemas and LLM definitions
                sub_agent.tools = sub_agent.registry.get_tool_schemas()
                sub_agent.llm_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": t["input_schema"],
                        },
                    }
                    for t in sub_agent.tools
                ]
                # Execute the sub-task
                start_time = time.time()
                try:
                    result = sub_agent.execute_task(prompt)
                    elapsed = time.time() - start_time
                except Exception as e:
                    elapsed = time.time() - start_time
                    raise
                # Build summary object
                sub_result = {
                    "agent_type": agent_type,
                    "description": description,
                    "success": result.get("success", False),
                    "iterations": result.get("iterations", 0),
                    "response": result.get("final_response", ""),
                    "timestamp": datetime.datetime.now().isoformat(),
                }
                if hasattr(agent.state, "sub_agent_results"):
                    agent.state.sub_agent_results.append(sub_result)
                return {
                    "success": result.get("success", False),
                    "output": (
                        f"Sub-agent ({agent_type}) completed in {result.get('iterations', 0)} iterations:\n"
                        + str(result.get("final_response", "No response"))[:500]
                        + ("..." if len(str(result.get("final_response", ""))) > 500 else "")
                    ),
                    "sub_agent_result": sub_result,
                }
            finally:
                agent.active_sub_agents -= 1
        except Exception as e:
            agent.active_sub_agents -= 1
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return TaskAgentTool()