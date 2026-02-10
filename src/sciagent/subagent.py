"""
Sub-Agent System - Spawn and manage isolated agent instances

Key principles:
- Each sub-agent has its own context window (isolation)
- Sub-agents cannot spawn other sub-agents (no recursion)
- Communication happens through return values only
- Parent only sees results, not intermediate reasoning
"""
import os
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from datetime import datetime

from .llm import LLMClient, Message
from .tools import ToolRegistry, BaseTool, ToolResult, create_default_registry
from .state import ContextWindow, generate_session_id
from .agent import AgentLoop, AgentConfig
from .defaults import DEFAULT_MODEL


@dataclass
class SubAgentConfig:
    """Configuration for a sub-agent"""
    name: str
    description: str
    system_prompt: str
    model: str = DEFAULT_MODEL
    max_iterations: int = 20
    allowed_tools: Optional[List[str]] = None  # None = all tools
    temperature: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "max_iterations": self.max_iterations,
            "allowed_tools": self.allowed_tools,
            "temperature": self.temperature
        }


@dataclass
class SubAgentResult:
    """Result from a sub-agent execution"""
    agent_name: str
    task: str
    success: bool
    output: str
    error: Optional[str] = None
    iterations: int = 0
    tokens_used: int = 0
    duration_seconds: float = 0.0
    session_id: Optional[str] = None  # For resumption
    
    def to_dict(self) -> Dict:
        return {
            "agent_name": self.agent_name,
            "task": self.task,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "iterations": self.iterations,
            "tokens_used": self.tokens_used,
            "duration_seconds": self.duration_seconds,
            "session_id": self.session_id
        }


class SubAgent:
    """
    An isolated agent instance with its own context
    
    Sub-agents:
    - Have their own system prompt
    - Have restricted tool access (optional)
    - Cannot spawn further sub-agents
    - Return only their final result to parent
    """
    
    def __init__(
        self,
        config: SubAgentConfig,
        tools: Optional[ToolRegistry] = None,
        working_dir: str = ".",
        is_nested: bool = False  # True if spawned by another agent
    ):
        self.config = config
        self.working_dir = working_dir
        self.is_nested = is_nested
        
        # Create filtered tool registry if restrictions specified
        if tools and config.allowed_tools is not None:
            self.tools = ToolRegistry()
            for tool_name in config.allowed_tools:
                tool = tools.get(tool_name)
                if tool:
                    self.tools.register(tool)
        else:
            self.tools = tools or create_default_registry(working_dir)
        
        # Remove Task tool to prevent recursive spawning
        if self.is_nested:
            self.tools.unregister("task")
            self.tools.unregister("spawn_agent")
        
        # Create the underlying agent
        agent_config = AgentConfig(
            model=config.model,
            temperature=config.temperature,
            max_iterations=config.max_iterations,
            working_dir=working_dir,
            verbose=False,  # Keep output clean
            auto_save=False
        )
        
        self.agent = AgentLoop(
            config=agent_config,
            tools=self.tools,
            system_prompt=config.system_prompt
        )
        
        self.session_id = self.agent.state.session_id
    
    def run(self, task: str) -> SubAgentResult:
        """Execute a task and return the result"""
        import time
        start_time = time.time()
        
        try:
            output = self.agent.run(task)
            success = True
            error = None
        except Exception as e:
            output = ""
            success = False
            error = str(e)
        
        duration = time.time() - start_time
        
        return SubAgentResult(
            agent_name=self.config.name,
            task=task,
            success=success,
            output=output,
            error=error,
            iterations=self.agent.iteration_count,
            tokens_used=self.agent.total_tokens,
            duration_seconds=duration,
            session_id=self.session_id
        )


