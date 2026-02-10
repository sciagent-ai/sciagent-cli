"""
Core Agent Loop - The main orchestration engine

This implements the classic agent loop:
    while(has_tool_calls):
        execute_tools()
        feed_results_to_llm()
"""
import os
import json
import signal
import traceback
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime

from .llm import LLMClient, Message, LLMResponse, ToolCall
from .tools import ToolRegistry, ToolResult, create_default_registry
from .state import (
    AgentState, ContextWindow, TodoList, StateManager,
    generate_session_id
)
from .display import Display, create_display, Spinner
from .defaults import DEFAULT_MODEL
from .prompts import build_system_prompt


@dataclass
class AgentConfig:
    """Configuration for the agent"""
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_tokens: int =  16384 # 32768  # Large limit for thorough code generation
    max_iterations: int = 120  # Default for complex tasks (simple tasks typically finish in <10)
    working_dir: str = "."
    verbose: bool = True
    auto_save: bool = True
    state_dir: str = ".agent_states"
    reasoning_effort: str = "medium"  # Extended thinking enabled at medium level


# DEFAULT_SYSTEM_PROMPT is now built dynamically from prompts/*.md files
# See prompts/loader.py for the build_system_prompt function


class AgentLoop:
    """
    The core agent execution loop
    
    Implements: Think → Act → Observe → Repeat
    """
    
    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        tools: Optional[ToolRegistry] = None,
        llm: Optional[LLMClient] = None,
        system_prompt: Optional[str] = None,
        display: Optional[Display] = None,
    ):
        self.config = config or AgentConfig()
        self.tools = tools or create_default_registry(self.config.working_dir)
        self.llm = llm or LLMClient(
            model=self.config.model,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            reasoning_effort=self.config.reasoning_effort
        )

        # Display management
        self.display = display or create_display(
            verbose=self.config.verbose,
            quiet=not self.config.verbose
        )

        # State management
        self.state_manager = StateManager(self.config.state_dir)
        
        # Initialize state
        # Compute absolute path to registry.yaml (in package directory)
        try:
            from importlib.resources import files
            registry_path = files("sciagent").joinpath("services", "registry.yaml")
        except (ImportError, TypeError):
            # Fallback for older Python or edge cases
            import pathlib
            registry_path = pathlib.Path(__file__).parent / "services" / "registry.yaml"

        # Build system prompt from modular files
        prompt = system_prompt or build_system_prompt(
            working_dir=os.path.abspath(self.config.working_dir),
            registry_path=str(registry_path)
        )
        self.state = AgentState(
            session_id=generate_session_id(),
            context=ContextWindow(system_prompt=prompt),
            todos=TodoList(),
            working_dir=self.config.working_dir,
            model=self.config.model,
            temperature=self.config.temperature,
            max_iterations=self.config.max_iterations
        )
        
        # Callbacks
        self._on_tool_start: Optional[Callable] = None
        self._on_tool_end: Optional[Callable] = None
        self._on_thinking: Optional[Callable] = None
        self._on_response: Optional[Callable] = None
        
        # Iteration tracking
        self.iteration_count = 0
        self.total_tokens = 0

        # Spiral detection - track repeated errors
        self._error_counts: Dict[str, int] = {}
        self._max_same_error = 3

        # User interrupt handling
        self._paused = False
        self._cancelled = False
        self._user_feedback = None
        self._original_sigint = signal.getsignal(signal.SIGINT)

    # =========================================================================
    # Interrupt Handling
    # =========================================================================

    def _handle_interrupt(self, signum, frame):
        """Handle Ctrl+C - just set flag, don't call input() here.

        IMPORTANT: Signal handlers can interrupt at any point, including
        during readline operations. Calling input() here causes
        "can't re-enter readline" errors. The actual menu is handled
        in _handle_pause_menu() which is called from the main loop.
        """
        self._paused = True
        print("\n\n⏸ Paused. Processing...")

    def _handle_pause_menu(self):
        """Display pause menu and get user choice. Called from main loop."""
        print("What would you like to do?")
        print("  [c] Continue")
        print("  [s] Stop")
        print("  [f] Give feedback/redirect")
        try:
            choice = input("\nChoice: ").strip().lower()
            if choice == 's':
                self._cancelled = True
                print("Stopping...")
            elif choice == 'f':
                feedback = input("Your feedback: ").strip()
                if feedback:
                    self._user_feedback = feedback
                    print(f"Got it. Will incorporate: {feedback[:50]}...")
            else:
                print("Continuing...")
        except EOFError:
            self._cancelled = True
        self._paused = False

    def _prompt_user_for_input(self, request: Dict[str, Any]) -> str:
        """
        Display a question to the user and get their response.

        Args:
            request: The ask_user tool output containing question, options, context, default

        Returns:
            The user's response string
        """
        print("\n" + "=" * 60)
        print("🤔 AGENT NEEDS YOUR INPUT")
        print("=" * 60)

        # Show context if provided
        if request.get("context"):
            print(f"\nContext: {request['context']}")

        # Show the question
        print(f"\n{request['question']}")

        # Show options if provided
        options = request.get("options")
        default = request.get("default")

        if options:
            print("\nOptions:")
            for i, opt in enumerate(options, 1):
                default_marker = " (default)" if opt == default else ""
                print(f"  [{i}] {opt}{default_marker}")
            print(f"  [0] Other (type your own response)")

            # Get choice
            prompt = f"\nYour choice [1-{len(options)}, or 0 for other]"
            if default:
                prompt += f" (Enter for '{default}')"
            prompt += ": "

            try:
                choice = input(prompt).strip()

                if not choice and default:
                    print(f"Using default: {default}")
                    return default

                if choice.isdigit():
                    idx = int(choice)
                    if 1 <= idx <= len(options):
                        selected = options[idx - 1]
                        print(f"Selected: {selected}")
                        return selected
                    elif idx == 0:
                        custom = input("Your response: ").strip()
                        return custom if custom else (default or options[0])

                # If input matches an option directly, use it
                if choice in options:
                    return choice

                # Otherwise treat as custom response
                return choice if choice else (default or options[0])

            except (EOFError, KeyboardInterrupt):
                print(f"\nUsing default: {default or options[0]}")
                return default or options[0]
        else:
            # Free-form response
            prompt = "\nYour response"
            if default:
                prompt += f" (Enter for '{default}')"
            prompt += ": "

            try:
                response = input(prompt).strip()
                if not response and default:
                    print(f"Using default: {default}")
                    return default
                return response if response else "No response provided"
            except (EOFError, KeyboardInterrupt):
                return default or "No response provided"

    def _setup_interrupt_handler(self):
        """Install our interrupt handler"""
        signal.signal(signal.SIGINT, self._handle_interrupt)

    def _restore_interrupt_handler(self):
        """Restore original interrupt handler"""
        signal.signal(signal.SIGINT, self._original_sigint)

    # =========================================================================
    # Skill Auto-Injection
    # =========================================================================

    def _get_matching_skill_content(self, task: str) -> Optional[str]:
        """
        Check if task matches any skill triggers.
        If so, return the skill workflow to inject into context.

        This makes skills automatic rather than opt-in - the agent doesn't
        need to explicitly call the skill tool to get workflow guidance.
        """
        try:
            from .skills import SkillLoader

            loader = SkillLoader()
            skill = loader.match_skill(task)

            if skill:
                return f"""[SYSTEM] Matched skill: {skill.name}

{skill.description}

---

**Follow this workflow:**

{skill.workflow}

---
"""
        except Exception:
            # If skills can't be loaded, continue without injection
            pass

        return None

    # =========================================================================
    # Context Management
    # =========================================================================

    def _summarize_context(self, messages: List) -> str:
        """
        Use LLM to summarize a section of conversation context.

        This preserves important information when compressing context,
        rather than simply truncating and losing information.
        """
        # Format messages for summarization
        formatted = []
        for msg in messages:
            role = msg.role if hasattr(msg, 'role') else msg.get('role', 'unknown')
            content = msg.content if hasattr(msg, 'content') else msg.get('content', '')

            if role == "tool":
                name = msg.name if hasattr(msg, 'name') else msg.get('name', 'tool')
                # Truncate long tool outputs for summary
                if len(content) > 500:
                    content = content[:500] + "... [truncated]"
                formatted.append(f"[Tool: {name}] {content}")
            elif role == "assistant":
                # Check for tool calls
                tool_calls = msg.tool_calls if hasattr(msg, 'tool_calls') else msg.get('tool_calls')
                if tool_calls:
                    tools_used = [tc.get('function', {}).get('name', 'unknown')
                                  if isinstance(tc, dict) else getattr(tc, 'name', 'unknown')
                                  for tc in tool_calls]
                    formatted.append(f"[Assistant used tools: {', '.join(tools_used)}]")
                if content:
                    formatted.append(f"[Assistant] {content[:300]}..." if len(content) > 300 else f"[Assistant] {content}")
            elif role == "user":
                formatted.append(f"[User] {content[:200]}..." if len(content) > 200 else f"[User] {content}")

        context_text = "\n".join(formatted)

        # Use the LLM to summarize
        summary_prompt = f"""Summarize the following conversation context concisely.
Focus on:
1. Key decisions made
2. Important findings/results
3. Files created or modified
4. Current state of the task

Context to summarize:
{context_text}

Provide a concise summary (max 500 words):"""

        try:
            from .llm import Message as LLMMessage
            summary_response = self.llm.chat([
                LLMMessage(role="user", content=summary_prompt)
            ])
            return summary_response.content
        except Exception as e:
            # Fallback: return a simple truncated version
            return f"[Context summary failed: {str(e)}]\n\nRecent activity included: {context_text[:1000]}..."

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def on_tool_start(self, callback: Callable[[str, Dict], None]):
        """Register callback for when a tool starts"""
        self._on_tool_start = callback
        return self
    
    def on_tool_end(self, callback: Callable[[str, ToolResult], None]):
        """Register callback for when a tool completes"""
        self._on_tool_end = callback
        return self
    
    def on_thinking(self, callback: Callable[[str], None]):
        """Register callback for LLM thinking/reasoning"""
        self._on_thinking = callback
        return self
    
    def on_response(self, callback: Callable[[str], None]):
        """Register callback for final responses"""
        self._on_response = callback
        return self
    
    # =========================================================================
    # Tool Execution
    # =========================================================================

    # Error patterns and fixes - language agnostic where possible
    _ERROR_PATTERNS = [
        # Timeouts
        (r'timeout|timed?\s*out', 'TIMEOUT'),
        # Import/Module errors (Python, Node, etc.)
        (r'importerror|modulenotfound|cannot find module|module not found', 'IMPORT_ERROR'),
        # Type errors
        (r'typeerror.*complex|cannot convert complex', 'COMPLEX_TYPE'),
        (r'typeerror|type error', 'TYPE_ERROR'),
        # Serialization
        (r'json.*serial|not json serial|circular|stringify', 'JSON_ERROR'),
        # Syntax errors
        (r'syntaxerror|syntax error|unexpected token|parsing error', 'SYNTAX_ERROR'),
        # File/path errors
        (r'filenotfound|enoent|no such file|path.*not found', 'FILE_NOT_FOUND'),
        # Permission errors
        (r'permission denied|eacces|access denied', 'PERMISSION_ERROR'),
        # Memory errors
        (r'out of memory|memoryerror|heap|allocation failed', 'MEMORY_ERROR'),
        # Network errors
        (r'connection refused|econnrefused|network|socket|fetch failed', 'NETWORK_ERROR'),
        # Key/attribute errors
        (r'keyerror|attributeerror|undefined is not|cannot read propert', 'KEY_ERROR'),
        # Index/bounds errors
        (r'indexerror|out of bounds|index out of range', 'INDEX_ERROR'),
        # Null/None errors
        (r'nonetype|null pointer|cannot read.*null|undefined', 'NULL_ERROR'),
        # Build errors
        (r'build failed|compilation failed|compile error', 'BUILD_ERROR'),
        # Test failures
        (r'test failed|assertion.*failed|expect.*received', 'TEST_FAILURE'),
    ]

    _FIX_SUGGESTIONS = {
        'TIMEOUT': (
            "Command timed out. Try:\n"
            "1. Create a simplified/faster version of the script\n"
            "2. Reduce data size or iterations\n"
            "3. Add progress output to see where it's stuck\n"
            "4. Break into smaller steps that complete quickly"
        ),
        'IMPORT_ERROR': (
            "Module not found. Try:\n"
            "1. Check spelling of module name\n"
            "2. Install missing dependency (pip install X, npm install X, etc.)\n"
            "3. Check if module is in correct path/directory\n"
            "4. Verify virtual environment is activated"
        ),
        'COMPLEX_TYPE': (
            "Complex number type error. Try:\n"
            "1. Use .real to extract real part before float operations\n"
            "2. Use abs() for magnitude\n"
            "3. Cast explicitly: float(x.real)"
        ),
        'TYPE_ERROR': (
            "Type mismatch. Try:\n"
            "1. Check variable types with print(type(x))\n"
            "2. Add explicit type conversion\n"
            "3. Verify function arguments match expected types"
        ),
        'JSON_ERROR': (
            "JSON serialization failed. Try:\n"
            "1. Convert numpy arrays: arr.tolist()\n"
            "2. Handle special types: default=str or custom encoder\n"
            "3. Check for circular references\n"
            "4. Use json.dumps(obj, default=lambda x: str(x)) as fallback"
        ),
        'SYNTAX_ERROR': (
            "Syntax error in code. Try:\n"
            "1. Check for missing brackets, quotes, or semicolons\n"
            "2. Verify indentation (Python) or braces (JS/Go/etc.)\n"
            "3. Look at the line number indicated\n"
            "4. Check for incompatible language version features"
        ),
        'FILE_NOT_FOUND': (
            "File or path not found. Try:\n"
            "1. Verify path is correct with ls or dir\n"
            "2. Check working directory with pwd\n"
            "3. Use absolute path instead of relative\n"
            "4. Create parent directories if needed: mkdir -p"
        ),
        'PERMISSION_ERROR': (
            "Permission denied. Try:\n"
            "1. Check file permissions: ls -la\n"
            "2. Ensure you own the file or have write access\n"
            "3. Don't write to system/protected directories\n"
            "4. Check if file is locked by another process"
        ),
        'MEMORY_ERROR': (
            "Out of memory. Try:\n"
            "1. Process data in smaller chunks\n"
            "2. Use generators instead of loading all into memory\n"
            "3. Delete unused variables\n"
            "4. Reduce problem size or use streaming"
        ),
        'NETWORK_ERROR': (
            "Network/connection error. Try:\n"
            "1. Check if service/URL is accessible\n"
            "2. Verify network connectivity\n"
            "3. Check for firewall or proxy issues\n"
            "4. Add retry logic with backoff"
        ),
        'KEY_ERROR': (
            "Key or attribute not found. Try:\n"
            "1. Check exact key/property name (case-sensitive)\n"
            "2. Use .get(key, default) for safe access\n"
            "3. Print available keys: print(obj.keys()) or console.log(Object.keys(obj))\n"
            "4. Check if object is None/null before accessing"
        ),
        'INDEX_ERROR': (
            "Index out of bounds. Try:\n"
            "1. Check array/list length before accessing\n"
            "2. Use len()-1 for last element\n"
            "3. Add bounds checking\n"
            "4. Verify loop ranges are correct"
        ),
        'NULL_ERROR': (
            "Null/None reference. Try:\n"
            "1. Add null check before accessing: if x is not None\n"
            "2. Use optional chaining: obj?.property (JS) \n"
            "3. Provide default values\n"
            "4. Trace back to find where None originates"
        ),
        'BUILD_ERROR': (
            "Build/compilation failed. Try:\n"
            "1. Read the full error message for specific issue\n"
            "2. Check for missing dependencies\n"
            "3. Verify configuration files are correct\n"
            "4. Try clean build: remove build artifacts and rebuild"
        ),
        'TEST_FAILURE': (
            "Test assertion failed. Try:\n"
            "1. Check expected vs actual values in error\n"
            "2. Verify test data/fixtures are correct\n"
            "3. Run single test in isolation to debug\n"
            "4. Add print/console.log to trace values"
        ),
    }

    def _error_signature(self, error: str) -> str:
        """Normalize error to detect repeated failures - language agnostic"""
        import re
        err = error.lower()
        # Remove variable parts (line numbers, paths, values)
        err = re.sub(r'line \d+', 'line N', err)
        err = re.sub(r"'[^']*'", "'X'", err)
        err = re.sub(r'"[^"]*"', '"X"', err)
        err = re.sub(r'\d+', 'N', err)

        # Match against known patterns
        for pattern, sig in self._ERROR_PATTERNS:
            if re.search(pattern, err):
                return sig
        return f"UNKNOWN_{hash(err[:100]) % 10000}"

    def _get_fix_suggestion(self, error_sig: str, error_text: str) -> str:
        """Get concrete fix suggestion for an error type"""
        if error_sig in self._FIX_SUGGESTIONS:
            return self._FIX_SUGGESTIONS[error_sig]
        # Generic fallback
        return (
            "Error occurred. Try:\n"
            "1. Read the full error message carefully\n"
            "2. Search for the error message online\n"
            "3. Simplify the code to isolate the issue\n"
            "4. Try an alternative approach"
        )

    def _extract_log_path(self, error_output: str) -> Optional[str]:
        """Extract log file path from error output if present."""
        import re
        # Look for patterns like "_logs/xxx.log" or "[Full log saved: path]"
        match = re.search(r'_logs/[^\s\]]+\.log', error_output)
        if match:
            return match.group(0)
        return None

    def _check_spiral(self, error: str):
        """Detect errors, provide fixes, and warn on debugging spirals.

        Three-stage escalation:
        1. First occurrence: Provide helpful inline fix suggestions
        2. Second occurrence: Suggest using debugger subagent to investigate
        3. Third occurrence: Ask user for help
        """
        sig = self._error_signature(error)
        self._error_counts[sig] = self._error_counts.get(sig, 0) + 1
        count = self._error_counts[sig]

        fix_suggestion = self._get_fix_suggestion(sig, error)

        # Try to extract log path from error (useful for 2nd stage)
        log_path = self._extract_log_path(error)
        log_ref = log_path or "_logs/"

        if count == 1:
            # First occurrence: provide helpful inline fix suggestion
            self.state.context.add_user_message(
                f"[SYSTEM] Error detected: {sig}\n\n"
                f"Suggested fixes:\n{fix_suggestion}\n\n"
                f"Apply one of these fixes and retry."
            )
        elif count == 2:
            # Second occurrence: suggest debug subagent
            error_preview = error[:300] if len(error) > 300 else error
            self.state.context.add_user_message(
                f"[SYSTEM] Same error occurred again: {sig}\n\n"
                f"The previous fix didn't work. Use the debug agent to investigate:\n"
                f"task(agent_name=\"debug\", task=\"Read {log_ref} and find root cause of: {error_preview}\")\n\n"
                f"Or try a different approach from:\n{fix_suggestion}"
            )
        elif count >= self._max_same_error:
            # Third occurrence: ask user for help
            self.state.context.add_user_message(
                f"[SYSTEM] DEBUGGING SPIRAL DETECTED\n\n"
                f"Error '{sig}' has occurred {count} times.\n\n"
                f"Please ask the user for guidance using ask_user tool."
            )
            self._error_counts[sig] = 0  # Reset after warning

    def _execute_tool(self, tool_call: ToolCall, defer_spiral: bool = False) -> ToolResult:
        """Execute a single tool call

        Args:
            tool_call: The tool call to execute
            defer_spiral: If True, skip spiral detection (caller will handle it later)
                         This is important for Anthropic API compliance - spiral warnings
                         add user messages which must come AFTER all tool_results.
        """
        if self._on_tool_start:
            self._on_tool_start(tool_call.name, tool_call.arguments)

        self.display.tool_start(tool_call.name, tool_call.arguments)

        # Show spinner for potentially long-running tools (only if takes > 0.3s)
        long_running_tools = {"bash", "shell", "web_search", "read_url", "http_request", "web", "service"}
        if tool_call.name in long_running_tools:
            with Spinner("Executing", quiet=self.display.quiet, delay=0.3):
                result = self.tools.execute(tool_call.name, **tool_call.arguments)
        else:
            result = self.tools.execute(tool_call.name, **tool_call.arguments)

        if self._on_tool_end:
            self._on_tool_end(tool_call.name, result)

        # Special handling for ask_user tool - prompt user and return their response
        if tool_call.name == "ask_user" and result.success:
            output = result.output
            if isinstance(output, dict) and output.get("awaiting_user_input"):
                # Get user input
                user_response = self._prompt_user_for_input(output)
                # Replace the tool result with the user's response
                result = ToolResult(
                    success=True,
                    output=f"User responded: {user_response}"
                )
                self.display.tool_end(tool_call.name, success=True, message=f"User: {user_response[:50]}...")
                return result

        # Spiral detection: track errors (skip if deferred to caller)
        if not defer_spiral:
            if result.error:
                self._check_spiral(result.error)
            elif result.output and 'error' in str(result.output).lower():
                self._check_spiral(str(result.output))

        # Format result message
        result_message = None
        if result.output:
            output_str = str(result.output)
            lines = output_str.count('\n') + 1

            # Always show full output for todo tool (important for user visibility)
            if tool_call.name == "todo":
                result_message = None  # Will trigger special handling below
            elif lines > 1:
                result_message = f"{lines} lines of output"
            else:
                result_message = output_str[:100]

        # Special handling for todo tool - show the full task list and sync state
        if tool_call.name == "todo" and result.success and result.output:
            self.display.tool_end(tool_call.name, success=True, message="")
            print(result.output)  # Print full todo list
            # Sync the agent's state with the tool's todo list
            if "todos" in tool_call.arguments:
                self.state.todos.sync_from_tool(tool_call.arguments["todos"])
        else:
            self.display.tool_end(
                tool_call.name,
                success=result.success,
                message=result_message,
                error=result.error
            )

        return result
    
    def _execute_tool_calls(self, tool_calls: List[ToolCall]) -> List[Dict]:
        """
        Execute all tool calls and return results.

        CRITICAL: Every tool_call MUST have a corresponding tool_result added to context,
        even if execution fails. This is required by Anthropic's API.

        IMPORTANT: Tool results must be added IMMEDIATELY after the assistant message
        with tool_use. No other messages (like spiral warnings) can be inserted in between.
        """
        results = []
        deferred_spiral_checks = []  # Defer spiral warnings until after all tool_results

        for tc in tool_calls:
            try:
                result = self._execute_tool(tc, defer_spiral=True)
            except Exception as e:
                # Ensure we still add a result even if tool execution crashes
                result = ToolResult(
                    success=False,
                    output=None,
                    error=f"Tool execution failed: {str(e)}"
                )
                self.display.tool_end(tc.name, success=False, error=str(e))

            results.append({
                "tool_call_id": tc.id,
                "name": tc.name,
                "result": result
            })

            # ALWAYS add tool result to context - this is mandatory for Anthropic API
            # Must happen before any user messages (like spiral warnings)
            self.state.context.add_tool_result(
                tool_call_id=tc.id,
                tool_name=tc.name,
                result=result.to_message()
            )

            # Collect errors for deferred spiral checking
            if result.error:
                deferred_spiral_checks.append(result.error)
            elif result.output and 'error' in str(result.output).lower():
                deferred_spiral_checks.append(str(result.output))

        # Now that all tool_results are added, check for spirals
        # This adds user messages which must come AFTER all tool_results
        for error in deferred_spiral_checks:
            self._check_spiral(error)

        return results
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    def _single_step(self) -> LLMResponse:
        """Execute a single iteration of the agent loop"""
        # Validate and repair message structure before LLM call
        # This prevents Anthropic API errors about orphaned tool_use blocks
        issues = self.state.context.validate_and_repair()
        if issues:
            for issue in issues:
                self.display.warning(f"Context repair: {issue}")

        messages = self.state.context.get_messages()
        tool_schemas = self.tools.get_schemas()

        # Show spinner while waiting for LLM response (only if takes > 0.5s)
        with Spinner("Thinking", quiet=self.display.quiet, delay=0.5):
            response = self.llm.chat(messages, tools=tool_schemas)

        # Track usage
        self.total_tokens += response.usage.get("prompt_tokens", 0)
        self.total_tokens += response.usage.get("completion_tokens", 0)

        return response
    
    def _check_iteration_limit(self, max_iter: int) -> Optional[str]:
        """
        Check if approaching iteration limit with incomplete tasks.
        Ask user for guidance if so.

        Returns:
            None to continue, or action string ('wrap_up', 'continue', or new max as string)
        """
        iterations_left = max_iter - self.iteration_count
        warn_threshold = 3  # Warn when 3 iterations left

        if iterations_left > warn_threshold:
            return None

        # Check if there are incomplete todos (use TodoStatus enum)
        from .state import TodoStatus
        incomplete_todos = [t for t in self.state.todos.items if t.status != TodoStatus.DONE]

        if not incomplete_todos:
            return None  # All done, no need to warn

        # Show warning and ask user
        print(f"\n⚠️  Approaching iteration limit ({iterations_left} iterations left)")
        print(f"   {len(incomplete_todos)} task(s) still incomplete:")
        for todo in incomplete_todos[:5]:  # Show max 5
            status_icon = "◐" if todo.status.name == "IN_PROGRESS" else "☐"
            print(f"     {status_icon} {todo.description}")
        if len(incomplete_todos) > 5:
            print(f"     ... and {len(incomplete_todos) - 5} more")

        print("\nWhat would you like to do?")
        print("  [w] Wrap up - ask agent to summarize current progress")
        print("  [c] Continue - keep going (may hit limit)")
        print("  [+N] Add N more iterations (e.g., +10, +25)")

        try:
            choice = input("\nChoice [w/c/+N]: ").strip().lower()

            if choice == 'w':
                return 'wrap_up'
            elif choice == 'c':
                return 'continue'
            elif choice.startswith('+') and choice[1:].isdigit():
                additional = int(choice[1:])
                return str(max_iter + additional)  # Return new max
            else:
                print("Invalid choice, continuing...")
                return 'continue'
        except (EOFError, KeyboardInterrupt):
            print("\nWrapping up...")
            return 'wrap_up'

    def _generate_wrap_up_result(self) -> str:
        """Generate a summary result when wrapping up early."""
        # Inject wrap-up instruction
        self.state.context.add_user_message(
            "[SYSTEM] Iteration limit approaching. Please provide a concise summary of:\n"
            "1. What was accomplished\n"
            "2. Current state of incomplete tasks\n"
            "3. What remains to be done\n"
            "Do NOT make any more tool calls - just summarize."
        )

        try:
            response = self._single_step()
            if response.content:
                return response.content
        except Exception as e:
            pass

        # Fallback: generate from todo state
        completed = [t for t in self.state.todos.items if t.status == "completed"]
        incomplete = [t for t in self.state.todos.items if t.status != "completed"]

        result = "## Progress Summary\n\n"
        if completed:
            result += "### Completed:\n"
            for t in completed:
                result += f"- {t.content}\n"
        if incomplete:
            result += "\n### Incomplete:\n"
            for t in incomplete:
                status = "In Progress" if t.status == "in_progress" else "Pending"
                result += f"- [{status}] {t.content}\n"

        return result

    def run(self, task: str, max_iterations: int = None) -> str:
        """
        Run the agent loop until completion.

        Args:
            task: The task/prompt to execute
            max_iterations: Override max iterations (default: from config)

        Returns:
            Final response from the agent
        """
        # Setup interrupt handling
        self._setup_interrupt_handler()
        self._cancelled = False
        self._user_feedback = None

        max_iter = max_iterations or self.config.max_iterations
        self._iteration_limit_checked = False  # Track if we've already asked user

        # Auto-inject matching skill workflow before the task
        skill_content = self._get_matching_skill_content(task)
        if skill_content:
            self.state.context.add_user_message(skill_content)

        # Add user message
        self.state.context.add_user_message(task)

        self.display.task_start(task, project_dir=self.config.working_dir)

        final_response = ""

        try:
            while self.iteration_count < max_iter:
                # Check for user cancellation
                if self._cancelled:
                    final_response = "(Stopped by user)"
                    break

                # Handle pause menu (user pressed Ctrl+C)
                if self._paused:
                    self._handle_pause_menu()
                    if self._cancelled:
                        final_response = "(Stopped by user)"
                        break

                # Check if approaching limit with incomplete tasks (ask once)
                if not self._iteration_limit_checked:
                    action = self._check_iteration_limit(max_iter)
                    if action:
                        self._iteration_limit_checked = True
                        if action == 'wrap_up':
                            final_response = self._generate_wrap_up_result()
                            break
                        elif action == 'continue':
                            pass  # Keep going
                        elif action.isdigit():
                            max_iter = int(action)
                            self._iteration_limit_checked = False  # Reset so check triggers again
                            print(f"   Increased max iterations to {max_iter}")

                # Inject user feedback if provided
                if self._user_feedback:
                    self.state.context.add_user_message(f"[User feedback]: {self._user_feedback}")
                    self._user_feedback = None

                self.iteration_count += 1

                # Compress context if getting too large (don't stop, compress)
                if self.state.context.token_estimate() > 120000:
                    print("  📦 Compressing context...")
                    self.state.context.compress_if_needed(summarizer=self._summarize_context)

                try:
                    response = self._single_step()
                except KeyboardInterrupt:
                    # Handle Ctrl+C that escaped signal handler
                    self._paused = True
                    self._handle_pause_menu()
                    if self._cancelled:
                        final_response = "(Stopped by user)"
                        break
                    continue  # Retry the LLM call
                except Exception as e:
                    error_msg = f"LLM Error: {str(e)}"
                    self.display.error(error_msg)
                    final_response = f"(Error: {str(e)})"
                    break

                # Handle pause after LLM call
                if self._paused:
                    self._handle_pause_menu()
                    if self._cancelled:
                        final_response = "(Stopped by user)"
                        break

                # Handle assistant content (thinking)
                if response.content:
                    if self._on_thinking and response.has_tool_calls:
                        self._on_thinking(response.content)
                    elif self._on_response and not response.has_tool_calls:
                        self._on_response(response.content)

                    # Show thinking if there are more tool calls coming
                    if response.has_tool_calls:
                        self.display.thinking(response.content)

                # Check for tool calls
                if response.has_tool_calls:
                    # Add assistant message with tool calls
                    self.state.context.add_assistant_message(
                        content=response.content,
                        tool_calls=[{
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments)
                            }
                        } for tc in response.tool_calls]
                    )

                    # Execute tools
                    self._execute_tool_calls(response.tool_calls)

                    # Handle pause after tool execution
                    if self._paused:
                        self._handle_pause_menu()
                        if self._cancelled:
                            final_response = "(Stopped by user)"
                            break

                else:
                    # No tool calls = done
                    final_response = response.content
                    self.state.context.add_assistant_message(content=response.content)
                    break

            # If we exit the loop due to max iterations (not break), generate result
            if self.iteration_count >= max_iter and not final_response:
                print(f"\n⚠️  Reached maximum iterations ({max_iter})")
                final_response = self._generate_wrap_up_result()

        finally:
            # Always restore original interrupt handler
            self._restore_interrupt_handler()

        # Auto-save state
        if self.config.auto_save:
            self.state_manager.save(self.state)

        self.display.task_complete({
            "iterations": self.iteration_count,
            "tokens": self.total_tokens
        })

        return final_response
    
    def run_interactive(self):
        """Run in interactive mode (REPL)"""
        print("🤖 Ready! Enter your task or question.\n")
        
        while True:
            try:
                user_input = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break
            
            if not user_input:
                continue
            
            if user_input.lower() == 'exit':
                break
            
            if user_input.lower() == 'status':
                print(f"\nSession: {self.state.session_id}")
                print(f"Messages: {len(self.state.context.messages)}")
                print(f"Iterations: {self.iteration_count}")
                print(f"Tokens: ~{self.total_tokens}")
                print(self.state.todos.to_string())
                continue
            
            if user_input.lower() == 'clear':
                self.state.context.clear()
                self.iteration_count = 0
                print("Context cleared.")
                continue
            
            # Run the task
            response = self.run(user_input)
            print(f"\n{response}")
    
    # =========================================================================
    # Session Management
    # =========================================================================
    
    def save_session(self) -> str:
        """Save current session and return session ID"""
        self.state_manager.save(self.state)
        return self.state.session_id
    
    def load_session(self, session_id: str) -> bool:
        """Load a previous session"""
        state = self.state_manager.load(session_id)
        if state:
            self.state = state
            return True
        return False
    
    def list_sessions(self) -> List[Dict]:
        """List available sessions"""
        return self.state_manager.list_sessions()


# =============================================================================
# Convenience Functions
# =============================================================================

def run_task(
    task: str,
    model: str = DEFAULT_MODEL,
    tools: Optional[ToolRegistry] = None,
    working_dir: str = ".",
    verbose: bool = True
) -> str:
    """
    Simple one-shot task execution
    
    Example:
        result = run_task("Create a Python script that prints hello world")
    """
    config = AgentConfig(
        model=model,
        working_dir=working_dir,
        verbose=verbose
    )
    agent = AgentLoop(config=config, tools=tools)
    return agent.run(task)


def create_agent(
    model: str = DEFAULT_MODEL,
    tools: Optional[ToolRegistry] = None,
    working_dir: str = ".",
    system_prompt: Optional[str] = None,
    verbose: bool = True
) -> AgentLoop:
    """
    Create a configured agent
    
    Example:
        agent = create_agent(model="openai/gpt-4o")
        agent.run("Analyze this codebase")
    """
    config = AgentConfig(
        model=model,
        working_dir=working_dir,
        verbose=verbose
    )
    return AgentLoop(config=config, tools=tools, system_prompt=system_prompt)
