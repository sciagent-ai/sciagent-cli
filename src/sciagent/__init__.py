"""
SWE Agent - A modular Software Engineering Agent Framework

Quick Start:
    from swe_agent import create_agent, run_task
    
    # One-shot task
    result = run_task("Create a hello world script")
    
    # Interactive agent
    agent = create_agent()
    agent.run_interactive()
    
    # With sub-agents
    from swe_agent import create_agent_with_subagents
    agent = create_agent_with_subagents()
    agent.run("Research this codebase and write tests")
"""

from .llm import LLMClient, Message, LLMResponse, ask
from .tools import (
    BaseTool, FunctionTool, ToolRegistry, ToolResult,
    create_default_registry, tool
)
from .state import (
    AgentState, ContextWindow, TodoList, TodoItem,
    StateManager, generate_session_id
)
from .agent import AgentLoop, AgentConfig, create_agent, run_task
from .subagent import (
    SubAgent, SubAgentConfig, SubAgentResult,
    SubAgentRegistry, SubAgentOrchestrator,
    create_agent_with_subagents
)

__version__ = "0.1.0"
__all__ = [
    # LLM
    "LLMClient", "Message", "LLMResponse", "ask",
    # Tools
    "BaseTool", "FunctionTool", "ToolRegistry", "ToolResult",
    "create_default_registry", "tool",
    # State
    "AgentState", "ContextWindow", "TodoList", "TodoItem",
    "StateManager", "generate_session_id",
    # Agent
    "AgentLoop", "AgentConfig", "create_agent", "run_task",
    # Sub-agents
    "SubAgent", "SubAgentConfig", "SubAgentResult",
    "SubAgentRegistry", "SubAgentOrchestrator",
    "create_agent_with_subagents",
]
