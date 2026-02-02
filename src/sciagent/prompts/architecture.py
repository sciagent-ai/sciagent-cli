"""
Claude Code-style Architecture: Minimal prompt + Rich tool schemas

The insight: Don't put capabilities in the system prompt.
Put them in tool descriptions. The LLM reads those too.
"""

# =============================================================================
# MINIMAL SYSTEM PROMPT (~200 tokens)
# =============================================================================

SYSTEM_PROMPT = """You are a software engineering agent with access to tools for file operations, search, command execution, and sub-agent delegation.

# Guidelines
- Read files before modifying
- Use the right tool for the job (see tool descriptions)
- For complex exploration, use task_agent instead of manual searching
- Be concise

# Error Recovery
- Tools auto-retry on transient failures
- If a tool fails after retries, try alternative approach
- Never give up on first failure

Working directory: {working_dir}
Available tools: {tool_count}
"""

# =============================================================================
# RICH TOOL SCHEMAS - This is where the complexity lives
# =============================================================================

TOOL_SCHEMAS = [
    # -------------------------------------------------------------------------
    # BASH - with retry behavior documented
    # -------------------------------------------------------------------------
    {
        "name": "bash",
        "description": """Execute shell commands with automatic retry on timeout.

BEHAVIOR:
- Commands auto-retry up to 3 times with exponential backoff (30s → 60s → 120s → 240s)
- Long-running commands (npm, pip, git clone) start with higher timeout
- On final timeout failure, returns suggestion to try alternative approach

WHEN TO USE:
- Running scripts, builds, tests
- Installing packages
- Git operations
- Any shell command

WHEN NOT TO USE:
- Reading files (use view instead)
- Writing files (use write_file instead)
- Searching code (use grep_search instead)""",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Initial timeout in seconds. Auto-extends on retry. Default: 30"
                }
            },
            "required": ["command"]
        }
    },

    # -------------------------------------------------------------------------
    # VIEW - file reading
    # -------------------------------------------------------------------------
    {
        "name": "view",
        "description": """Read file contents or list directory.

ALWAYS use this before editing a file. Shows line numbers for easy reference.

For directories: lists contents with file/folder indicators.
For files: shows content with line numbers. Use start_line/end_line for large files.""",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path"},
                "start_line": {"type": "integer", "description": "Start line (1-indexed)"},
                "end_line": {"type": "integer", "description": "End line (-1 for EOF)"}
            },
            "required": ["path"]
        }
    },

    # -------------------------------------------------------------------------
    # WRITE_FILE
    # -------------------------------------------------------------------------
    {
        "name": "write_file",
        "description": """Create or overwrite a file with content.

Use for:
- Creating new files
- Completely replacing file contents

For partial edits, use str_replace instead.""",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to create/overwrite"},
                "content": {"type": "string", "description": "Full file content"}
            },
            "required": ["path", "content"]
        }
    },

    # -------------------------------------------------------------------------
    # STR_REPLACE - surgical edits
    # -------------------------------------------------------------------------
    {
        "name": "str_replace",
        "description": """Replace a unique string in a file.

REQUIREMENTS:
- old_str must appear EXACTLY ONCE in the file
- Use view first to see exact content including whitespace

FAILS WHEN:
- String not found
- String appears multiple times (make it more specific)""",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit"},
                "old_str": {"type": "string", "description": "Exact string to replace (must be unique)"},
                "new_str": {"type": "string", "description": "Replacement string"}
            },
            "required": ["path", "old_str", "new_str"]
        }
    },

    # -------------------------------------------------------------------------
    # GLOB_SEARCH - find files
    # -------------------------------------------------------------------------
    {
        "name": "glob_search",
        "description": """Find files matching a glob pattern.

Examples:
- **/*.py - all Python files
- src/**/*.ts - TypeScript in src/
- **/test_*.py - all test files

Returns list of matching paths sorted by modification time.""",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Base directory (default: working dir)"}
            },
            "required": ["pattern"]
        }
    },

    # -------------------------------------------------------------------------
    # GREP_SEARCH - search content
    # -------------------------------------------------------------------------
    {
        "name": "grep_search",
        "description": """Search file contents using regex.

Use for:
- Finding function/class definitions
- Locating imports or dependencies
- Finding TODOs or error messages

Returns matching lines with file paths and line numbers.""",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search"},
                "path": {"type": "string", "description": "Directory to search (default: working dir)"},
                "include": {"type": "string", "description": "File glob to include (e.g., *.py)"}
            },
            "required": ["pattern"]
        }
    },

    # -------------------------------------------------------------------------
    # TASK_AGENT - Sub-agent delegation (the big one)
    # -------------------------------------------------------------------------
    {
        "name": "task_agent",
        "description": """Spawn a specialized sub-agent for complex tasks.

AVAILABLE AGENTS:

1. **explore** - Fast codebase exploration
   Use for: "where is X defined?", "how does Y work?", "find all Z"
   Example: task_agent(type="explore", task="find where user auth is handled")

2. **bash_runner** - Command execution specialist
   Use for: Complex shell workflows, build processes
   Example: task_agent(type="bash_runner", task="build the project and run tests")

3. **researcher** - Web search and documentation
   Use for: Finding solutions, library docs, best practices
   Example: task_agent(type="researcher", task="how to display molecules in React")

4. **planner** - Architecture and planning
   Use for: Designing implementation approach before coding
   Example: task_agent(type="planner", task="plan the auth system refactor")

WHEN TO USE:
- Task requires multiple search/read operations
- You need focused expertise on a subtask
- Exploring unfamiliar codebase
- Research before implementation

WHEN NOT TO USE:
- Simple single-file operations
- You already know exactly what to do""",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["explore", "bash_runner", "researcher", "planner"],
                    "description": "Type of sub-agent to spawn"
                },
                "task": {
                    "type": "string",
                    "description": "Clear description of what the agent should do"
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background? Default: false"
                }
            },
            "required": ["type", "task"]
        }
    },

    # -------------------------------------------------------------------------
    # TODO_WRITE - Task tracking
    # -------------------------------------------------------------------------
    {
        "name": "todo_write",
        "description": """Track task progress with a todo list.

USE FOR:
- Complex multi-step tasks
- Tracking what's done vs remaining
- Making progress visible

FORMAT:
Each todo has: content, status (pending/in_progress/completed)

GUIDELINES:
- Mark todo in_progress BEFORE starting work on it
- Mark completed IMMEDIATELY after finishing
- Only one item should be in_progress at a time""",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
                        },
                        "required": ["content", "status"]
                    }
                }
            },
            "required": ["todos"]
        }
    },
]


# =============================================================================
# HOW TO USE THIS
# =============================================================================

def build_system_prompt(working_dir: str, tools: list) -> str:
    """Build minimal system prompt."""
    return SYSTEM_PROMPT.format(
        working_dir=working_dir,
        tool_count=len(tools)
    )


def get_tool_schemas() -> list:
    """
    Return tool schemas for LLM.

    The LLM reads these descriptions when deciding which tool to use.
    This is where your "intelligence" about tool usage lives.
    """
    return TOOL_SCHEMAS


# =============================================================================
# KEY INSIGHT
# =============================================================================
"""
BEFORE (your original approach):
- 2000+ token system prompt trying to explain everything
- Simple tool schemas with minimal descriptions
- Agent didn't know WHEN to use each tool

AFTER (Claude Code approach):
- ~200 token system prompt with principles only
- Rich tool schemas that explain WHEN and HOW
- Sub-agent descriptions embedded in task_agent tool
- Retry/recovery behavior documented in bash tool

The LLM reads tool descriptions just like it reads the system prompt.
Move the complexity there.
"""
