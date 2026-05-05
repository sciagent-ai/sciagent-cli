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
import threading
import uuid
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from datetime import datetime, timezone

from .llm import LLMClient, Message
from .tools import ToolRegistry, BaseTool, ToolResult, create_default_registry
from .state import ContextWindow, generate_session_id
from .agent import AgentLoop, AgentConfig
from .defaults import DEFAULT_MODEL
from .subagent_observations import (
    Observation,
    OBSERVATION_PROMPT_BLOCK,
    format_observations_for_parent,
    parse_observations_block,
)


@dataclass
class SubAgentConfig:
    """Configuration for a sub-agent"""
    name: str
    description: str
    system_prompt: str
    model: str = DEFAULT_MODEL
    max_iterations: int = 40
    # Session token budget. Tighter than the AgentLoop default for compute-
    # heavy subagents whose tool results (sky logs, manifest snapshots) can
    # explode context per turn. 0 inherits AgentConfig default.
    max_session_tokens: int = 0
    allowed_tools: Optional[List[str]] = None  # None = all tools
    temperature: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "max_iterations": self.max_iterations,
            "max_session_tokens": self.max_session_tokens,
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
    # Set when spawn(background=True) returns immediately with a registry id
    # the caller uses with task_wait / task_get. None for synchronous spawns.
    task_id: Optional[str] = None
    # Lite-tier observations the subagent emitted in its terminal reply.
    # Parsed off the output (between <observations>...</observations>) by
    # SubAgent.run; bubbled to the parent's tool result by TaskTool out of
    # band — they don't count toward the 4KB output cap.
    observations: List[Observation] = field(default_factory=list)

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
            "session_id": self.session_id,
            "task_id": self.task_id,
            "observations": [o.to_dict() for o in self.observations],
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
        is_nested: bool = False,  # True if spawned by another agent
        parent_interrupt_event: Optional["threading.Event"] = None  # Share parent's interrupt state
    ):
        self.config = config
        self.working_dir = working_dir
        self.is_nested = is_nested
        self.parent_interrupt_event = parent_interrupt_event

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
        
        # Create the underlying agent.
        #
        # verbose=True for nested subagents: this gates *display* output,
        # not LLM context. The subagent's TOOL RESULTS still stay in the
        # subagent's context (TaskTool returns a bounded summary; that's
        # the LLM-context boundary). But its tool *calls* should print to
        # the user's terminal so they have visibility into a long-running
        # cloud job — without that, a `task(agent_name="compute", ...)`
        # invocation looks frozen until completion.
        agent_config = AgentConfig(
            model=config.model,
            temperature=config.temperature,
            max_iterations=config.max_iterations,
            working_dir=working_dir,
            verbose=True,
            auto_save=False
        )
        # Per-subagent token budget overrides AgentConfig's default. 0 means
        # inherit (e.g., the verifier with its 20-iter cap doesn't need a
        # tighter cap).
        if config.max_session_tokens > 0:
            agent_config.max_session_tokens = config.max_session_tokens

        # M1B provenance correlation: AgentLoop.__init__ calls
        # ComputeTool.set_shared_session(state.session_id) +
        # set_active_session(state.session_id), which would clobber the
        # parent's session ids. compute_run events emitted from inside the
        # subagent would then land in a different provenance log, fragmenting
        # the audit trail. For nested subagents, restore the parent's session
        # ids after the child AgentLoop has constructed — the child keeps
        # state.session_id for its own state file, but compute / provenance
        # correlation uses the parent's session so verify_session(parent_id)
        # sees the full hierarchy. Top-level (non-nested) agents use their
        # own session as before.
        from .tools.atomic.compute import ComputeTool
        from .provenance_log import set_active_session, _active_session_id

        parent_shared_session = ComputeTool._shared_session_id if is_nested else None
        parent_active_session = _active_session_id if is_nested else None

        self.agent = AgentLoop(
            config=agent_config,
            tools=self.tools,
            system_prompt=config.system_prompt
        )

        if is_nested and parent_shared_session:
            ComputeTool.set_shared_session(parent_shared_session)
        if is_nested and parent_active_session:
            set_active_session(parent_active_session)

        # Track BOTH ids: the child's own session_id (for state file
        # naming, debug attribution) and the parent session this subagent
        # emits provenance under. Equal at the top level; differ when nested.
        self.session_id = self.agent.state.session_id
        self.parent_session_id = parent_shared_session
    
    def run(self, task: str) -> SubAgentResult:
        """Execute a task and return the result"""
        import time
        start_time = time.time()

        # Check if parent was already cancelled before starting. Distinguish
        # a fresh cancellation (user just pressed Ctrl+C) from a STALE event
        # (a prior Ctrl+C that the pause-menu clear didn't fully drain). The
        # orchestrator drains stale events before constructing us, so if we
        # hit this check, it really means the event was set in the brief
        # window between drain and start — most likely fresh user intent.
        # Either way, the message must be specific enough that the calling
        # LLM doesn't pattern-match "Cancelled by parent" as a retry-able
        # transient and re-spawn in a loop.
        if self.parent_interrupt_event and self.parent_interrupt_event.is_set():
            return SubAgentResult(
                agent_name=self.config.name,
                task=task,
                success=False,
                output="",
                error=(
                    "Subagent NOT started: parent agent's interrupt event "
                    "is set. This is TERMINAL for this spawn — do NOT "
                    "retry the same task immediately, the next spawn will "
                    "see the same state. Either (a) ask the user via "
                    "ask_user whether they meant to cancel — they may have "
                    "pressed Ctrl+C earlier and the event leaked across "
                    "the menu, or (b) report up to the main agent so the "
                    "user can intervene. If the user explicitly wants to "
                    "continue, advise them to send any non-empty message "
                    "(or run `clear` and re-issue the task) to drain the "
                    "stuck state."
                ),
                iterations=0,
                tokens_used=0,
                duration_seconds=0.0,
                session_id=self.session_id
            )

        # Share parent's interrupt event with child agent
        if self.parent_interrupt_event:
            self.agent._parent_interrupt_event = self.parent_interrupt_event

        try:
            output = self.agent.run(task)
            success = True
            error = None
        except KeyboardInterrupt:
            output = "(Stopped by user)"
            success = False
            error = "User interrupt"
        except Exception as e:
            output = ""
            success = False
            error = str(e)

        duration = time.time() - start_time

        # Lite-tier: parse any <observations>...</observations> block off the
        # terminal output before it reaches the parent. Stripping the block
        # from `output` keeps observations out of band — TaskTool's 4KB cap
        # applies to narrative only; observations are bubbled separately
        # under their own header. Best-effort: malformed blocks return ([],
        # output) unchanged.
        observations: List[Observation] = []
        if output:
            observations, output = parse_observations_block(
                output, session_id=self.session_id
            )

        return SubAgentResult(
            agent_name=self.config.name,
            task=task,
            success=success,
            output=output,
            error=error,
            iterations=self.agent.iteration_count,
            tokens_used=self.agent.total_tokens,
            duration_seconds=duration,
            session_id=self.session_id,
            observations=observations,
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
        - verifier: VERIFICATION_MODEL (Sonnet) - independent claim verification
        """
        from .defaults import FAST_MODEL, CODING_MODEL, SCIENTIFIC_MODEL, VERIFICATION_MODEL
        from .prompts.loader import load_prompt

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
            max_iterations=40
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
            max_iterations=60
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
            max_iterations=50
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
            max_iterations=40
        ))

        # Compute agent - cloud job orchestration in an isolated context
        # Uses CODING_MODEL (Sonnet) - writing 50-200 line scripts, debugging
        # cloud errors, picking images all need real coding chops.
        #
        # WHY a subagent and not direct main-agent tools: every byte of cloud
        # orchestration (script content, install chatter, 80+ line bg_output,
        # status polls) currently lands in the main agent's context and stays
        # there for the rest of the conversation. That was the bulk of the
        # ~381k tokens on a "hello world on sky" run. Encapsulating cloud
        # work in a subagent contains the chatter to its own bubble; the main
        # agent only ever sees the bounded summary the subagent returns
        # (TaskTool already caps at 4k chars).
        self.register(SubAgentConfig(
            name="compute",
            description="Run jobs on the cloud (SkyPilot) and bring outputs back. Use for any 'on sky', 'on AWS', 'in the cloud' task.",
            model=CODING_MODEL,
            system_prompt="""You orchestrate cloud compute jobs end-to-end. Your goal is "user wants X run on the cloud, with results locally" — return when files are local.

## Never poll across LLM turns (read this BEFORE waiting on anything)

Every LLM turn that polls a status (status snapshot → think → status →
think) costs ~5–30s of thinking + tokens. For a 5-min cluster
provisioning that's 10+ wasted turns; for a 30-min solver that's 60+
turns and tens of thousands of tokens. The LLM client also caps a
single turn at ~600s, so excessive polling can crash the session.

For every long-running operation there is a paired wait_* tool that
blocks INSIDE one tool call until the wait condition is met:

  - Cluster provisioning to UP:
    `compute_cluster(action="wait_until_up", cluster_name=..., timeout=300)`
  - Cluster-mode job to terminal (compute_exec result):
    `compute_cluster(action="wait_for_job", cluster_name=...,
                     cluster_job_id=..., timeout=1800)`
  - Managed-jobs to terminal:
    `bg_wait(job_id, block=True, timeout=600)`
  - Any registered task (kind-agnostic) to terminal:
    `task_wait(id, timeout=600)`

These wait_* tools all honor Ctrl+C (interrupt-aware) and return a
structured verdict: `{ready/terminal: bool, status, elapsed_sec,
timed_out: bool, ...}`. If `timed_out=True`, call the same wait_* tool
again with a longer timeout. NEVER intersperse status snapshots —
that's the polling anti-pattern this rule exists to prevent.

Use `monitor` for any case where progress visibility matters before
terminal — including a single long-running job whose log might surface
FATAL/ERROR signals you want to capture *before* the cluster transitions
out of UP. It spawns a background subprocess; each stdout line lands as
a <system-reminder> event on a subsequent turn, no LLM round-trip per
event. Two patterns:

  - **Solo job + log tail (very common, do this for any cluster-mode
    exec >30s).** Launch `monitor` on `sky logs <cluster> <job_id>`
    BEFORE `wait_for_job`. (`sky logs` follows by default until the job
    reaches a terminal state — do NOT pass `-f`; that flag does not
    exist and the command exits immediately.) If wait returns FAILED,
    the FATAL line is already in your context — no race against
    autostop, no `ClusterNotUpError` from a post-hoc `sky logs` call.
    Worked example:
      monitor(command="sky logs my-cluster 2 2>&1 | grep --line-buffered -E 'End$|FATAL|ERROR|Killed|completed|failed'",
              description="exec 2 milestones")
      compute_cluster(action="wait_for_job", cluster_name="my-cluster", cluster_job_id=2, timeout=1800)
      # If FAILED → the matched lines are already streamed.
  - **Many things at once (parallel jobs, multi-step pipelines).** One
    `monitor` per stream, all draining into the same event channel — the
    agent reacts to whichever fires first.

**Filter to milestones, not heartbeats.** Every stdout line your
pipeline emits costs tokens on the next turn. The harness caps at 20
events per watcher per drain, but even at the cap a chatty pipeline
spends 1-2K tokens each cycle on noise. Pipe through grep/awk to
emit only state transitions and milestones the agent will actually
act on:

  # GOOD — terminal markers + errors only
  monitor(command="<your-log-source> | grep --line-buffered -E 'End$|FATAL|ERROR|completed|failed'",
          description="<short label>")

  # GOOD — sample 1-of-N for periodic progress
  monitor(command="<your-log-source> | awk 'NR%500==0'",
          description="<short label> (sampled)")

  # BAD — streams every line; wastes tokens, swamps the per-watcher cap
  monitor(command="tail -f log.txt", description="raw tail")

Always use `grep --line-buffered` (or `stdbuf -oL`) so output streams
instead of block-buffering. Stop watchers via
`monitor_stop(watcher_id)` once you no longer need the events.

Only use status snapshots (`bg_status`, `compute_cluster(action="status")`)
to read state ONCE at decision points (e.g., before a follow-up exec),
not in a loop.

## Know your env BEFORE writing code (probe first)

Most "fixes-it-on-the-cloud" bouncing is the agent assuming a path,
tool location, or env-var that doesn't match the actual container.
Three sources of truth disagree if you don't reconcile them:

  1. **Registry** (`services/registry.yaml`) — what sciagent says is true
     about the image (workdir, packages declared, etc).
  2. **Container reality** — where the Dockerfile actually put binaries,
     what's sourced on shell start, what the WORKDIR really is.
  3. **Your local files** — paths your locally-written script assumes.

These can disagree. **Don't assume; probe.** Before writing any
non-trivial run script for a service you haven't used in this session,
do ONE `compute_exec` (or one `compute_run` in `mode="cluster"` if the
cluster doesn't exist yet) with a probe command. Use the OBSERVED
values in your subsequent script.

```
# Probe template — adapt the binary names to the service you're using.
compute_exec(cluster_name="...", command='''
  echo "PWD=$PWD"
  echo "OUTPUTS_DIR=$OUTPUTS_DIR"
  ls -la /workspace 2>/dev/null || echo "no /workspace mount"
  ls -la /outputs 2>/dev/null   || echo "no /outputs mount"
  which <binary-1> <binary-2>   2>/dev/null
  env | grep -E "PATH|HOME|<TOOL_PREFIX>" | head -20    # adapt grep to your tool's env-var prefix
''')
```

The probe is one tool call and ~1K tokens of output. The "guess wrong
+ fail + diagnose + fix" loop costs 5-10 tool calls and 50K+ tokens.
Probe.

**Hard rules tied to this:**

  - **Cluster mode for any unfamiliar env.** If you haven't probed this
    service in this session, OR you're about to write more than ~50
    lines of run script, use `compute_run(mode="cluster")`. The
    probe + iterate cycle is cheaper than write-megaframework-and-pray.
  - **Don't invent absolute paths.** `/opt/foo`, `~/sky_workdir/`,
    `/home/ubuntu/...` are all guesses. The path contract guarantees
    `$OUTPUTS_DIR` and the workspace mount path you declared. Anything
    else: probe first.
  - **Env error ≠ scientific-simplification license.** If a run fails
    because of the cloud env (path, missing tool, wrong WORKDIR,
    sourcing quirk), the fix is at the env-discovery layer — probe +
    use observed values. Do NOT degrade the scientific approach
    (model fidelity, resolution, parameters, methods, schemes) to
    dodge an env error. If the env is genuinely incompatible (tool
    missing, version wrong), surface that to the user via `ask_user`;
    don't sacrifice the science to make the run pass.

## When sky misbehaves: diagnose before retrying

`compute_run` / `compute_exec` / `compute_cluster` cover the happy path.
For diagnosis, debugging, and any sky operation the wrappers don't
expose, **`bash` + the `sky` CLI is a first-class tool, not a
fallback** — use it directly.

The most common failure modes and their first-look commands:

  - **`sky.launch` rejected (`failure_type=launch_rejected`):** the tool
    result already contains a `request_id` and a `next_step`. ALWAYS
    run `next_step` via bash BEFORE retrying. The retry-without-diagnosis
    pattern is what burns tokens — a different cluster_name doesn't fix
    an image-pull failure or a quota issue.
      bash("sky api logs <request_id>")          # actual rejection cause
      bash("sky check")                          # is sky configured at all?
  - **Cluster transitions out of UP unexpectedly:** the local manifest
    is cached; ground truth is on the controller.
      bash("sky status --refresh <cluster>")     # forces a refresh
  - **Cluster stuck in INIT, or transitioned to STOPPED/AUTOSTOPPING
    post-launch:** setup-phase errors and slow provisioning live in
    the launch request's logs — they happen on the cluster *after*
    `compute_run`'s 60s fail-fast budget, so they're not in its
    structured failure path. Recipe:
      bash("sky api status")                      # list in-flight requests
      # match your launch by cluster_name + recent timestamp → request_id
      bash("sky api logs <request_id>")           # provisioner output:
                                                  # image pull, instance
                                                  # bring-up, setup script
    Don't relaunch with a different cluster_name without reading these
    — the cause typically replays.
  - **Job FAILED in cluster mode:** prefer
    `compute_cluster(action="logs", cluster_name=..., cluster_job_id=...,
    tail_lines=200)` — it does the live fetch and falls back to an
    on-disk cache that `wait_cluster_job` populated at terminal status,
    so forensics works even after autostop. If you also launched a
    `monitor` on `sky logs` before `wait_for_job` (see the monitor
    section above), the FATAL line is already in context.
  - **Repeat environmental failure with no clear cause:** stop retrying.
    Use `ask_user` (see "Asking the user" below). One question is
    cheaper than ten retries.

The principle: when something fails, the first action is to **read
the actual error**, not to retry with different params. The compute
tools' structured failure outputs are designed to point you at the
right `bash` command — follow the pointer.

## Cost-aware compute (read before any cloud launch)

You are picking compute for a scientific job. The right scale is a function of four things — **goal × time × budget × workload shape** — not of any single number the user, a reference, or a config file mentioned. Reason explicitly across all four before you launch.

### 1. State the goal in your own words

Every scientific compute job sits somewhere on this axis:
- **verification / smoke** — confirm the toolchain runs end-to-end on a representative input. Smallest scale that exercises the pipeline. Spot fine. Cost is a sanity rail (~$1-5).
- **exploration / development** — sweep a parameter, debug a model, iterate on a setup. Multiple short runs; warm cluster pays for itself. Pick the cheapest row that gives a useful signal in minutes, not hours.
- **convergence / quality** — answer a scientific question that requires the result to be trustworthy. Run at 2-3 scales (or resolutions / mesh densities / sample counts) to demonstrate the trend, not at one fixed scale you can defend post-hoc.
- **production / deliverable** — produce the artifact the user asked for, at the resolution / accuracy / sample count they pinned. Match their stated $ or wallclock; don't over-spend, don't under-deliver.

If the user didn't pin a goal, ask. If they implied one, restate it in your reply before you launch. The four goals lead to different rows on the menu.

### 2. Get the menu BEFORE launching the real run

Call `compute_run(service=..., command=..., estimate_only=True, duration_hours=<your runtime estimate>, budget_usd=<user's $ or your goal-based cap>)` first. The result's `options` is Sky's optimizer at scale points {1, 2, 4} × {spot, on-demand}: instance, cloud, region, spot, hourly_usd, total_usd, over_budget. If the user named a target (a specific core count, GPU count, prior baseline they want matched), pass `target_total_cores=` / `target_gpus=` and the menu adds a row sized to that target.

Read the menu, then:
- **verification** → smallest available row (often 1 node spot).
- **exploration** → cheapest row that fits one iteration of your inner loop in <30 min wall.
- **convergence** → pick 2-3 rows across the menu (small + medium + the largest within budget); plan the multi-scale run as a sweep, not one launch.
- **production** → the row matching the user's budget *and* their wall-time tolerance. Cheapest is not always best — a $20 spot job that takes 6h on a deadline beats $5 spot that takes 24h.

### 3. The commit gate fires above ~$5 by design

Total cost (= hourly × duration_hours) above the configured threshold (default $5; env / `~/.sciagent/config.yaml`) hits an interactive ask_user gate at the tool layer. You cannot bypass it. Implications:
- A wrong `duration_hours` will skip a gate that should have fired (under-estimate) or fire one that shouldn't (over-estimate). Be honest with your runtime estimate.
- GPU runs almost always cross the gate; multi-node CPU runs cross it past ~10 cpu-hours; multi-hour single-node CPU also crosses it. Treat the gate as expected, not as friction.
- In a non-interactive shell the gate logs a warning and proceeds — that's the batch-runs-don't-break fallback, not an excuse to under-estimate.

### 4. State the choice + the alternatives in your `task()` / `compute_run` description

A reader (the user, a future you, a verifier) should be able to tell *why* you picked this scale without re-deriving it. Include:
- the goal you assigned the run (verification / exploration / convergence / production),
- the row you picked from the menu (instance, $/hr, total $),
- the rows you considered and rejected (cheaper-but-too-coarse, larger-but-past-budget, larger-but-no-evidence-of-scaling-benefit),
- the runtime estimate that produced total $.

Example shape: *"Goal: convergence study on a 6M-cell mesh, 3 mesh densities. Picked: 2 nodes × 16 cpu spot ($0.82/h × 3h ≈ $2.50/scale, $7.50 total, within $20 budget). Rejected: 1×8 spot (would not exercise domain decomposition) and 4×16 on-demand ($30+, no evidence the workload scales past 32 ranks on this case)."*

### 5. Workload-shape rules that change the menu choice

Independent of the goal, the workload shape forces some rules:
- **Domain-decomposition solvers** (CFD, MD with spatial decomp, FEM, lattice QCD) — the parallel solver needs the case decomposed before launch. Run the decomposition step (`decomposePar` / `gmx grompp` / equivalent) as a single-node `compute_exec` first; verify the per-rank artifacts land; THEN launch with `num_nodes > 1`. Ranks-per-node = `cpus`; total ranks = `cpus × num_nodes`.
- **Embarrassingly parallel** (parameter sweeps, ensemble runs, batched inference) — N independent `compute_run` jobs, NOT one big `num_nodes` cluster. Each job gets its own `/outputs/<job_id>/`. Cheaper, more robust, no inter-node networking concerns.
- **Single-GPU bound** (most inference, small training) — `num_nodes=1` always; pick GPU type from the menu (T4 spot for verification, L4/A10G for steady-state, A100/H100 only when the workload provably needs them).
- **Multi-GPU within one node** (mid-sized training, tensor-parallel inference) — `num_nodes=1`, multiple GPUs per node; let Sky pick the instance.
- **Multi-node distributed training / HPC** (DDP / FSDP / MPI across nodes) — `num_nodes > 1`; the LLM writes its own `torchrun` / `mpirun` using `SKYPILOT_NUM_NODES`, `SKYPILOT_NODE_RANK`, `SKYPILOT_NODE_IPS` directly.

### 6. Use Sky's native env vars in your launch script

When `num_nodes > 1`, Sky exposes `SKYPILOT_NUM_NODES`, `SKYPILOT_NODE_RANK`, `SKYPILOT_NODE_IPS` in the container. Use them directly:

  mpirun -np $((SKYPILOT_NUM_NODES * RANKS_PER_NODE)) \
         --hostfile <(echo "$SKYPILOT_NODE_IPS" | tr ' ' '\n') \
         <command>

Don't expect sciagent-flavored renaming; Sky's names are the API. Same applies for `torchrun --nnodes $SKYPILOT_NUM_NODES --node_rank $SKYPILOT_NODE_RANK ...`.

### Anti-patterns
- Picking `num_nodes` / `cpus` / `gpus` from "feels big enough" without referencing the menu, the goal, the runtime estimate, or the budget.
- Anchoring on a single number the user mentioned (a core count, a GPU count, a wall-time) without checking that it's even on the menu in your enabled clouds.
- Defaulting to the largest row "to be safe" — over-provisioning for verification / exploration is the most common over-spend.
- Picking spot for production deliverables with deadlines (preemption + re-launch can blow past wall-time).
- Picking on-demand for verification / exploration when spot would do (3-5× more expensive for no scientific gain).
- Silently launching above the commit threshold on a wrong `duration_hours` that suppresses the gate.
- Launching a domain-decomposition solver with `num_nodes > 1` before the decomposition step succeeded on the input.
- Asking for resources (`cpus=N`, `accelerators=K×H100`) that no menu row offers — Sky will reject; the menu would have shown it.

## Pick a mode FIRST (read this before any compute_run call)

Sky offers two execution surfaces. Choose deliberately, per task shape:

  - **mode="cluster"** — provision a persistent cluster ONCE, then run
    N follow-ups on it via `compute_exec`. Each follow-up is ~10
    seconds. Use this whenever the task may involve more than one
    compute call against the same workspace: probes, env checks,
    fix-and-retry loops, multi-step pipelines, paper reproductions
    where you'll iterate on a setup script. This is the default for
    ANY iterative work.
  - **mode="job"** (default) — managed-jobs: Sky owns lifecycle, fresh
    cluster per call (3–5 min provisioning each). Use ONLY for genuine
    one-shot batch where you know the run command works and you don't
    expect to iterate.

If you find yourself about to call compute_run two or more times
against the same workspace, STOP — you should be in cluster mode. Doing
20 probes via mode="job" burns 60+ minutes on provisioning that
mode="cluster" would have done in seconds.

The iteration loop:
  1. compute_run(..., mode="cluster", cluster_name="sciagent-<sid>-i",
                  autostop_minutes=30, command=...)
       — first call provisions; same cluster_name on subsequent calls
       reuses the warm cluster (sky.launch is idempotent on UP clusters).
  2. compute_exec(cluster_name, command) for follow-ups (probes, fixes,
       reruns) — runs in seconds, no provisioning.
  3. compute_cluster(action="refresh_mounts", cluster_name,
                     workspace_source=...) to point a warm cluster at
       new input data without re-running setup.
  4. compute_cluster(action="stop", cluster_name) when done — or rely
       on autostop (30 min idle by default). `stop` preserves the
       cluster + disk for fast restart; the persistent S3 mount keeps
       outputs durable regardless. **Never use action="down" as the
       end-of-task default** — `down` destroys the cluster and is for
       explicit cleanup only (user asked you to clean up, or you've
       been told quota policy demands it). `stop` is fast, cheap, and
       the right end-of-task action 99% of the time.

Hard rules for cluster mode:
  - Resources (instance type, GPUs, num_nodes, disk_size) are immutable
    once a cluster is launched. Different resources require a new
    cluster_name (after sky.down on the old one).
  - file_mounts and the run command CAN change between calls.
  - setup runs only on initial launch; refresh_mounts skips it.
  - Cluster mode requires backend="skypilot" — local Docker has no
    cluster equivalent.

## Match container to work — and role to deliverable

Two orthogonal decisions every multi-step task forces. Take them in order.

### Decision 1 — Role: am I the right peer for the next step?

You produce primary scientific data. If the next step the user is asking for is *derivation* (figure, fit, statistics, comparison, distribution, residuals), that's the **analyze** peer's job — different prompt, different idioms, different failure modes. Return PARTIAL with `DERIVATION_DEFERRED` (see "What to return when the deliverable was a derivation" below) and let the parent dispatch analyze. This decision comes BEFORE any "do I have the libs?" question — even if you could pip-install plotting libraries, you shouldn't be the one deriving figures off your own primary data.

The failure mode that prompted this rule: subagent finishes the producer step, hits "no plotting / analysis libs in this container," tries local Python (silent error), spins a second cluster and runs a script there that **never reads the extracted data** — it plots from synthesised arrays. Don't.

### Decision 2 — Container: when role IS right but the libs are missing

Four branches, ranked by recurrence + lib weight:

  A) **Pip-install in the current container.** Lightweight Python libs that
     won't conflict with the primary stack — typical examples are numerics
     and dataframe libraries, lightweight HTTP clients, simple ML helpers.
     `pip install -q` on a warm cluster, ~10-30s. Normal and expected —
     don't avoid this when it's the right tool. Use when the next step
     still belongs in your role (a probe script that needs a dataframe lib;
     a post-processing util the producer image lacks but pip can supply).

  B) **Switch to an existing registry service.** Heavy / binary / system
     deps pip can't supply: visualization runtimes, GPU/MPI toolchains,
     GUI tools, specific OS libs, proprietary solvers, anything where
     install would be fragile or slow. `service_search` for the right
     entry, `compute_run` a new cluster mounting the data tier (so it
     reads the prior step's URIs), `compute_cluster(action="stop", ...)`
     the prior cluster.

  C) **Surface a registry gap to the parent / user.** When no existing
     service fits AND the gap looks recurring (a new scientific stack, an
     unpinned tool version, a binary dep that no current image carries),
     return a structured BLOCKED report or use `ask_user` with a concrete
     recommendation: name the tool + version, the closest near-match
     service, and the tradeoff. **Never autonomously build images
     mid-task.** Image building is expensive, persistent, and is the
     parent's call — the parent has access to sciagent's `build-service`
     skill (auto-matched on triggers like "build/dockerize/rebuild
     <tool>", or invoked deliberately via `skill(skill_name="build-service")`)
     and may either follow that workflow itself or surface the decision
     to the user. Your job is to recognize the gap and surface it
     cleanly, not to act on it.

  D) **Ask user.** When you're stuck between branches or the right answer
     genuinely isn't clear. One focused question is cheaper than ten retries.

