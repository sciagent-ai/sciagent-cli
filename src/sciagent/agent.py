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
import subprocess
import threading
import traceback
from typing import Dict, Any, List, Optional, Callable

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.patch_stdout import patch_stdout
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

        # Store registry path for skill variable substitution
        self._registry_path = str(registry_path)

        # Build system prompt from modular files
        prompt = system_prompt or build_system_prompt(
            working_dir=os.path.abspath(self.config.working_dir),
            registry_path=self._registry_path
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

        # Integrity: Evidence tracking (Action 3)
        self._evidence = {
            "fetches_total": 0,
            "fetches_ok": 0,
            "execs_total": 0,
            "execs_ok": 0,
            "files_created": 0
        }

        # Integrity: Consecutive failure tracking for external data (Action 2)
        # Force user prompt after 3 consecutive failures to prevent LLM from
        # proceeding with "local knowledge" when required data isn't available
        self._consecutive_external_failures = 0
        self._max_consecutive_external_failures = 3

        # User interrupt handling - thread-safe for immediate response
        self._paused = False
        self._cancelled = False
        self._user_feedback = None
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._interrupt_event = threading.Event()  # Signals blocking ops to check
        self._menu_lock = threading.Lock()  # Prevents multiple menus
        self._menu_shown = False  # Track if menu is currently displayed
        self._interrupt_count = 0  # Track rapid Ctrl+C presses
        self._parent_interrupt_event = None  # Set by parent for subagents

    # =========================================================================
    # Interrupt Handling
    # =========================================================================

    def _handle_interrupt(self, signum, frame):
        """Handle Ctrl+C with immediate menu display.

        Uses threading to show menu immediately without waiting for
        blocking operations to complete. This provides responsive UX.
        """
        self._interrupt_count += 1
        self._paused = True
        self._interrupt_event.set()  # Signal any blocking waits

        # Force stop on 3 rapid interrupts (escape hatch)
        if self._interrupt_count >= 3:
            print("\n\n🛑 Force stopping...")
            self._cancelled = True
            # Restore original handler and re-raise to actually exit
            self._restore_interrupt_handler()
            raise KeyboardInterrupt("Force stopped by user (3x Ctrl+C)")

        # Show menu immediately in a thread (non-blocking)
        if not self._menu_shown:
            print("\n\n⏸ Paused.")
            menu_thread = threading.Thread(target=self._show_menu_thread, daemon=True)
            menu_thread.start()

    def _show_menu_thread(self):
        """Show pause menu in a separate thread for immediate response."""
        with self._menu_lock:
            if self._menu_shown:  # Another thread already showing
                return
            self._menu_shown = True

        try:
            self._handle_pause_menu()
        finally:
            with self._menu_lock:
                self._menu_shown = False
                self._interrupt_count = 0  # Reset after menu handled

    def _handle_pause_menu(self):
        """Display pause menu and get user choice."""
        # Small delay to let any pending output settle
        import time
        time.sleep(0.1)

        print("What would you like to do?")
        print("  [c] Continue")
        print("  [s] Stop")
        print("  [f] Give feedback/redirect")

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                choice = pt_prompt("\nChoice [c/s/f]: ").strip().lower()

                if choice == 's' or choice == 'stop':
                    self._cancelled = True
                    print("Stopping...")
                    break
                elif choice == 'f' or choice == 'feedback':
                    feedback = pt_prompt("Your feedback: ").strip()
                    if feedback:
                        self._user_feedback = feedback
                        print(f"Got it. Will incorporate: {feedback[:50]}...")
                    break
                elif choice == 'c' or choice == 'continue' or choice == '':
                    print("Continuing...")
                    break
                else:
                    # Unrecognized input - prompt again
                    if attempt < max_attempts - 1:
                        print(f"  Please enter 'c' to continue, 's' to stop, or 'f' for feedback.")
                    else:
                        print("  Defaulting to continue...")

            except (EOFError, OSError, KeyboardInterrupt):
                # EOFError: stdin closed, OSError: terminal issues
                self._cancelled = True
                break

        self._paused = False
        self._interrupt_event.clear()  # Reset for next interrupt

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
            max_attempts = 5  # Prevent infinite loops on bad input

            for attempt in range(max_attempts):
                # Check if user cancelled via Ctrl+C
                if self._cancelled:
                    fallback = default or options[0]
                    print(f"\n(Cancelled, using: {fallback})")
                    return fallback

                # Show options on first attempt or after invalid input
                if attempt == 0:
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
                    choice = pt_prompt(prompt).strip()

                    # Empty input with default available
                    if not choice and default:
                        print(f"Using default: {default}")
                        return default

                    # Empty input with no default - re-prompt
                    if not choice:
                        print("⚠️  Please enter a choice (1-{} or 0 for other)".format(len(options)))
                        continue

                    # Numeric input - validate it's in range
                    if choice.isdigit():
                        idx = int(choice)
                        if 1 <= idx <= len(options):
                            selected = options[idx - 1]
                            print(f"✓ Selected: {selected}")
                            return selected
                        elif idx == 0:
                            # User explicitly chose "Other" - get custom response
                            custom = pt_prompt("Your response: ").strip()
                            if custom:
                                print(f"✓ Custom response: {custom}")
                                return custom
                            elif default:
                                print(f"Using default: {default}")
                                return default
                            else:
                                print("⚠️  Empty response. Please try again.")
                                continue
                        else:
                            # Number out of range
                            print(f"⚠️  Invalid choice '{idx}'. Please enter 1-{len(options)} or 0 for other.")
                            continue

                    # Check if input matches an option text exactly (case-insensitive)
                    choice_lower = choice.lower()
                    for i, opt in enumerate(options):
                        if opt.lower() == choice_lower or opt.lower().startswith(choice_lower):
                            print(f"✓ Selected: {opt}")
                            return opt

                    # Input doesn't match any option - this is likely an error
                    # Don't auto-accept arbitrary text as a response
                    print(f"⚠️  '{choice}' is not a valid option.")
                    print(f"    Enter a number (1-{len(options)}) or 0 to provide a custom response.")

                except (EOFError, KeyboardInterrupt):
                    print(f"\nUsing default: {default or options[0]}")
                    return default or options[0]

            # Exhausted attempts - use default or first option
            fallback = default or options[0]
            print(f"\n⚠️  Max attempts reached. Using: {fallback}")
            return fallback
        else:
            # Free-form response (no options)
            prompt = "\nYour response"
            if default:
                prompt += f" (Enter for '{default}')"
            prompt += ": "

            try:
                response = pt_prompt(prompt).strip()
                if not response and default:
                    print(f"Using default: {default}")
                    return default
                return response if response else "No response provided"
            except (EOFError, KeyboardInterrupt):
                return default or "No response provided"

    def _setup_interrupt_handler(self):
        """Install our interrupt handler and reset state.

        If this agent has a parent (is a subagent), skip installing
        signal handler - let parent handle signals and propagate via event.
        """
        self._interrupt_event.clear()
        self._menu_shown = False
        self._interrupt_count = 0
        # Only install signal handler if we're the top-level agent
        if self._parent_interrupt_event is None:
            signal.signal(signal.SIGINT, self._handle_interrupt)

    def _restore_interrupt_handler(self):
        """Restore original interrupt handler and cleanup"""
        # Only restore if we installed a handler (top-level agent)
        if self._parent_interrupt_event is None:
            signal.signal(signal.SIGINT, self._original_sigint)
        self._interrupt_event.clear()
        self._menu_shown = False

    def _is_cancelled(self) -> bool:
        """Check if this agent or parent was cancelled."""
        if self._cancelled:
            return True
        if self._parent_interrupt_event and self._parent_interrupt_event.is_set():
            self._cancelled = True  # Propagate to local state
            return True
        return False

    def _wait_for_menu_if_paused(self) -> bool:
        """Wait for any active menu interaction to complete.

        Returns True if cancelled, False otherwise.
        """
        if not self._paused:
            return self._is_cancelled()

        # Wait for menu thread to finish (with timeout to avoid deadlock)
        for _ in range(100):  # 10 second max wait
            if not self._menu_shown:
                break
            import time
            time.sleep(0.1)

        # If menu wasn't shown in thread, show it now
        if self._paused and not self._menu_shown:
            self._handle_pause_menu()

        return self._cancelled

    # =========================================================================
    # Interruptible LLM Calls
    # =========================================================================

    def _interruptible_llm_call(
        self,
        messages,
        tools=None,
        poll_interval: float = 0.2
    ):
        """
        Execute LLM call in a way that can be interrupted by Ctrl+C.

        Runs the actual HTTP call in a background thread and polls for
        completion while checking the interrupt event. This allows the
        user to stop long-running LLM calls immediately.

        Args:
            messages: Messages to send to LLM
            tools: Tool schemas
            poll_interval: How often to check for interrupts (seconds)

        Returns:
            LLMResponse from the model

        Raises:
            InterruptedError: If user cancelled during the call
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        # Container for result/exception from background thread
        result_container = {"response": None, "error": None}

        def _run_llm():
            try:
                result_container["response"] = self.llm.chat(messages, tools=tools)
            except Exception as e:
                result_container["error"] = e

        # Start LLM call in background thread
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_llm)

            # Poll for completion while checking interrupt flag
            while not future.done():
                # Check if user requested stop
                if self._cancelled:
                    # Can't actually cancel the HTTP request, but we can return early
                    # The background thread will complete eventually but we ignore it
                    raise InterruptedError("LLM call cancelled by user")

                # Check if user paused (Ctrl+C pressed)
                if self._interrupt_event.is_set():
                    # Wait for menu interaction to complete
                    if self._wait_for_menu_if_paused():
                        raise InterruptedError("LLM call cancelled by user")
                    # User chose to continue - keep polling

                # Wait briefly before next check
                try:
                    future.result(timeout=poll_interval)
                    break  # Completed successfully
                except FuturesTimeoutError:
                    continue  # Not done yet, keep polling

        # Check for errors from the background thread
        if result_container["error"]:
            raise result_container["error"]

        return result_container["response"]

    # =========================================================================
    # Integrity: Gates and Fail-Fast (Actions 1, 2, 3)
    # =========================================================================

    # External tools that access resources outside the agent's control
    EXTERNAL_TOOLS = {"web", "fetch", "http_request", "service", "web_search", "read_url"}

    # Failure signals for external resources
    FAILURE_SIGNALS = ["403", "404", "500", "timeout", "refused", "unavailable", "connection error"]

    # Container/docker specific failure signals
    CONTAINER_FAILURE_SIGNALS = [
        # Missing packages
        "importerror", "modulenotfounderror", "no module named",
        # Container issues
        "image not found", "no such image", "pull access denied",
        "unable to find image",  # Docker's exact error when image not pulled
        # Execution failures
        "exec failed", "container failed", "exited with code",
    ]

    def _check_gates(self, tool_call: ToolCall) -> Optional[str]:
        """
        Action 1: Gate check that runs for ALL tool calls.
        Returns None if passed, or error message if blocked.

        Extend this method to add project-specific integrity checks.
        """
        # Placeholder for custom gates (data_gate, exec_gate, etc.)
        return None

    def _handle_gate_failure(self, tool_call: ToolCall, gate_error: str) -> ToolResult:
        """Handle a gate check failure."""
        self.display.warning(f"Gate blocked: {gate_error}")
        return ToolResult(success=False, output=None, error=f"Blocked by gate: {gate_error}")

    def _pause_for_user(self, reason: str, options: List[str]) -> ToolResult:
        """
        Action 2: Pause execution and get user decision on external failure.
        Forces human involvement instead of letting LLM work around failures.
        """
        print(f"\n⚠️  {reason}")
        print("\nWhat would you like to do?")
        for i, opt in enumerate(options, 1):
            print(f"  [{i}] {opt}")

        try:
            choice = pt_prompt("\nChoice: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(options):
                selected = options[int(choice) - 1]
                return ToolResult(success=False, output=None, error=f"User chose: {selected}")
            return ToolResult(success=False, output=None, error=f"User input: {choice}")
        except (EOFError, KeyboardInterrupt):
            return ToolResult(success=False, output=None, error="User chose: stop")

    def _is_container_failure(self, command: str, result: ToolResult) -> bool:
        """Check if a bash command was a docker/container execution that failed."""
        if result.success:
            return False

        cmd_lower = command.lower()
        if "docker" not in cmd_lower:
            return False

        # Check error output for container-specific failures
        error_text = str(result.error or "").lower() + str(result.output or "").lower()
        return any(sig in error_text for sig in self.CONTAINER_FAILURE_SIGNALS + self.FAILURE_SIGNALS)

    def _extract_missing_image(self, error_text: str) -> Optional[str]:
        """Extract image name from 'Unable to find image' error."""
        import re
        # Docker error format: "Unable to find image 'ghcr.io/org/image:tag' locally"
        match = re.search(r"unable to find image ['\"]([^'\"]+)['\"]", error_text.lower())
        if match:
            # Return original case from error text
            original_match = re.search(r"[Uu]nable to find image ['\"]([^'\"]+)['\"]", error_text)
            return original_match.group(1) if original_match else match.group(1)
        return None

    def _auto_pull_image(self, image: str) -> bool:
        """Attempt to pull a docker image. Returns True if successful."""
        self.display.info(f"Image not found locally, pulling {image}...")
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=300  # 5 min timeout for pull
            )
            if result.returncode == 0:
                self.display.success(f"Successfully pulled {image}")
                return True
            else:
                self.display.warning(f"Failed to pull {image}: {result.stderr[:200]}")
                return False
        except subprocess.TimeoutExpired:
            self.display.warning(f"Timeout pulling {image}")
            return False
        except Exception as e:
            self.display.warning(f"Error pulling {image}: {e}")
            return False

    def _collect_evidence_summary(self) -> Dict[str, int]:
        """Action 3: Collect evidence summary for final output."""
        return self._evidence.copy()

    def _print_evidence_summary(self):
        """Action 3: Print lightweight evidence summary before final response."""
        ev = self._evidence
        if ev["fetches_total"] > 0 or ev["execs_total"] > 0 or ev["files_created"] > 0:
            print(f"\n📊 Session: {ev['fetches_ok']}/{ev['fetches_total']} fetches, "
                  f"{ev['execs_ok']}/{ev['execs_total']} execs, "
                  f"{ev['files_created']} files created")

            if ev["fetches_total"] > 0 and ev["fetches_ok"] == 0:
                print("⚠️  No external data successfully retrieved.")

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
                # Apply variable substitution to skill workflow
                # Skills use <placeholder> syntax (e.g., <registry_path>)
                workflow = skill.workflow
                workflow = workflow.replace("<registry_path>", self._registry_path)
                workflow = workflow.replace("{registry_path}", self._registry_path)
                workflow = workflow.replace("<working_dir>", os.path.abspath(self.config.working_dir))
                workflow = workflow.replace("{working_dir}", os.path.abspath(self.config.working_dir))

                return f"""[SYSTEM] Matched skill: {skill.name}

{skill.description}

**Registry path**: `{self._registry_path}`

---

**Follow this workflow:**

{workflow}

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
            with Spinner("Executing", quiet=self.display.quiet, delay=0.3, show_hint=True):
                result = self.tools.execute(tool_call.name, **tool_call.arguments)
        else:
            result = self.tools.execute(tool_call.name, **tool_call.arguments)

        if self._on_tool_end:
            self._on_tool_end(tool_call.name, result)

        # Integrity Action 2: Fail-fast on container/external failures
        # Track evidence for external tools
        if tool_call.name in self.EXTERNAL_TOOLS:
            self._evidence["fetches_total"] += 1
            if result.success:
                self._evidence["fetches_ok"] += 1
                # Reset consecutive failures on success
                self._consecutive_external_failures = 0
            else:
                # Track consecutive failures
                self._consecutive_external_failures += 1

                # Fail-fast: after max consecutive external data failures, force user decision
                # This prevents the LLM from silently "proceeding with local knowledge"
                if self._consecutive_external_failures >= self._max_consecutive_external_failures:
                    self._consecutive_external_failures = 0  # Reset counter
                    return self._pause_for_user(
                        f"External data access failed {self._max_consecutive_external_failures} times: {result.error or 'No data retrieved'}",
                        options=[
                            "Provide alternative data source (I'll specify)",
                            "Continue with explicit limitations (document missing data)",
                            "Stop task - required data not available"
                        ]
                    )

        # Track bash executions (especially docker)
        if tool_call.name == "bash":
            self._evidence["execs_total"] += 1
            if result.success:
                self._evidence["execs_ok"] += 1

            # Fail-fast: docker/container command failed → handle intelligently
            cmd = tool_call.arguments.get("command", "")
            if self._is_container_failure(cmd, result):
                error_text = str(result.error or "") + str(result.output or "")

                # Check for "unable to find image" - auto-pull and retry
                missing_image = self._extract_missing_image(error_text)
                if missing_image:
                    if self._auto_pull_image(missing_image):
                        # Retry the original command
                        self.display.info("Retrying command after pull...")
                        retry_result = self.tools.execute(tool_call)
                        if retry_result.success:
                            self._evidence["execs_ok"] += 1
                            return retry_result
                        # If retry still fails, fall through to pause
                        result = retry_result
                        error_text = str(result.error or "") + str(result.output or "")

                # Show clear error with actual Docker output, not just exit code
                # Extract first meaningful error line from output
                display_error = result.error or "Unknown error"
                if result.output:
                    # Look for actual error content in output
                    for line in str(result.output).split('\n'):
                        line = line.strip()
                        if line and any(sig in line.lower() for sig in ['error', 'unable', 'cannot', 'failed', 'not found', 'denied']):
                            display_error = line[:200]  # Truncate long lines
                            break

                return self._pause_for_user(
                    f"Container execution failed: {display_error}",
                    options=[
                        "Use build-service to add missing package",
                        "Install at runtime (pip install in container)",
                        "Try alternative approach",
                        "Stop"
                    ]
                )

        # Track file creation
        if tool_call.name == "file_ops":
            action = tool_call.arguments.get("action", "")
            if action in ("write", "create") and result.success:
                self._evidence["files_created"] += 1

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
        pending_images = []  # Collect images to inject as multimodal message

        for tc in tool_calls:
            # Integrity Action 1: Gate check runs for ALL tools
            gate_error = self._check_gates(tc)
            if gate_error:
                result = self._handle_gate_failure(tc, gate_error)
                self.display.tool_end(tc.name, success=False, error=gate_error)
            else:
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

            # Check if this is an image result from file_ops
            is_image_result = (
                result.success and
                isinstance(result.output, dict) and
                result.output.get("type") == "image"
            )

            if is_image_result:
                # Collect image for multimodal message
                pending_images.append({
                    "media_type": result.output["media_type"],
                    "data": result.output["data"],
                    "file_path": result.output.get("file_path", "unknown")
                })
                # Use display text for the tool result (don't send base64 as text)
                tool_result_text = result.output.get("display_text", "[Image loaded]")
            else:
                tool_result_text = result.to_message()

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
                result=tool_result_text
            )

            # Collect errors for deferred spiral checking
            if result.error:
                deferred_spiral_checks.append(result.error)
            elif result.output and not is_image_result and 'error' in str(result.output).lower():
                deferred_spiral_checks.append(str(result.output))

        # Now that all tool_results are added, check for spirals
        # This adds user messages which must come AFTER all tool_results
        for error in deferred_spiral_checks:
            self._check_spiral(error)

        # If images were loaded, inject them as a multimodal user message
        # This allows the LLM to actually "see" the images
        if pending_images:
            image_paths = [img["file_path"] for img in pending_images]
            self.state.context.add_multimodal_user_message(
                text=f"[System: {len(pending_images)} image(s) loaded: {', '.join(image_paths)}. Please analyze the image(s) above.]",
                images=pending_images
            )

        return results
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    def _single_step(self) -> LLMResponse:
        """Execute a single iteration of the agent loop.

        Uses interruptible LLM call so user can stop with Ctrl+C.
        """
        # Validate and repair message structure before LLM call
        # This prevents Anthropic API errors about orphaned tool_use blocks
        issues = self.state.context.validate_and_repair()
        if issues:
            for issue in issues:
                self.display.warning(f"Context repair: {issue}")

        messages = self.state.context.get_messages()
        tool_schemas = self.tools.get_schemas()

        # Use interruptible LLM call - allows Ctrl+C to stop immediately
        # The spinner runs in the main thread while LLM call runs in background
        with Spinner("Thinking", quiet=self.display.quiet, delay=0.5, interrupt_event=self._interrupt_event):
            response = self._interruptible_llm_call(messages, tools=tool_schemas)

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
            choice = pt_prompt("\nChoice [w/c/+N]: ").strip().lower()

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
                # Check for user cancellation (including parent cancellation for subagents)
                if self._is_cancelled():
                    final_response = "(Stopped by user)"
                    break

                # Handle pause menu (user pressed Ctrl+C)
                if self._wait_for_menu_if_paused():
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
                except InterruptedError:
                    # LLM call was interrupted by user (via our interruptible wrapper)
                    # Check if they want to stop or continue
                    if self._cancelled:
                        final_response = "(Stopped by user)"
                        break
                    # User chose to continue after pause - retry the LLM call
                    continue
                except KeyboardInterrupt:
                    # Handle Ctrl+C that escaped signal handler
                    self._paused = True
                    self._interrupt_event.set()
                    if self._wait_for_menu_if_paused():
                        final_response = "(Stopped by user)"
                        break
                    continue  # Retry the LLM call
                except Exception as e:
                    error_msg = f"LLM Error: {str(e)}"
                    self.display.error(error_msg)
                    final_response = f"(Error: {str(e)})"
                    break

                # Handle pause after LLM call
                if self._wait_for_menu_if_paused():
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
                    if self._wait_for_menu_if_paused():
                        final_response = "(Stopped by user)"
                        break

                else:
                    # No tool calls = done
                    # Integrity Action 3: Show evidence summary before final output
                    self._print_evidence_summary()
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
                user_input = pt_prompt("\n> ").strip()
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