class SubAgentRegistry:
    """Registry of available sub-agent configurations.

    Simplified to 3 core agents following Claude Code's pattern:
    - explore: Fast, read-only - for codebase/log exploration
    - plan: Inherit model, read-only - for breaking down problems
    - general: Inherit model, all tools - for complex multi-step tasks
    """

    def __init__(self):
        self._configs: Dict[str, SubAgentConfig] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register built-in sub-agent types.

        Model selection:
        - explore: FAST_MODEL (Haiku) - just reading files, quick searches
        - debug: CODING_MODEL (Sonnet) - error tracing, log reading
        - research: CODING_MODEL (Sonnet) - web search, doc reading
        - plan: SCIENTIFIC_MODEL (Opus) - architecture needs deep reasoning
        - general: CODING_MODEL (Sonnet) - implementation tasks
        """
        from .defaults import FAST_MODEL, CODING_MODEL, SCIENTIFIC_MODEL

        # Explore agent - fast, read-only, for quick codebase searches
        # Uses FAST_MODEL (Haiku) for speed and cost efficiency
        self.register(SubAgentConfig(
            name="explore",
            description="Fast codebase exploration. Use for quick searches and file lookups.",
            model=FAST_MODEL,
            system_prompt="""You are a fast exploration agent. Quickly find and report information.

## What You Do
- Search for files and patterns
- Read files and summarize
- List directory contents
- Find relevant code

## Output
Be concise:
1. **Found**: What you found
2. **Location**: File paths and line numbers

Do NOT make changes. Only explore and report.""",
            allowed_tools=["file_ops", "search", "bash"],
            max_iterations=15
        ))

        # Debug agent - capable, read-only, for error investigation
        # Uses CODING_MODEL (Sonnet) - good enough for tracing errors
        # Has web access to research error solutions
        self.register(SubAgentConfig(
            name="debug",
            description="Investigate errors, trace root causes, research solutions. Use when fixing errors.",
            model=CODING_MODEL,
            system_prompt="""You are a debugging agent. Thoroughly investigate errors and find solutions.

## What You Do
- Read error logs completely
- Trace errors to their source
- Identify root causes
- **Search online for solutions** when needed
- Suggest specific fixes

## Process
1. Read the full error/log file
2. Identify the actual error (not just symptoms)
3. Trace back to the source
4. If unfamiliar error: web(command="search", query="{package} {error message}")
5. Report root cause and fix

## Output
1. **Error**: What went wrong
2. **Root Cause**: Why it happened
3. **Location**: File and line number
4. **Fix**: How to resolve it (with code if applicable)
5. **Source**: URL if you researched online

Do NOT make changes. Only investigate and report.""",
            allowed_tools=["file_ops", "search", "bash", "web", "skill"],
            max_iterations=30
        ))

        # Research agent - for web-based research and documentation
        # Uses CODING_MODEL (Sonnet) - sufficient for web search and reading docs
        self.register(SubAgentConfig(
            name="research",
            description="Web research, documentation lookup, literature review. Use for external knowledge.",
            model=CODING_MODEL,
            system_prompt="""You are a research agent. Find and synthesize information from the web.

## What You Do
- Search for documentation, tutorials, examples
- Find scientific papers and methods
- Look up API references and best practices
- Research libraries and their usage patterns

## Process
1. Search with specific queries: web(command="search", query="...")
2. Fetch promising sources: web(command="fetch", url="...")
3. Extract key information
4. Save findings to _outputs/ if substantial

## Output Format
1. **Finding**: What you learned
2. **Source**: URL or citation
3. **Details**: Key facts, code examples, parameters
4. **Recommendation**: How to apply this

Always cite sources. Do NOT fabricate information.""",
            allowed_tools=["web", "file_ops", "search"],
            max_iterations=20
        ))

        # Plan agent - for breaking down complex problems
        # Uses SCIENTIFIC_MODEL (Opus) - architecture needs deep reasoning
        self.register(SubAgentConfig(
            name="plan",
            description="Break down complex tasks into steps. Use before implementing anything non-trivial.",
            model=SCIENTIFIC_MODEL,
            system_prompt="""You are a planning agent. Analyze problems and create actionable plans.

## Process
1. Understand the goal
2. Explore what exists (use tools to read code/docs)
3. Identify concrete steps
4. Order by dependencies
5. Output clear plan

## Output Format
```
## Goal
<one sentence>

## Steps
1. [id] Description
   - What to do
   - Expected outcome

2. [id] Description (depends on: 1)
   - What to do
   - Expected outcome

## Notes
- Risks or considerations
```

Do NOT execute. Only plan.""",
            allowed_tools=["file_ops", "search", "bash", "web", "skill", "todo"],
            max_iterations=15
        ))

        # General agent - full capability for complex tasks
        # Uses CODING_MODEL (Sonnet) - good for implementation tasks
        self.register(SubAgentConfig(
            name="general",
            description="Complex multi-step tasks requiring exploration AND action.",
            model=CODING_MODEL,
            system_prompt="""You are a capable agent for complex tasks.