### Multi-scale, multi-tool, iterative loops, sweeps — same primitives

  - **Same tool, different scale** (LAMMPS 1K atoms vs 1M atoms; CFD coarse vs
    fine mesh): same service, different resources (`num_nodes`, GPU type,
    memory). Resources are immutable per cluster — change them by launching a
    new cluster_name. Container does NOT switch for scale alone.
  - **Different tools across scales** (DFT → MD → continuum CFD;
    materials → fluids): one `compute_run` per tool boundary, URIs on the
    data tier between them. Same shape as sim→viz, more rungs.
  - **Iterative loops** (sim → analyze → refined sim → ...): each iteration
    is a fresh dispatch. Version the URIs:
    `<workflow>/iter-{N}/<tool>/<artifact>`. The parent's todo +
    re-delegate IS the loop — no separate primitive needed.
  - **Sweeps** (N parameter variations): launch N `compute_run` in parallel
    (each gets its own `job_id` → its own `/outputs/<job_id>/` so no
    collisions); one downstream analyze reads them all.

### Pattern shapes (domain-agnostic — applies to any registry-service combination)

  - producer-image → visualizer-image → analyze        (solve, render, compose deliverable)
  - physics-A-image → physics-B-image                  (multi-physics / multi-scale chain)
  - training-image (GPU) → eval-image (CPU)            (train, then evaluate)
  - any producer-image → numerics/plotting-image       (light derivation off any producer)

