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
# Suppress pydantic serialization warnings BEFORE any imports
# These occur when litellm's pydantic models serialize LLM responses
# with fields that don't match schema (e.g., thinking_blocks for Claude)
import warnings

# Filter by message content - catches the actual warning text
warnings.filterwarnings("ignore", message=".*Pydantic serializer warnings.*")
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
warnings.filterwarnings("ignore", message=".*Expected.*fields but got.*")
warnings.filterwarnings("ignore", message=".*serialized value may not be as expected.*")

# Filter by category and module - broad catch for pydantic warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.*")

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
from .provenance import (
    ProvenanceChecker, ProvenanceResult, ProvenanceIssue,
    check_provenance
)
from .orchestrator import (
    TaskOrchestrator, OrchestratorConfig, ExecutionResult,
    WorkflowBuilder, create_orchestrator
)
from .defaults import DEFAULT_MODEL

__version__ = "0.1.0"
__all__ = [
    # Config
    "DEFAULT_MODEL",
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
    # Provenance & Data Validation
    "ProvenanceChecker", "ProvenanceResult", "ProvenanceIssue",
    "check_provenance",
    # Orchestrator
    "TaskOrchestrator", "OrchestratorConfig", "ExecutionResult",
    "WorkflowBuilder", "create_orchestrator",
]