Think step by step:
1. Understand what's needed
2. Explore to gather context
3. Execute the task
4. Verify the result

Use all available tools as needed.""",
            max_iterations=50
        ))
    
    def register(self, config: SubAgentConfig):
        """Register a sub-agent configuration"""
        self._configs[config.name] = config
    
    def get(self, name: str) -> Optional[SubAgentConfig]:
        """Get a sub-agent config by name"""
        return self._configs.get(name)
    
    def list_agents(self) -> List[Dict]:
        """List all available sub-agent types"""
        return [
            {"name": c.name, "description": c.description}
            for c in self._configs.values()
        ]


class SubAgentOrchestrator:
    """
    Orchestrates sub-agent spawning and execution
    
    Provides:
    - Sequential execution
    - Parallel execution
    - Result aggregation
    """
    
    def __init__(
        self,
        tools: Optional[ToolRegistry] = None,
        working_dir: str = ".",
        max_workers: int = 4
    ):
        self.tools = tools or create_default_registry(working_dir)
        self.working_dir = working_dir
        self.max_workers = max_workers
        self.registry = SubAgentRegistry()
        
        # Track active sub-agents
        self._active: Dict[str, SubAgent] = {}
        self._results: List[SubAgentResult] = []
    
    def spawn(
        self,
        agent_name: str,
        task: str,
        custom_config: Optional[SubAgentConfig] = None
    ) -> SubAgentResult:
        """
        Spawn and run a sub-agent

        Args:
            agent_name: Name of registered agent type
            task: Task to execute
            custom_config: Optional custom configuration

        Returns:
            SubAgentResult with output
        """
        config = custom_config or self.registry.get(agent_name)

        if not config:
            return SubAgentResult(
                agent_name=agent_name,
                task=task,
                success=False,
                output="",
                error=f"Unknown agent type: {agent_name}. Available: {[a['name'] for a in self.registry.list_agents()]}"
            )
        
        # Create and run the sub-agent
        sub_agent = SubAgent(
            config=config,
            tools=self.tools,
            working_dir=self.working_dir,
            is_nested=True
        )
        
        result = sub_agent.run(task)
        self._results.append(result)
        self._active[result.session_id] = sub_agent
        
        return result
    
    def spawn_parallel(
        self,
        tasks: List[Dict[str, str]]
    ) -> List[SubAgentResult]:
        """
        Spawn multiple sub-agents in parallel
        
        Args:
            tasks: List of {"agent_name": str, "task": str}
            
        Returns:
            List of results (in completion order)
        """
        results = []
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self.spawn,
                    t["agent_name"],
                    t["task"]
                ): t for t in tasks
            }
            
            for future in as_completed(futures):
                task_info = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    results.append(SubAgentResult(
                        agent_name=task_info["agent_name"],
                        task=task_info["task"],
                        success=False,
                        output="",
                        error=str(e)
                    ))
        
        return results
    
    def resume(self, session_id: str, task: str) -> Optional[SubAgentResult]:
        """Resume a previous sub-agent session"""
        sub_agent = self._active.get(session_id)
        if not sub_agent:
            return None
        
        return sub_agent.run(task)
    
    def get_history(self) -> List[Dict]:
        """Get history of all sub-agent executions"""
        return [r.to_dict() for r in self._results]


# =============================================================================
# Task Tool - Allows parent agent to spawn sub-agents
# =============================================================================

class TaskTool(BaseTool):
    """Tool that allows the agent to spawn sub-agents"""

    name = "task"
    description = """Delegate a task to a specialized sub-agent.

Available agents:
- explore: Fast codebase search (uses Haiku). Quick file/pattern lookups.
- debug: Error investigation with web research. Use when fixing errors.
- research: Web research, documentation, literature review. Use for external knowledge.
- plan: Break down complex problems into steps.
- general: Complex multi-step tasks requiring both exploration AND action.