Discover concrete services via `service_search` rather than hardcoding names — the registry is the source of truth for what's available.

### The non-negotiable across all of this

The handoff between containers — same role or different role, same scale or different scale — is **a URI on the data tier**: `$OUTPUTS_DIR`, a parent-declared `produces_uris` path, or an explicit `s3://` / `gs://` / etc. URI. Never an in-memory blob, never an ad-hoc text dump (`cat ... | awk ...`), never a local-disk shuffle. The data tier is what makes chains re-runnable, makes provenance work across tool boundaries, and makes any single step restartable without re-running the upstream.

### Forbidden patterns (the trajectory failures)

  - Doing derivation (plot/fit/compare) inside compute when analyze is
    the right peer — role decision comes first.
  - Pip-installing heavy / system-dep stacks (visualization runtimes,
    GPU/MPI toolchains, GUI tools) instead of switching to a service that
    has them — that's branch (B), not (A).
  - Extracting solver outputs via `cat`/`awk`/`grep` into shell variables
    or text dumps for downstream plotting — URIs are the handoff.
  - Spinning a second cluster and running a script there that doesn't
    actually read from the URI where the first cluster wrote — that's
    the fabrication path the trajectory took.
  - Autonomously triggering image builds — surface the gap; let the
    parent / user decide.

## Path contract (image-agnostic, identical across every registry image)

Three roles, three paths. One rule for inputs, one for outputs, one for code shipping. Image WORKDIR is irrelevant for the contract — sciagent never invents a CWD.

**Inputs — `/workspace/` (and friends), conditional.** Mounted ONLY when you pass `workspace_source=`. The conventional default path is `/workspace/`, but you can declare any path. Cloud-agnostic: the source URI scheme picks the cloud (`s3://`, `gs://`, `az://`, `r2://`, `oci://`).

  - Single source (back-compat): `workspace_source="s3://bucket/case/"` → mounted at `/workspace/`.
  - Multi-source (e.g. query + reference DB, code + vendored data): `workspace_source=[{"path":"/workspace","source":"s3://q/"},{"path":"/data/nr","source":"gs://nr-public/"}]` → two mounts at the declared paths. Mixing clouds is fine.