Use 'explore' for quick local searches.
Use 'debug' when investigating errors.
Use 'research' for documentation, APIs, scientific methods.
Use 'plan' before implementing anything non-trivial.
Use 'general' for complex tasks that need to make changes."""

    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the sub-agent to use",
                "enum": ["explore", "debug", "research", "plan", "general"]
            },
            "task": {
                "type": "string",
                "description": "The task for the sub-agent to complete"
            }
        },
        "required": ["agent_name", "task"]
    }
    
    def __init__(self, orchestrator: SubAgentOrchestrator):
        self.orchestrator = orchestrator
    
    def execute(self, agent_name: str, task: str) -> ToolResult:
        result = self.orchestrator.spawn(agent_name, task)
        
        if result.success:
            return ToolResult(
                success=True,
                output=f"[Sub-agent '{agent_name}' completed in {result.iterations} iterations]\n\n{result.output}"
            )
        else:
            return ToolResult(
                success=False,
                output=None,
                error=f"Sub-agent failed: {result.error}"
            )


def create_agent_with_subagents(
    model: str = DEFAULT_MODEL,
    working_dir: str = ".",
    verbose: bool = True
) -> AgentLoop:
    """
    Create an agent with sub-agent spawning capability

    Example:
        agent = create_agent_with_subagents()
        agent.run("Research this codebase, then write tests for the main module")
    """
    # Create tools with sub-agent support
    tools = create_default_registry(working_dir)
    orchestrator = SubAgentOrchestrator(tools=tools, working_dir=working_dir)
    tools.register(TaskTool(orchestrator))
    
    config = AgentConfig(
        model=model,
        working_dir=working_dir,
        verbose=verbose
    )
    
    system_prompt = """You are an expert software engineering agent with the ability to delegate tasks to specialized sub-agents.

## Available Sub-Agents
Use the `task` tool to delegate work:
- **researcher**: For exploring and understanding code (read-only)
- **reviewer**: For code review and finding issues
- **test_writer**: For writing tests
- **general**: For complex multi-step tasks

## When to Use Sub-Agents
- Use sub-agents for tasks that benefit from fresh context
- Use sub-agents for parallel exploration
- Keep your main context clean by delegating research

## Guidelines
1. Break complex tasks into subtasks
2. Delegate research and exploration to sub-agents
3. Use results from sub-agents to inform your decisions
4. Maintain overall task coordination

Current working directory: {working_dir}
""".format(working_dir=os.path.abspath(working_dir))

    return AgentLoop(config=config, tools=tools, system_prompt=system_prompt)


# =============================================================================
# Workflow Tool - Execute dependency-aware task workflows
# =============================================================================

class WorkflowTool(BaseTool):
    """Tool for executing dependency-aware task workflows."""

    name = "workflow"
    description = """Execute a workflow of tasks with dependencies.

Define tasks with:
- id: Unique task identifier
- content: Task description
- task_type: research, code, validate, review, general
- depends_on: List of task IDs this depends on
- result_key: Key for storing output (used by dependent tasks)
- can_parallel: Whether this can run with sibling tasks

The orchestrator will:
1. Resolve dependencies
2. Run independent tasks in parallel
3. Pass results to dependent tasks
4. Track progress and errors