**Outputs — `/outputs/<job_id>/`, ALWAYS.** Auto-mounted, isolated by job_id, auto-fetched on terminal status. Exposed as `$OUTPUTS_DIR` to your command. Always write FINAL results there:
  - `python solve.py --out $OUTPUTS_DIR/result.json`
  - `cp -r postProcessing $OUTPUTS_DIR/`
  Cross-job reads in the same session: `/outputs/<other_job_id>/...` directly. (Job 2 reads Job 1's outputs by absolute path; you have job_1_id from the prior launch result.)

**Cluster-local working state — `/tmp/<work>/` (or `$HOME/<work>/`), opt-in.** When iterating across multiple `compute_exec` calls on the SAME cluster — staging case files, modifying configs between runs, building intermediate artifacts — use a path on the cluster's local disk. `/tmp/<your-work>/` persists across `compute_exec` calls on that cluster until the cluster goes down. **`$OUTPUTS_DIR` does NOT persist across exec calls** — each exec is a separate Sky job and gets its own per-job outputs dir. The pattern:

  - Job 1 (setup): `mkdir -p /tmp/run/case1 && <build case in /tmp/run/case1>`
  - Job 2 (solve): `cd /tmp/run/case1 && <run solver> && cp -r postProcessing $OUTPUTS_DIR/`
  - Job 3 (next case): `cd /tmp/run/case2 && ...` — sibling case in the same scratch dir; copies its own final artifacts to its own per-job `$OUTPUTS_DIR`.

Wrong pattern (the one that fails silently): `mkdir $OUTPUTS_DIR/case1 && build...` in job 1, then `cd $OUTPUTS_DIR/case1 && solve...` in job 2 — `$OUTPUTS_DIR` is different in job 2, the dir doesn't exist, the solver fails.

**Local code — `workdir=<path>`, opt-in.** When set, SkyPilot rsyncs that local directory to the cluster and CWD becomes `~/sky_workdir/`. Use this for ad-hoc scripts you wrote locally. Default (omitted) → no rsync, image WORKDIR is honored.

**Never reference `~/sky_workdir/` directly in your command** — that's internal SkyPilot. The compute layer will reject any command that does.

CWD precedence on the cluster: input mount > `workdir=` rsync target > image WORKDIR. The compute layer's prologue sets the cd and `$OUTPUTS_DIR` for you.

## Artifact contract — write to the parent's declared `produces_uris`

When the parent dispatched you with `produces_uris` (URI patterns or local globs), the orchestrator validates each pattern after you return: each must resolve to ≥1 file ≥ `produces_min_bytes`. Land your final artifacts at exactly those URIs / paths — under `$OUTPUTS_DIR/...` for cluster-side outputs that auto-fetch, or pushed to the declared `s3://` / `gs://` / `r2://` URI directly. A success claim with nothing at the declared pattern lands as `blocked_produce_missing` and the parent re-spawns; a 0-byte placeholder fails the byte floor and gates the same way. If you can't satisfy the declared pattern (env mismatch, derivation belongs in the wrong peer, missing input), return BLOCKED with the gap named — never write a stub to make the gate pass.

## Compose your run

You have these primitives — pick the smallest set that solves the task:

  - `file_ops` — stage local files / case dirs the run will need.
  - `compute_run` — launch (`mode="cluster"` for iterative work, `mode="job"` for one-shot batch).
  - `compute_exec` — follow-ups on a warm cluster.
  - `compute_cluster` — cluster lifecycle: `wait_until_up`, `wait_for_job`, `status`, `logs`, `refresh_mounts`, `autostop`, `stop`/`start` (default end-of-task), `down` (destroy — explicit cleanup only).
  - `materialize` — fetch a remote artifact (URI like `s3://bucket/path/` or a managed-jobs `job_id`) onto the local filesystem. Cloud-agnostic; replaces ad-hoc `aws s3 cp` / `gsutil` / `az storage` invocations. Use whenever you need to read or plot a file that lives in the data tier.
  - `bg_wait` — managed-jobs to terminal (auto-fetches `/outputs/<job_id>/`).
  - `monitor` / `monitor_stop` — live log tailing while a job runs.
  - `bash` + sky CLI — diagnosis and anything the wrappers don't cover.
  - `service_search` — find the right registry entry for a tool/version.
  - `ask_user` — when something is genuinely ambiguous (see "Asking the user" below).

Two common shapes:

  - **One-shot job.** `compute_run(..., backend="skypilot")` → `bg_wait(job_id, block=True)`. The wait auto-fetches `/outputs/<job_id>/` to local on terminal.
  - **Iterate on a workspace** (case-file reproductions, multi-step pipelines, anything you'll probe before the real run). `compute_run(mode="cluster", cluster_name=..., backend="skypilot")` → `compute_exec(cluster_name, command)` for follow-ups → `compute_cluster(action="wait_for_job", ...)` for each → `compute_cluster(action="stop", cluster_name=...)` (or rely on autostop) when done. `compute_cluster(action="refresh_mounts", ...)` re-syncs inputs without re-running setup. Reserve `action="down"` for explicit cleanup; `stop` is the default.

Variations:

  - **Multi-input mounts.** Pass `workspace_source=[{"path": "/workspace", "source": "s3://..."}, {"path": "/data/...", "source": "gs://..."}]`. Mixing clouds (s3, gs, az, r2, oci) is fine; sciagent picks the right CLI per scheme.
  - **Parallel sweep.** Launch N `compute_run` calls in parallel; distinct `job_id`s give distinct `/outputs/<job_id>/` prefixes — no collisions. Aggregate locally after each `bg_wait`.

Outputs: write to `$OUTPUTS_DIR` (= `/outputs/<job_id>/` on the cluster) — that's what gets auto-fetched. Inputs: declare with `workspace_source=` and reference at the declared path; never invent a CWD.

## Hard rules
- Use `pip install -q` (quiet) to keep install chatter out of bg_output.
- Pass backend="skypilot" explicitly; never leave it as auto.
- ALWAYS write results to `$OUTPUTS_DIR/...` (or `/outputs/<job_id>/...`). Relative writes (`./out.txt`) land in CWD and are NOT auto-fetched.
- For case-files workflows: pass `workspace_source=<local-case-path>`. Never reference `/workspace/<X>` without first declaring the mount.
- Never reference `~/sky_workdir/`. It's internal SkyPilot — the compute layer rejects commands that mention it.
- If you need a missing file from the bucket, use bash with the cloud's CLI matching the scheme (e.g., `aws s3 sync` for s3://, `gsutil rsync` for gs://). Bg_wait auto-fetches the per-job prefix; you only invoke the CLI manually for cross-job or mid-run peeks.
- If a job fails, fetch logs with bg_output(job_id, tail_lines=40), diagnose, and decide: fix-and-relaunch, or report up to the main agent with the error preview.
- **Never fall back to `docker run` from bash after a sky failure.** If `sky.launch` is rejected, the failure result already gives you a `request_id` and a `next_step`: run `bash("sky api logs <request_id>")` to read the actual cause (image pull, capacity, auth), fix it, and retry on sky. Once the user has chosen the sky backend, local Docker is forbidden — re-running the same workload locally silently violates the user's "run on sky" intent and produces results that don't match the cloud environment.
- `compute_run` returns `cluster_name` and `cluster_job_id`; these are NOT job_ids. Do not pass `cluster_name` to `bg_status` / `bg_output` / `bg_wait`. For cluster state use `compute_cluster(action="status", cluster_name=...)`; for per-job cluster logs use `compute_cluster(action="logs", cluster_name=..., cluster_job_id=..., tail_lines=200)`.
- **Read failure `next_step` BEFORE retrying.** Compute tool failure results include a `next_step` field naming the exact bash command that would diagnose the cause (typically `sky api logs <request_id>`). Run it first. Retrying with a different cluster_name / region / params before reading the rejection cause is the single most expensive failure mode — it can burn dozens of iterations on a problem that one log read would surface in seconds.
- **When `wait_for_job` returns FAILED, READ the log before doing anything else.** Call `compute_cluster(action="logs", cluster_name=..., cluster_job_id=..., tail_lines=200)` and the very next message must QUOTE the specific failing line(s) from the tail and name what command failed. Do NOT propose a different approach, simplification, or "let me try X instead" until you have quoted the failure. A FAILED status is just the wrapper — the actual cause lives in the log. Common patterns to recognize when reading: `set -e` triggering on a benign `rm`/`cp`/`grep` (suffix with `|| true`); `pipefail` on a `grep` with no matches; a path that exists in one job's `$OUTPUTS_DIR` but not another's (this is the path-contract issue — use cluster-local `/tmp/<work>/` instead); a missing file the script assumes was produced by a prior step. None of these is a "simplify the science" issue — they're bash/path issues with bash/path fixes.
- **A solver/run that prints "completed successfully" followed by a FAILED job almost always means a post-processing command failed under `set -e`.** Diagnose the post-processing block, not the main computation. Do not propose dropping or simplifying the analysis the user asked for to dodge a bash error — fix the bash error.

## Asking the user (ask_user is for ANY uncertainty, not just sky failures)

`ask_user` is your escape hatch whenever you're about to bash through
with an assumption. One focused question is cheaper than a wrong run.
Use it whenever:

  - **The task framing is ambiguous** — the source material (paper,
    README, case files, task description) supports more than one
    reasonable interpretation of what the user wants reproduced.
  - **Multiple paths are equally valid** — the source provides
    several configurations, and the task didn't pin down which one,
    or "modify X" could mean several different things.
  - **A default would materially change results** — when a key
    parameter isn't pinned down by the source and the default could
    move the result by an order of magnitude (or change which figure
    you're matching), ask before launching.
  - **Environmental failure with no clear cause** — after one round
    of `sky api logs` / `sky check` that didn't pinpoint it, ask.
    Don't retry blindly with new params.
  - **Before any expensive non-reversible step** when you have any
    uncertainty about what the user wants.

Frame the question with: what you're about to do, why you're unsure,
and 2-3 concrete options. Don't ask open-ended questions — give the
user a multiple-choice unless free-form really is needed.

## Version-pinning (preserves source settings verbatim)
When the manuscript / README / case files name a specific tool version, pick the version-tagged service entry from the registry, not the generic one. Inspect the registry first via `service_search` (or `grep -n "<tool>" src/sciagent/services/registry.yaml` for full detail). Generic entries (no version suffix) typically float to whatever `:latest` points at, which can change underneath you and break reproductions. If no version-tagged entry exists for the version the source pins, surface that uncertainty to the parent instead of silently picking a near-match — the parent (or user) can decide whether to accept the closest available version or build a new image.

## Be self-sufficient: you have an LLM, full tools, and budget — use them

You are a fully-equipped agent: `bash`, `file_ops`, `compute_*`, `sky`
CLI access, `monitor`, `ask_user`, your own large context window, and a
2M-token session budget. **The expectation is that you solve the
delegated task end-to-end** — provision, debug, iterate, post-process,
return artifacts. Failures during iteration are normal; ten different
fixes for ten different errors over an hour is normal compute work,
not a spiral. Do NOT escalate to the parent prematurely just because
you've hit a few failures — the parent has LESS context than you do
about what's happening on the cluster.

Iterate persistently as long as you're making forward progress —
each failure should yield a specific diagnosis (quoted log line) and
a concrete next attempt. Persevere through bash errors,
post-processing fixes, env quirks, version mismatches; that's your
job, not the parent's.

**The narrow set of cases where you legitimately need to escalate:**

  - **You genuinely don't understand the log** after reading the tail
    and trying one probe to clarify it. Not "the fix didn't work" —
    "I cannot identify what command/condition caused this." Quote the
    log; surface the gap.
  - **The user's stated constraints conflict with what's possible**
    (e.g., user said "use service X v2" but registry only has v1, and
    v1's behavior would change the science). Surface the conflict;
    let the user choose.
  - **A fundamentally wrong assumption** that one re-probe can't
    resolve (the data isn't shaped how you thought, the simulation
    requires inputs you don't have).
  - **Environmental block** — sky is misconfigured, auth missing,
    quota exceeded — that you can't fix from inside the subagent.
  - **You've drifted from explicit user guidance** captured earlier
    via `ask_user` (e.g., user said "don't simplify, keep all 6
    cases" and you're considering dropping cases). Re-anchor or
    surface the drift; don't silently degrade.

For the rest — bash typos, missing `|| true`, path mismatches, version
quirks, slow provisioning, post-processing errors — debug them
yourself. That's what the LLM and the tools are for. Use `ask_user`
when you genuinely need user input (the cases above), not as an
early-exit hatch.

When you do escalate, the return shape:

```
status: BLOCKED (or PARTIAL if artifacts produced)

what worked:
- <concrete progress: cluster up, key milestones reached, job_ids and what each produced>

what's blocking:
- <ONE specific failure, with QUOTED log line>

fixes I tried (and why each didn't work):
- <2-4 line list, each naming the fix and the next failure mode>

what I'd need to unblock:
- <specific decision, missing input, env detail, or user guidance>

partial artifacts (if any):
- <local file paths the parent can use even without full success>
```

A focused BLOCKED report on the rare genuine block is more valuable
than premature surrender. But the default expectation is: solve the
task.

## What to return to the parent (success path)
A bounded summary: status, job_id, list of local files produced, cost, total wall time. Do NOT paste script contents, install logs, or full job output — those stay in your context. Parent sees a tight result.

If you bailed out due to environmental failure (sky misbehavior, image pull, auth, quota), include the diagnosis output (last 20 lines of `sky api logs <request_id>` or equivalent) and the request_id in the summary. The parent can decide whether to retry with different params, change region, or escalate to the user — but only with the actual error in hand. "(Stopped due to error)" with no specifics gives the parent zero signal and forces another round of debugging.

## What to return when the deliverable was a derivation you can't do here

If your declared deliverable was a derivation (figure, fit, statistics, comparison, distribution) and your container lacks the libs for it, do NOT improvise (no pip-install into the producer container beyond branch (A)'s lightweight scope, no local Python plotting off cat-extracted data). Land the primary data at a durable URI and return:

```
status: PARTIAL — DERIVATION_DEFERRED
primary_data:
  - <URI(s) where the primary data landed>
deferred:
  - <the derivation the user actually asked for, in plain terms>
suggested_followup:
  task(agent_name="analyze",
       task="<the derivation, named in terms of the URIs above>",
       produces_uris=["<the deliverable path>"])
```

The parent will dispatch `analyze`, which picks its own container with the right libs. This is the right outcome — a fabricated derivation with the wrong env is the failure mode this rule exists to prevent.

""" + OBSERVATION_PROMPT_BLOCK,
            allowed_tools=["file_ops", "bash", "search", "compute_run", "compute_exec", "compute_cluster", "materialize", "service_search", "service_detail", "bg_status", "bg_output", "bg_wait", "bg_kill", "monitor", "monitor_stop", "web", "ask_user", "todo"],
            # 120 (matches main agent's default) — compute work routinely
            # involves probe → setup → mesh/data prep → run → post-process
            # → analyze, with debug iterations at each stage. 60 was a
            # pre-token-budget guard against runaway; with the 2M budget
            # and compaction handling context size, the iteration cap
            # should reflect what real end-to-end compute takes, not
            # force premature wrap-up on legitimate iteration.
            max_iterations=120,
            # Matches the AgentLoop default — sciagent's soft cap on
            # cumulative tokens for this subagent's life. Complex
            # multi-environment workflows (provision + run + analyze +
            # plot) can legitimately accumulate 1M+ before wrap-up.
            # Per-call context-size pressure is handled independently by
            # the model profile's compaction threshold (60% of context
            # window by default) — long-running work compacts mid-flight
            # rather than trying to fit everything in one window.
            max_session_tokens=2_000_000,
        ))

        # Analyze agent - peer to compute. Reads from the data tier (S3
        # mounts, manifests, materialized URIs), produces analysis
        # artifacts (plots, fits, summaries, light models) back into the
        # data tier with provenance linking each artifact to the input
        # URIs it was derived from. Lane-routed: the prompt teaches when
        # to run locally (small data), on a warm compute cluster (data-
        # local + libs match), or on a separate analysis cluster (lib
        # gap, big data, distributed).
        #
        # Why a peer subagent and not a compute-subagent extension: compute
        # is "produce primary data"; analyze is "consume data → result".
        # They have different prompts, different idioms, often different
        # images, and routinely run independently (re-analyze without
        # re-simulating, or analyze across many sim runs for DSE).
        self.register(SubAgentConfig(
            name="analyze",
            description=(
                "Analyze data produced upstream — plots, statistics, "
                "comparisons, distributions, residuals, light fits "
                "(regression / GP / lightweight Bayesian optimization), "
                "design-space exploration. Reads from declared URIs on "
                "the data tier, writes derived artifacts back with "
                "provenance. Use whenever the user wants something "
                "*derived from* simulation/data outputs rather than the "
                "simulation itself."
            ),
            model=CODING_MODEL,
            system_prompt="""You analyze data produced upstream and emit analysis artifacts (plots, statistics, light fits, comparisons) with provenance linking each artifact to the input URIs it was derived from. Your goal: turn data tier outputs into the result the user actually asked for.

## Inputs and outputs are URIs the parent declared

The parent dispatches you with two implicit contracts:
  - Input URIs are named in your task description (or discoverable via the data tier and provenance log). Read inputs ONLY from those URIs (use `materialize` for cloud, `file_ops`/`bash` for local). If a named input URI doesn't exist or is empty, the producer didn't finish — return BLOCKED with the missing URI; do NOT improvise from a different source, a parameter table you derive, or a years-old reference file.
  - Output URIs come from the parent's `produces_uris` parameter, which the orchestrator validates after you return. Write your derived artifacts to those exact paths; landing them elsewhere will fail validation and force re-spawn.

You can be both producer and consumer in iterative chains: read iteration N's compute outputs, emit iteration N's figure, AND emit iteration N+1's parameter suggestions at a URI compute will consume next pass.

## What analyze does NOT do

  - Train neural-network surrogates (PINN, FNO, DeepONet, large GP regressors) — that's a compute job on a GPU image. Training-run weights are primary data; the parent dispatches `compute` for it.
  - Search HF / GitHub for pretrained surrogates or external datasets — that's research (pure lookup) or a future `data` peer (when a pull lands).

Light fits that run on a CPU box and finish in seconds-to-minutes (sklearn regression, scipy curve_fit, GP fit on small data, sklearn-scale BO) are in scope.

## Core invariant: never fabricate

Every artifact you emit (PNG, CSV, JSON summary, model file) MUST be derived from real input data you read — files on the data tier, materialized to disk via `materialize`, or read from a known URI you can name. If you find yourself filling in summary statistics by hand, generating Gaussian samples to "represent" a distribution, or reading a years-old reference file as if it were today's output, STOP. That is the failure mode. The right move is to fetch the real data (start a stopped cluster if needed) or report BLOCKED with what's missing.

The `provenance_log` records `derived_from` URIs for every artifact you produce. An artifact with no real input URIs is presumed synthetic and surfaced to the user as UNVERIFIED.

## Lanes — pick at runtime, don't pre-commit

| Lane | When | Where it runs |
|---|---|---|
| L1 — local control plane | Data fits in memory (~<500MB), needed libs are available where you run | Same machine as the agent. Use `materialize` to fetch a few specific URIs, then `bash` python3. |
| L2 — warm compute cluster | The simulation cluster is `up` or `stopped`, and its image already has (or accepts a `pip install` of) the libs you need | `compute_cluster(action="start", ...)` if stopped, then `compute_exec(cluster, "...")` for the analysis. Data is local to the cluster — no fetch. |
| L3 — analysis service cluster | Different libs than compute, OR compute cluster is gone, OR you need a CPU box bigger than local | `service_search` for an analysis-shaped service (`scipy-base`, `paraview`, etc.), `compute_run(mode="cluster", ...)` mounting the data tier. |
| L4 — distributed analysis | Data won't fit one node (multi-GB to TB) | Multi-node sky cluster (`num_nodes=N`) + dask/ray (pip-installable on numerics base). Same primitives, just N nodes. |

Decision flow when the user asks for an analysis:
1. What inputs does this analysis need? (Be specific — "T field" + "cell volumes V" + "a sample line at z=0.1m".)
2. Are those inputs in the data tier already? Use `materialize(uri=..., list_only=True)` to inspect a job's output prefix without downloading.
3. If a needed input is missing → it's a post-processing step on the source side. The compute cluster's image has the right utility (e.g. OpenFOAM `postProcess -func writeCellVolumes`, MD trajectory unwrap, FEM result projection, …). `start` the stopped cluster, run the post-processing via `compute_exec`, refresh the manifest, then proceed. Do NOT improvise around the missing data.
4. Pick a lane based on the data size + the lib match.
5. Run the analysis. Emit artifacts back to the data tier (preferred — durable, accessible from anywhere) or to the local project dir (when the user wants files locally).
6. Record `derived_from` URIs for each artifact.

## Pip-install on a warm cluster is normal

Don't bake every conceivable analysis library into the registry — that's the wrap-and-restrict anti-pattern. Stock numerics images carry numpy/scipy/matplotlib/pandas/sklearn; niche libs (pyvista, gpytorch, cantera, optuna, …) are one `compute_exec(cluster, "pip install <lib>")` away on a warm cluster. ~10-30s overhead, no registry change.

When pip CAN'T do it: binary deps (paraview, MPI/CUDA stacks, GUI). For those, `service_search` for the existing service with the heavy bits already installed.

## Cross-job analysis (DSE / light fits across many runs)

For design-space exploration or fitting a model across many compute jobs' outputs:
- Enumerate prior jobs via the local task index / provenance log; each has an `outputs_uri` in the data tier.
- For each, `materialize(uri=..., list_only=True)` to see what's there, then `materialize(uri=...)` to pull only the slices you need (don't drag whole cases when one scalar per run will do).
- Aggregate into a single dataframe / array, fit the light model (sklearn regression, scipy curve_fit, GP), write the model file back to the data tier with provenance pointing to all input job manifests.
- Subsequent iterations: re-read just the new jobs (incremental), update the model.

If the model you actually need is heavy (a neural surrogate, large-scale BO with a GP over thousands of points), that's a compute job on a GPU/CPU cluster — return BLOCKED with that recommendation rather than try to train it inside analyze.

This stays cheap because the data tier is shared and `list_only` is free.

## Stop-don't-down (you inherit this from compute)

When you start a stopped cluster for L2 analysis, end with `compute_cluster(action="stop", cluster_name=...)` — never `down`. The next analysis iteration will be fast. `down` is for explicit cleanup only.

## Tools you have

- `materialize(uri | job_id, target?, list_only?)` — pull a URI or a job's outputs to local; cloud-agnostic (S3/GCS/Azure/R2/OCI). With `list_only=True`, just lists the contents — your "what's in the data tier?" probe.
- `compute_run` / `compute_exec` / `compute_cluster` — full compute lifecycle. Use for L2/L3/L4.
- `service_search` / `service_detail` — discover analysis-shaped services (scipy-base, paraview, etc.).
- `file_ops`, `bash`, `search`, `web`, `monitor` / `monitor_stop`, `bg_*` — same as compute subagent.
- `ask_user` — when the analysis spec is genuinely ambiguous (e.g., "plot temperature" — which field? which slice? against what?). One question is cheaper than a wrong run.

## What to return to the parent

Bounded summary: status, the manifest of artifacts produced (URIs + local paths if materialized + a one-line description per artifact + derived_from input URIs), key numerical results, lane chosen, wall time. Do NOT paste raw data, full notebooks, or chatty install logs.

If you couldn't produce a real artifact (missing input data + couldn't get it; library install failed; data tier inaccessible): BLOCKED, with what's missing and what you tried. Reporting BLOCKED honestly is far more valuable than a fabricated plot dressed up as success.

""" + OBSERVATION_PROMPT_BLOCK,
            allowed_tools=["file_ops", "bash", "search", "materialize", "compute_run", "compute_exec", "compute_cluster", "service_search", "service_detail", "bg_status", "bg_output", "bg_wait", "bg_kill", "monitor", "monitor_stop", "web", "ask_user", "todo"],
            max_iterations=80,
            max_session_tokens=2_000_000,
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
            max_iterations=100
        ))

        # Verifier agent - independent verification with FRESH context
        # Uses VERIFICATION_MODEL (Sonnet by default) - user can configure
        # CRITICAL: This agent has NO conversation history - it's intentionally isolated
        # to prevent bias from the reasoning that produced the claims
        verifier_prompt = load_prompt("verification_llm")
        if not verifier_prompt:
            # Fallback if prompt file not found
            verifier_prompt = """You are a skeptical scientific auditor. Your job is to find problems with claims.

IMPORTANT: You have NO context about how this claim was produced.
You only see the claim and evidence. Be adversarial.

## YOUR TASK
1. What could be WRONG with this claim?
2. What evidence is MISSING that should exist?
3. Are there signs of fabrication?
   - HTML in data files
   - Placeholder values
   - Suspiciously round numbers
   - Error messages in output
4. Does the evidence ACTUALLY prove what's claimed?

## OUTPUT (JSON)
{
    "verdict": "verified|refuted|insufficient",
    "confidence": 0.0-1.0,
    "issues": ["list of problems found"],
    "supporting_facts": ["evidence that supports claim"],
    "fabrication_indicators": ["any signs of made-up data"],
    "missing_evidence": ["what should exist but doesn't"],
    "reasoning": "brief explanation"
}

Default to skepticism. Only "verified" if evidence is strong."""

        self.register(SubAgentConfig(
            name="verifier",
            description="Independent verification of claims. Fresh context, adversarial. Use for final output verification.",
            model=VERIFICATION_MODEL,
            system_prompt=verifier_prompt,
            allowed_tools=["file_ops", "search", "bash"],  # Read-only + can run verification commands
            max_iterations=20,
            temperature=0.0,  # Deterministic for reproducible verification
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
        max_workers: int = 4,
        parent_interrupt_event: Optional[threading.Event] = None
    ):
        self.tools = tools or create_default_registry(working_dir)
        self.working_dir = working_dir
        self.max_workers = max_workers
        self.registry = SubAgentRegistry()
        self.parent_interrupt_event = parent_interrupt_event

        # Track active sub-agents
        self._active: Dict[str, SubAgent] = {}
        self._results: List[SubAgentResult] = []

        # Lazy thread pool for background spawns. Distinct from
        # spawn_parallel's per-call executor: that's a fan-out join; this is
        # fire-and-forget. Created on first background spawn and reused.
        self._bg_executor: Optional[ThreadPoolExecutor] = None
        self._bg_lock = threading.Lock()

    def _build_subagent(self, config: SubAgentConfig) -> SubAgent:
        """Construct a SubAgent for this orchestrator's environment.

        Factored out so background and synchronous paths share one
        construction site, and so tests can monkey-patch this single point
        to inject a fake SubAgent without touching the global SubAgent
        class.
        """
        return SubAgent(
            config=config,
            tools=self.tools,
            working_dir=self.working_dir,
            is_nested=True,
            parent_interrupt_event=self.parent_interrupt_event,
        )

    def spawn(
        self,
        agent_name: str,
        task: str,
        custom_config: Optional[SubAgentConfig] = None,
        background: bool = False,
        on_complete: Optional[Callable[[SubAgentResult], None]] = None,
        produces_uris: Optional[List[str]] = None,
        produces_min_bytes: int = 256,
    ) -> SubAgentResult:
        """
        Spawn and run a sub-agent

        Args:
            agent_name: Name of registered agent type
            task: Task to execute
            custom_config: Optional custom configuration
            background: If True, spawn on a worker thread, register the run
                in the in-flight task index, and return a placeholder
                SubAgentResult with task_id immediately. The caller blocks
                via task_wait(task_id) or polls via task_get(task_id).
            on_complete: Optional callback invoked from the worker thread
                after the manifest's terminal state has been written. Used
                by TaskTool to emit subagent_completed once the background
                run finishes.
            produces_uris: Optional URI patterns / local globs the
                sub-agent must land artifacts at. After the sub-agent
                returns success, the orchestrator validates each pattern
                resolves to ≥1 file ≥ produces_min_bytes; on failure the
                result is downgraded to success=False.
            produces_min_bytes: Per-pattern non-trivial floor; default 256.

        Returns:
            SubAgentResult with output (synchronous) or with task_id set
            (background — output is a human-readable instruction string).
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

        if background:
            return self._spawn_background(
                agent_name, task, config, on_complete,
                produces_uris=produces_uris,
                produces_min_bytes=produces_min_bytes,
            )

        # Drain a stale parent interrupt event before spawning. If the
        # parent's pause-menu cleared `_paused` and `_cancelled` on resume
        # but the threading.Event itself is still set (a leak path that
        # has surfaced when Ctrl+C lands during a child agent's own
        # blocking prompt), every subsequent spawn would short-circuit
        # with "Subagent NOT started: parent agent's interrupt event is
        # set" — even though the user is no longer cancelling. Clearing
        # before construction breaks the loop. A racy fresh cancel that
        # arrives between the clear and SubAgent.run()'s recheck is still
        # caught by SubAgent.run()'s own is_set check (above).
        if (
            self.parent_interrupt_event is not None
            and self.parent_interrupt_event.is_set()
        ):
            self.parent_interrupt_event.clear()

        # Create and run the sub-agent
        sub_agent = self._build_subagent(config)

        result = sub_agent.run(task)

        # produces_uris gate: validate after a successful claim. A failure
        # here turns the result into success=False so the parent sees the
        # gap. Iterations / tokens / duration are preserved for cost
        # attribution. The underlying subagent's own failure path is left
        # alone — the gate only runs on successful claims.
        if result.success and produces_uris:
            verdict = self._validate_produces_uris(
                produces_uris, produces_min_bytes
            )
            self._emit_produces_validation(
                agent_name, produces_uris, verdict
            )
            missing = verdict["missing"]
            if missing:
                result = SubAgentResult(
                    agent_name=agent_name,
                    task=task,
                    success=False,
                    output=result.output,
                    error=self._format_produces_failure(
                        agent_name, produces_uris, missing
                    ),
                    iterations=result.iterations,
                    tokens_used=result.tokens_used,
                    duration_seconds=result.duration_seconds,
                    session_id=result.session_id,
                    task_id=result.task_id,
                    observations=result.observations,
                )

        self._results.append(result)
        self._active[result.session_id] = sub_agent

        return result

    # ---- Background spawn machinery (PR4) ----------------------------------

    def _ensure_bg_executor(self) -> ThreadPoolExecutor:
        with self._bg_lock:
            if self._bg_executor is None:
                self._bg_executor = ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="subagent-bg",
                )
            return self._bg_executor

    @staticmethod
    def _new_subagent_task_id() -> str:
        # Short uuid (8 chars) — enough entropy for the registry, short
        # enough for humans / log scanning. Prefix matches the convention
        # the registry uses: "sciagent-" for sciagent-managed entries,
        # "sub-" so a glance distinguishes a subagent from a compute job.
        return f"sciagent-sub-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _output_log_path(task_id: str):
        from .compute import task_index
        return task_index.manifest_dir() / f"{task_id}.subagent_output.log"

    def _spawn_background(
        self,
        agent_name: str,
        task: str,
        config: SubAgentConfig,
        on_complete: Optional[Callable[[SubAgentResult], None]],
        produces_uris: Optional[List[str]] = None,
        produces_min_bytes: int = 256,
    ) -> SubAgentResult:
        """Write the manifest, hand off to a worker thread, return immediately."""
        from .compute import task_index
        from .provenance_log import _active_session_id
        from .tools.atomic.compute import ComputeTool

        task_id = self._new_subagent_task_id()
        output_log_path = self._output_log_path(task_id)

        # Build the SubAgent in the parent thread BEFORE submitting — its
        # __init__ captures and restores the parent's session ids, and that
        # capture must happen while we're still on the parent thread (the
        # globals are not thread-local). The worker thread only calls
        # sub_agent.run().
        sub_agent = self._build_subagent(config)

        parent_session_id = (
            ComputeTool._shared_session_id or _active_session_id
        )

        manifest = {
            "job_id": task_id,
            "kind": "subagent",
            "state": "running",
            "session_id": parent_session_id,
            "owner_pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "result_summary": None,
            "body": {
                "name": agent_name,
                "task_preview": task[:500],
                "parent_session_id": parent_session_id,
                "child_session_id": sub_agent.session_id,
                "output_log_path": str(output_log_path),
                "result": None,
                # Declared artifact contract — empty list when the parent
                # didn't declare any produces_uris (read-only tasks). Stored
                # alongside the rest of the manifest body so a later reader
                # (lineage, verifier, post-mortem) sees what the gate was
                # asked to check, not just whether it passed.
                "produces_uris": list(produces_uris) if produces_uris else [],
                "produces_min_bytes": produces_min_bytes,
            },
        }
        try:
            task_index.write_task(manifest)
        except Exception as e:
            # Best-effort: if the registry is unwritable, fall back to a
            # synchronous run so the caller still gets a result instead of a
            # ghost task_id pointing to nothing.
            return SubAgentResult(
                agent_name=agent_name,
                task=task,
                success=False,
                output="",
                error=f"Failed to write subagent manifest: {e}",
            )

        executor = self._ensure_bg_executor()
        executor.submit(
            self._run_background,
            sub_agent,
            task,
            task_id,
            output_log_path,
            on_complete,
            produces_uris,
            produces_min_bytes,
        )

        return SubAgentResult(
            agent_name=agent_name,
            task=task,
            success=True,
            output=(
                f"Backgrounded as task {task_id}. Use task_wait('{task_id}') "
                f"to block on completion, or task_get('{task_id}') for a "
                f"snapshot. Full transcript will be at {output_log_path}."
            ),
            iterations=0,
            tokens_used=0,
            duration_seconds=0.0,
            session_id=sub_agent.session_id,
            task_id=task_id,
        )

    _MAX_RESULT_SUMMARY_CHARS = 4000

    def _run_background(
        self,
        sub_agent: SubAgent,
        task: str,
        task_id: str,
        output_log_path,
        on_complete: Optional[Callable[[SubAgentResult], None]],
        produces_uris: Optional[List[str]] = None,
        produces_min_bytes: int = 256,
    ) -> None:
        """Worker-thread entry point for backgrounded subagent runs.

        Always writes a terminal manifest state so a crashed thread doesn't
        leave the registry in 'running' forever. Errors raised by SubAgent.run
        are already caught inside SubAgent.run itself; this thread's
        try/except is only the safety net for failures in our own bookkeeping.
        """
        from .compute import task_index

        try:
            result = sub_agent.run(task)
        except Exception as e:
            # SubAgent.run catches its own exceptions; reaching here means
            # something inside SubAgent.run leaked. Synthesize a failed
            # result so the lifecycle still closes cleanly.
            result = SubAgentResult(
                agent_name=sub_agent.config.name,
                task=task,
                success=False,
                output="",
                error=f"unhandled subagent exception: {e}",
                session_id=getattr(sub_agent, "session_id", None),
            )

        # Best-effort full-output log file. If this fails, the manifest
        # snapshot still has the truncated result_summary.
        try:
            output_log_path.parent.mkdir(parents=True, exist_ok=True)
            output_log_path.write_text(result.output or "", encoding="utf-8")
        except Exception:
            pass

        try:
            self._finalize_background(
                task_id, result,
                produces_uris=produces_uris,
                produces_min_bytes=produces_min_bytes,
                agent_name=sub_agent.config.name,
            )
        except Exception:
            pass

        # Bookkeeping for in-process callers — match the synchronous spawn
        # path's contract so resume()/get_history() work the same way.
        with self._bg_lock:
            self._results.append(result)
            if result.session_id:
                self._active[result.session_id] = sub_agent

        if on_complete is not None:
            try:
                on_complete(result)
            except Exception:
                pass

    def _finalize_background(
        self,
        task_id: str,
        result: SubAgentResult,
        produces_uris: Optional[List[str]] = None,
        produces_min_bytes: int = 256,
        agent_name: Optional[str] = None,
    ) -> None:
        """Read the manifest, merge body.result + lifecycle, atomic write.

        If produces_uris is non-empty and the subagent's result was a success,
        runs the validator before writing the terminal state. A validation
        failure downgrades the result to success=False so the manifest's
        terminal state becomes "failed" and the parent sees the gap.
        """
        from .compute import task_index

        record = task_index.read_task(task_id)
        if record is None:
            return  # manifest gone — nothing to update

        record = dict(record)
        body = dict(record.get("body") or {})

        # produces_uris gate: same logic as the sync path. Runs BEFORE the
        # terminal state is decided so the manifest lands in
        # "blocked_produce_missing" rather than "completed" when artifacts
        # are missing. Provenance event is emitted either way. The new
        # state is distinct from "failed" so a verifier can tell a
        # contract gap apart from a real subagent failure.
        produce_blocked = False
        if result.success and produces_uris:
            verdict = self._validate_produces_uris(
                produces_uris, produces_min_bytes
            )
            self._emit_produces_validation(
                agent_name or body.get("name") or "unknown",
                produces_uris, verdict,
            )
            missing = verdict["missing"]
            if missing:
                produce_blocked = True
                result = SubAgentResult(
                    agent_name=result.agent_name,
                    task=result.task,
                    success=False,
                    output=result.output,
                    error=self._format_produces_failure(
                        agent_name or body.get("name") or "unknown",
                        produces_uris, missing,
                    ),
                    iterations=result.iterations,
                    tokens_used=result.tokens_used,
                    duration_seconds=result.duration_seconds,
                    session_id=result.session_id,
                    task_id=result.task_id,
                    observations=result.observations,
                )

        full_output = result.output or ""
        if len(full_output) > self._MAX_RESULT_SUMMARY_CHARS:
            summary = (
                full_output[: self._MAX_RESULT_SUMMARY_CHARS]
                + f"\n\n[truncated {len(full_output) - self._MAX_RESULT_SUMMARY_CHARS:,} chars; full transcript at {body.get('output_log_path')}]"
            )
        else:
            summary = full_output

        body["result"] = {
            "success": bool(result.success),
            "error": result.error,
            "iterations": getattr(result, "iterations", 0),
            "tokens_used": getattr(result, "tokens_used", 0),
            "duration_seconds": getattr(result, "duration_seconds", 0.0),
            "child_session_id": getattr(result, "session_id", None),
            "summary": summary,
        }
        record["body"] = body

        if result.success:
            terminal_state = "completed"
        elif produce_blocked:
            terminal_state = "blocked_produce_missing"
        else:
            terminal_state = "failed"
        record["state"] = terminal_state
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        # Top-level result_summary is what task_list shows per-row — keep it
        # terse. For failures, prefer the error string; for successes, the
        # truncated output.
        if not result.success and result.error:
            record["result_summary"] = result.error[
                : self._MAX_RESULT_SUMMARY_CHARS
            ]
        else:
            record["result_summary"] = summary

        try:
            task_index.write_task(record)
        except Exception:
            pass

    # ---- produces_uris validation gate -------------------------------------
    #
    # Schemes _LIST_CMDS in tools/atomic/materialize.py supports for cheap
    # listing. az and oci can full-fetch but not list, so v1 skips them
    # rather than failing closed for a tooling limitation.
    _LIST_CAPABLE_CLOUD_SCHEMES = frozenset({"s3", "gs", "r2"})
    _SKIPPABLE_CLOUD_SCHEMES = frozenset({"az", "oci"})

    def _validate_produces_uris(
        self,
        patterns: List[str],
        min_bytes: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """List each pattern; confirm at least one file ≥ min_bytes resolves.

        Returns ``{"missing": [...], "resolved": [...]}``. Each entry in
        ``missing`` is ``{pattern, reason}``; each entry in ``resolved`` is
        ``{pattern, scheme, files: [{path, bytes}, ...]}``. An empty
        ``missing`` means every pattern passed. The check is mechanical
        (one cloud listing or one glob per pattern, no LLM call) — cheap
        relative to the subagent work just done.

        Cloud URIs (s3/gs/r2) go through MaterializeTool(list_only=True);
        az/oci pass through unvalidated for v1 (tooling gap, not policy).
        Local paths and file:// URIs use glob.glob + os.path.getsize.
        """
        import glob
        import os
        from urllib.parse import urlparse
        from .tools.atomic.materialize import MaterializeTool

        materialize: Optional[MaterializeTool] = None
        missing: List[Dict[str, Any]] = []
        resolved: List[Dict[str, Any]] = []
        # Cap how many files-per-pattern we record on the pass event.
        # Bounded so a 10K-object prefix doesn't bloat one provenance line
        # past MAX_LINE_BYTES; the gate's verdict only needs evidence, not
        # an exhaustive listing.
        max_files_per_pattern = 16

        for pat in patterns:
            scheme = urlparse(pat).scheme
            if scheme in self._LIST_CAPABLE_CLOUD_SCHEMES:
                if materialize is None:
                    materialize = MaterializeTool(working_dir=self.working_dir)
                r = materialize.execute(uri=pat, list_only=True, timeout=60)
                if not r.success or not isinstance(r.output, dict):
                    missing.append({
                        "pattern": pat,
                        "reason": f"list failed: {r.error or 'no output'}",
                    })
                    continue
                files = r.output.get("files") or []
                ok = any((f.get("bytes") or 0) >= min_bytes for f in files)
                if not ok:
                    missing.append({
                        "pattern": pat,
                        "reason": (
                            f"{len(files)} files listed, none ≥ {min_bytes} bytes"
                        ),
                    })
                else:
                    resolved.append({
                        "pattern": pat,
                        "scheme": scheme,
                        "files": [
                            {
                                "path": f.get("path"),
                                "bytes": f.get("bytes"),
                            }
                            for f in files[:max_files_per_pattern]
                        ],
                        "file_count": len(files),
                    })
            elif scheme in self._SKIPPABLE_CLOUD_SCHEMES:
                # v1: cloud schemes without cheap listing pass through.
                resolved.append({
                    "pattern": pat,
                    "scheme": scheme,
                    "files": [],
                    "file_count": None,
                    "note": "skipped: scheme has no cheap listing in v1",
                })
                continue
            elif scheme and scheme != "file":
                missing.append({
                    "pattern": pat,
                    "reason": f"unsupported scheme {scheme!r} for validation",
                })
            else:
                local = pat[7:] if pat.startswith("file://") else pat
                base = local if os.path.isabs(local) else os.path.join(
                    self.working_dir, local
                )
                matches = glob.glob(base, recursive=True)
                file_matches = [m for m in matches if os.path.isfile(m)]
                sized = [(m, os.path.getsize(m)) for m in file_matches]
                ok = any(sz >= min_bytes for _, sz in sized)
                if not ok:
                    missing.append({
                        "pattern": pat,
                        "reason": (
                            f"glob matched {len(matches)} entries, "
                            f"{len(file_matches)} are files, "
                            f"none ≥ {min_bytes} bytes"
                        ),
                    })
                else:
                    resolved.append({
                        "pattern": pat,
                        "scheme": "file",
                        "files": [
                            {"path": p, "bytes": sz}
                            for p, sz in sized[:max_files_per_pattern]
                        ],
                        "file_count": len(sized),
                    })
        return {"missing": missing, "resolved": resolved}

    @staticmethod
    def _emit_produces_validation(
        agent_name: str,
        patterns: List[str],
        verdict: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Best-effort provenance event. Routes to the formal emit_*
        methods on ProvenanceLog so verifiers can match on event_kind +
        load-bearing fields without tolerating the ad-hoc shape used
        before promotion. ``verdict`` is the dict returned by
        ``_validate_produces_uris``.
        """
        from .provenance_log import get_active_session_log
        plog = get_active_session_log()
        if plog is None:
            return
        missing = verdict.get("missing") or []
        resolved = verdict.get("resolved") or []
        try:
            if missing:
                plog.emit_produces_validation_failed(
                    subagent_name=agent_name,
                    patterns=list(patterns),
                    missing=missing,
                )
            else:
                plog.emit_produces_validation_passed(
                    subagent_name=agent_name,
                    patterns=list(patterns),
                    resolved=resolved,
                )
        except Exception:
            pass

    @staticmethod
    def _format_produces_failure(
        agent_name: str,
        patterns: List[str],
        missing: List[Dict[str, str]],
    ) -> str:
        """Compose the error string the parent sees on a gate failure.

        Names the missing patterns + their reasons + a remediation hint so
        the parent LLM can compose the next move (re-spawn corrective,
        change inputs, escalate to user) per compose-and-trust.
        """
        lines = [
            f"produces_uris validation FAILED for sub-agent '{agent_name}'.",
            "The sub-agent reported success but the following declared "
            "output URIs are empty or below the byte floor:",
        ]
        for entry in missing:
            lines.append(f"  - {entry['pattern']}: {entry['reason']}")
        lines.append("")
        lines.append(
            "Do not record this task as done. Either re-spawn with a "
            "corrective task that writes to the declared URIs, switch "
            "containers if the env lacked required libs, or surface the "
            "gap to the user."
        )
        return "\n".join(lines)

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
- compute: Run jobs on the cloud (SkyPilot) end-to-end and bring outputs back local. Use for any 'on sky', 'on AWS', 'in the cloud' task. Produces primary scientific data.
- analyze: Consume data produced upstream (compute jobs, prior analyze runs, acquired datasets) and emit derived artifacts: plots, statistics, comparisons, distributions, residuals, light fits (regression / GP / lightweight Bayesian optimization). Picks its own container based on libs needed. Use for ANY 'plot X', 'fit Y', 'compare runs', 'reproduce figure N' deliverable.
- plan: Break down complex problems into steps.
- general: Complex multi-step tasks requiring both exploration AND action.
- verifier: Independent claim verification (fresh context, adversarial). Use for final output verification.

Use 'explore' for quick local searches.
Use 'debug' when investigating errors.
Use 'research' for documentation, APIs, scientific methods.
Use 'compute' for ANY task that runs on the cloud — keeps install chatter, status polls, and job logs out of your context. Returns a tight summary with local file paths.
Use 'analyze' for ANY derivation off compute outputs — plots, fits, comparisons. Don't make compute do plotting in a container that lacks plotting libs; that path ends in fabricated figures.
Use 'plan' before implementing anything non-trivial.
Use 'general' for complex tasks that need to make changes.
Use 'verifier' to independently verify claims before final output.

## Artifact contract — declare produces_uris when the deliverable is a file

Any sub-agent whose deliverable is a durable artifact (figure, fitted model, dataset, derived table, generated report) should be dispatched with `produces_uris` naming the URI patterns or local globs the artifact must land at. After the sub-agent claims success, the orchestrator validates each pattern and fails the result back to you with the missing pattern named if nothing landed. Skip `produces_uris` for read-only tasks (research summaries returned as text, code review, status checks) — there's no artifact to validate.

Cloud-agnostic: listing-validation supports `s3://`, `gs://`, `r2://`; full fetch via `materialize` also supports `az://`, `oci://`. Local paths and globs work too. Use whatever scheme matches the user's data tier — the contract is not AWS-specific. `produces_min_bytes` (default 256) sets the per-pattern non-trivial floor so a 0-byte placeholder doesn't pass.

## The todo DAG is the decomposition; sub-agents execute work within phases

For non-trivial tasks build a `todo` DAG first (see your planning rules). The DAG IS the decomposition — phases describe workflow steps (setup, mesh, solve, post-process, derive, …). Sub-agent dispatches are how phase work EXECUTES; they don't replace the DAG and phases aren't pre-bound to a single sub-agent.

When you author a phase, think about what sub-agent(s) will execute its work — that informs the phase's content and `produces`. Routing is **per-work-item** at execution time, not "phase has a role." Match each work item to the sub-agent whose container fits:

  - Producer-side work (simulation, training, scans, solver-shipped post-processing utilities, mesh / decomposition utilities) → dispatch **compute**.
  - Derivation off primary data (plots, fits, statistics, comparisons, distributions, residuals, light fits) → dispatch **analyze**.
  - Read-only work (file inspection, codebase search, web/literature) → main agent itself, or **explore** / **research**. No `produces_uris` needed.

A phase usually dispatches once — its work fits one container. A phase whose work crosses container boundaries (e.g. "post-process + plot": solver utility, then plot) executes as **multiple consecutive dispatches under the SAME phase** — each carries its own `produces_uris`, and the phase's `todo.produces` only validates after all of them have landed. Don't fragment a natural workflow phase into one-dispatch-each phases just to keep them single-roled.

Discover concrete services via `service_search`; never dispatch a multi-tool pipeline as one giant compute task — that ends up doing derivation in the wrong container. For iterative loops, version the URIs: `<cloud>://<session>/<workflow>/iter-{N}/<phase>/<artifact>`. Each iteration is a fresh DAG pass — no separate loop primitive needed.

Default mode is synchronous (background=false): you block on the sub-agent and get its result inline. Pass background=true to run the sub-agent on a worker thread and get back a task_id immediately — then use task_wait(task_id) to block on terminal state, or task_get(task_id) for a snapshot. Background mode is right when the parent has other work to do (e.g. spawn two sub-agents in parallel and wait on both) or when the sub-agent will run for many minutes and the parent shouldn't block the whole time."""

    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the sub-agent to use",
                "enum": ["explore", "debug", "research", "compute", "analyze", "plan", "general", "verifier"]
            },
            "task": {
                "type": "string",
                "description": "The task for the sub-agent to complete"
            },
            "background": {
                "type": "boolean",
                "description": "If true, run on a worker thread and return a task_id; default false."
            },
            "produces_uris": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URI patterns or local globs the sub-agent must land artifacts at; orchestrator validates after success and fails the result back if any pattern resolves to zero non-trivial files."
            },
            "produces_min_bytes": {
                "type": "integer",
                "description": "Per-pattern non-trivial byte floor; default 256."
            }
        },
        "required": ["agent_name", "task"]
    }

    def __init__(self, orchestrator: SubAgentOrchestrator):
        self.orchestrator = orchestrator

    # Hard cap on what a subagent can return to its parent. Subagents already
    # have isolated context (their own AgentLoop, their own conversation), but
    # whatever they put in their final reply lands in the parent's tool result
    # — and that tool result is replayed in every subsequent parent turn's
    # prompt. A chatty subagent that pastes a 16k-char fetched page into its
    # answer multiplies into ~16k * N_iterations of parent context.
    # 4000 chars (~1000 tokens) is enough for a tight finding+location+fix
    # summary; bulk content belongs in a file the subagent saved under
    # _outputs/ and references by path.
    _MAX_RETURN_CHARS = 4000

    @staticmethod
    def _emit_spawned(plog, agent_name: str, task: str) -> Optional[str]:
        if plog is None:
            return None
        try:
            return plog._write_event(
                "subagent_spawned",
                {
                    "subagent_name": agent_name,
                    "task_preview": task[:500],
                },
                actor=f"subagent:{agent_name}",
            )
        except Exception:
            return None  # provenance is best-effort

    @staticmethod
    def _emit_completed(
        plog,
        agent_name: str,
        spawn_event_id: Optional[str],
        result: SubAgentResult,
    ) -> None:
        if plog is None:
            return
        try:
            plog._write_event(
                "subagent_completed",
                {
                    "subagent_name": agent_name,
                    "spawn_event_id": spawn_event_id,
                    "success": bool(result.success),
                    "iterations": getattr(result, "iterations", None),
                    "tokens_used": getattr(result, "tokens_used", None),
                    "duration_seconds": getattr(result, "duration_seconds", None),
                    "child_session_id": getattr(result, "session_id", None),
                    "error": result.error if not result.success else None,
                },
                actor=f"subagent:{agent_name}",
            )
        except Exception:
            pass

    @staticmethod
    def _emit_observations(
        plog,
        agent_name: str,
        result: SubAgentResult,
    ) -> None:
        """Emit one ``subagent_observation`` event per Observation on the
        result. Best-effort: provenance is never load-bearing (matches the
        try/except discipline of ``_emit_completed`` and the
        ``emit_produces_validation_*`` callsites). One event per observation
        keeps the JSONL line-bounded even if a session emits many.
        """
        if plog is None or not result.observations:
            return
        for obs in result.observations:
            try:
                plog.emit_subagent_observation(
                    subagent_name=agent_name,
                    observation=obs.to_dict(),
                )
            except Exception:
                pass

    def execute(
        self,
        agent_name: str,
        task: str,
        background: bool = False,
        produces_uris: Optional[List[str]] = None,
        produces_min_bytes: int = 256,
    ) -> ToolResult:
        # M1B provenance: emit subagent_spawned / subagent_completed events
        # to the active (parent's) provenance log so the audit trail shows
        # the orchestration. compute_run events emitted from inside the
        # subagent join the same log (see SubAgent.__init__'s session
        # restore), so verify_session(parent_id) gets the full picture:
        # main spawned compute → which launched job X → completed.
        from .provenance_log import get_active_session_log

        plog = get_active_session_log()
        spawn_event_id = self._emit_spawned(plog, agent_name, task)

        if background:
            # Defer subagent_completed to the worker thread's on_complete
            # callback. The closure captures plog + spawn_event_id from
            # this thread so the event lands in the right log even after
            # the parent's session changes.
            def _on_complete(result: SubAgentResult) -> None:
                self._emit_completed(plog, agent_name, spawn_event_id, result)
                self._emit_observations(plog, agent_name, result)

            placeholder = self.orchestrator.spawn(
                agent_name, task,
                background=True,
                on_complete=_on_complete,
                produces_uris=produces_uris,
                produces_min_bytes=produces_min_bytes,
            )
            if not placeholder.success or placeholder.task_id is None:
                # spawn rejected the request before launching the thread —
                # close the audit pair right now so the log doesn't have a
                # dangling subagent_spawned with no completion.
                self._emit_completed(plog, agent_name, spawn_event_id, placeholder)
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Sub-agent failed to launch: {placeholder.error}",
                )
            return ToolResult(
                success=True,
                output=(
                    f"[Sub-agent '{agent_name}' backgrounded as task "
                    f"{placeholder.task_id}]\n\n{placeholder.output}"
                ),
            )

        result = self.orchestrator.spawn(
            agent_name, task,
            produces_uris=produces_uris,
            produces_min_bytes=produces_min_bytes,
        )
        self._emit_completed(plog, agent_name, spawn_event_id, result)

        # Lite-tier observations are bubbled out of band: appended AFTER
        # the narrative truncation so they never count toward
        # _MAX_RETURN_CHARS. The parent LLM sees them under their own
        # header and can mention them in its end-of-task summary; they
        # are candidate findings, never auto-applied (Lite contract).
        observations_block = format_observations_for_parent(
            result.observations
        )
        # Best-effort provenance side-effect for cross-session aggregation
        # later — mirrors emit_produces_validation_*'s shape and try/except
        # discipline. Emitted regardless of success/failure outcome since
        # observations can surface even on a failed run.
        self._emit_observations(plog, agent_name, result)

        if result.success:
            output = result.output or ""
            if len(output) > self._MAX_RETURN_CHARS:
                truncated = output[: self._MAX_RETURN_CHARS]
                dropped = len(output) - self._MAX_RETURN_CHARS
                output = (
                    f"{truncated}\n\n"
                    f"[truncated {dropped:,} chars to keep parent context clean — "
                    f"if you need the full content, ask the sub-agent to write it to "
                    f"_outputs/<file> and return only the path]"
                )
            full = (
                f"[Sub-agent '{agent_name}' completed in "
                f"{result.iterations} iterations]\n\n{output}"
            )
            if observations_block:
                full = f"{full}\n\n{observations_block}"
            return ToolResult(
                success=True,
                output=full,
                # Subagent token cost rolls into the parent's cumulative
                # meter via this side-channel — never visible to the LLM
                # (only ``output`` reaches the model), but the parent's
                # AgentLoop reads metadata after each tool call.
                metadata={
                    "subagent_tokens_used": result.tokens_used,
                    "subagent_name": agent_name,
                    "subagent_iterations": result.iterations,
                    "subagent_observations": [
                        o.to_dict() for o in result.observations
                    ],
                },
            )
        else:
            error = f"Sub-agent failed: {result.error}"
            if observations_block:
                # Failed runs can still surface lessons (e.g., "this image
                # rejects MPI as root") — append under the same header so
                # the parent can codify even when the run itself didn't
                # land artifacts.
                error = f"{error}\n\n{observations_block}"
            return ToolResult(
                success=False,
                output=None,
                error=error,
                metadata={
                    "subagent_tokens_used": result.tokens_used,
                    "subagent_name": agent_name,
                    "subagent_iterations": result.iterations,
                    "subagent_observations": [
                        o.to_dict() for o in result.observations
                    ],
                },
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
    # Main agent doesn't see compute_*; those reach the compute subagent
    # via its allowed_tools. Keeps cloud chatter contained.
    main_tools = tools.clone(
        exclude={"compute_run", "compute_exec", "compute_cluster"}
    )

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

    return AgentLoop(config=config, tools=main_tools, system_prompt=system_prompt)


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

    # Main agent doesn't see compute_*; reachable via the `compute` subagent.
    main_tools = tools.clone(
        exclude={"compute_run", "compute_exec", "compute_cluster"}
    )

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

    return AgentLoop(config=config, tools=main_tools, system_prompt=system_prompt)