Example workflow:
[
  {"id": "research", "content": "Research API patterns", "task_type": "research"},
  {"id": "design", "content": "Design the API", "depends_on": ["research"]},
  {"id": "implement", "content": "Implement API", "depends_on": ["design"], "task_type": "code"},
  {"id": "test", "content": "Write tests", "depends_on": ["implement"], "task_type": "validate"}
]"""

    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "task_type": {
                            "type": "string",
                            "enum": ["research", "code", "validate", "review", "general"]
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "result_key": {"type": "string"},
                        "can_parallel": {"type": "boolean"}
                    },
                    "required": ["id", "content"]
                },
                "description": "List of tasks with dependencies"
            },
            "execute": {
                "type": "boolean",
                "description": "If true, execute the workflow. If false, just validate and show plan.",
                "default": False
            }
        },
        "required": ["tasks"]
    }

    def __init__(self, orchestrator: SubAgentOrchestrator, working_dir: str = "."):
        self.subagent_orchestrator = orchestrator
        self.working_dir = working_dir

    def execute(self, tasks: List[Dict[str, Any]], execute: bool = False) -> ToolResult:
        from .tools.atomic.todo import TodoTool
        from .orchestrator import TaskOrchestrator, OrchestratorConfig

        # Create todo with tasks
        todo = TodoTool()

        # Add status to all tasks
        for task in tasks:
            task.setdefault("status", "pending")
            task.setdefault("task_type", "general")
            task.setdefault("depends_on", [])
            task.setdefault("can_parallel", True)
            task.setdefault("result_key", task["id"])

        # Validate tasks
        result = todo.execute(todos=tasks)
        if not result.success:
            return ToolResult(
                success=False,
                output=None,
                error=f"Invalid workflow: {result.error}"
            )

        # Show execution plan
        plan_result = todo.execute(query="execution_order")

        if not execute:
            return ToolResult(
                success=True,
                output=f"## Workflow Validated\n\n{plan_result.output}\n\nSet execute=true to run this workflow."
            )

        # Execute the workflow
        config = OrchestratorConfig(
            max_parallel_tasks=4,
            verbose=True,
        )

        orchestrator = TaskOrchestrator(
            todo_tool=todo,
            subagent_orchestrator=self.subagent_orchestrator,
            config=config,
        )

        exec_result = orchestrator.execute_all()

        # Format output
        lines = [
            "## Workflow Execution Complete",
            "",
            f"**Status:** {'Success' if exec_result['success'] else 'Failed'}",
            f"**Completed:** {exec_result['completed']}/{exec_result['total']}",
            f"**Failed:** {exec_result['failed']}/{exec_result['total']}",
            f"**Duration:** {exec_result['duration_seconds']:.1f}s",
            "",
            "### Results",
            ""
        ]

        for key, value in exec_result['results'].items():
            preview = str(value)[:200]
            if len(str(value)) > 200:
                preview += "..."
            lines.append(f"**{key}:**")
            lines.append(f"  {preview}")
            lines.append("")

        return ToolResult(
            success=exec_result['success'],
            output="\n".join(lines),
            error=None if exec_result['success'] else f"{exec_result['failed']} tasks failed"
        )


def create_agent_with_orchestration(
    model: str = DEFAULT_MODEL,
    working_dir: str = ".",
    verbose: bool = True
) -> AgentLoop:
    """
    Create an agent with full orchestration capability.

    Features:
    - Sub-agent spawning (task tool)
    - Workflow execution (workflow tool)
    - Dependency-aware parallel execution
    - Result passing between tasks

    Example:
        agent = create_agent_with_orchestration()
        agent.run('''
        Create a workflow to:
        1. Research the codebase
        2. Design improvements
        3. Implement changes
        4. Write tests
        ''')
    """
    # Create tools with orchestration support
    tools = create_default_registry(working_dir)
    orchestrator = SubAgentOrchestrator(tools=tools, working_dir=working_dir)

    # Register both task and workflow tools
    tools.register(TaskTool(orchestrator))
    tools.register(WorkflowTool(orchestrator, working_dir))

    config = AgentConfig(
        model=model,
        working_dir=working_dir,
        verbose=verbose
    )

    system_prompt = """You are an expert software engineering agent with task orchestration capabilities.

## Available Tools

### task - Delegate single tasks
Use for isolated, independent tasks:
- **researcher**: Explore and understand code (read-only)
- **reviewer**: Review code for issues
- **test_writer**: Write tests
- **general**: General purpose tasks

### workflow - Execute task workflows
Use for complex multi-step work with dependencies:
- Define tasks with IDs and dependencies
- Orchestrator runs independent tasks in parallel
- Results automatically pass to dependent tasks

## When to Use Each

**Use `task`** for:
- Single research questions
- One-off code reviews
- Independent explorations

**Use `workflow`** for:
- Multi-step implementations
- Tasks that build on each other
- Work requiring specific execution order

## Workflow Example

```json
{{
  "tasks": [
    {{"id": "research", "content": "Research auth patterns", "task_type": "research"}},
    {{"id": "design", "content": "Design auth system", "depends_on": ["research"]}},
    {{"id": "implement", "content": "Implement auth", "depends_on": ["design"], "task_type": "code"}},
    {{"id": "test", "content": "Write auth tests", "depends_on": ["implement"], "task_type": "validate"}}
  ],
  "execute": true
}}
```

Current working directory: {working_dir}
""".format(working_dir=os.path.abspath(working_dir))

    return AgentLoop(config=config, tools=tools, system_prompt=system_prompt)
