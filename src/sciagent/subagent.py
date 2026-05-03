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


@dataclass
class SubAgentConfig:
    """Configuration for a sub-agent"""
    name: str
    description: str
    system_prompt: str
    model: str = DEFAULT_MODEL
    max_iterations: int = 40
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
    # Set when spawn(background=True) returns immediately with a registry id
    # the caller uses with task_wait / task_get. None for synchronous spawns.
    task_id: Optional[str] = None

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

        # Check if parent was already cancelled before starting
        if self.parent_interrupt_event and self.parent_interrupt_event.is_set():
            return SubAgentResult(
                agent_name=self.config.name,
                task=task,
                success=False,
                output="",
                error="Cancelled by parent",
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

Only use status snapshots (`bg_status`, `compute_cluster(action="status")`)
to read state ONCE at decision points (e.g., before a follow-up exec),
not in a loop.

## Pick a mode FIRST (read this before any compute_run call)

Sky offers two execution surfaces. Choose deliberately, per task shape:

  - **mode="cluster"** — provision a persistent cluster ONCE, then run
    N follow-ups on it via `compute_exec`. Each follow-up is ~10
    seconds. Use this whenever the task may involve more than one
    compute call against the same workspace: probes, env checks,
    fix-and-retry loops, multi-step pipelines, paper reproductions
    where you'll iterate on Allrun. This is the default for ANY
    iterative work.
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
  4. compute_cluster(action="down", cluster_name) when done — or rely
       on autostop (30 min idle by default).

Hard rules for cluster mode:
  - Resources (instance type, GPUs, num_nodes, disk_size) are immutable
    once a cluster is launched. Different resources require a new
    cluster_name (after sky.down on the old one).
  - file_mounts and the run command CAN change between calls.
  - setup runs only on initial launch; refresh_mounts skips it.
  - Cluster mode requires backend="skypilot" — local Docker has no
    cluster equivalent.

## Path contract (image-agnostic, identical across every registry image)

Three roles, three paths. One rule for inputs, one for outputs, one for code shipping. Image WORKDIR is irrelevant for the contract — sciagent never invents a CWD.

**Inputs — `/workspace/` (and friends), conditional.** Mounted ONLY when you pass `workspace_source=`. The conventional default path is `/workspace/`, but you can declare any path. Cloud-agnostic: the source URI scheme picks the cloud (`s3://`, `gs://`, `az://`, `r2://`, `oci://`).

  - Single source (back-compat): `workspace_source="s3://bucket/case/"` → mounted at `/workspace/`.
  - Multi-source (e.g. query + reference DB, code + vendored data): `workspace_source=[{"path":"/workspace","source":"s3://q/"},{"path":"/data/nr","source":"gs://nr-public/"}]` → two mounts at the declared paths. Mixing clouds is fine.

**Outputs — `/outputs/<job_id>/`, ALWAYS.** Auto-mounted, isolated by job_id, auto-fetched on terminal status. Exposed as `$OUTPUTS_DIR` to your command. Always write results there:
  - `python solve.py --out $OUTPUTS_DIR/result.json`
  - `cp -r postProcessing $OUTPUTS_DIR/`
  - `gmx mdrun -o $OUTPUTS_DIR/traj.xtc`
  Cross-job reads in the same session: `/outputs/<other_job_id>/...` directly. (Job 2 reads Job 1's outputs by absolute path; you have job_1_id from the prior launch result.)

**Local code — `workdir=<path>`, opt-in.** When set, SkyPilot rsyncs that local directory to the cluster and CWD becomes `~/sky_workdir/`. Use this for ad-hoc scripts you wrote locally. Default (omitted) → no rsync, image WORKDIR is honored.

**Never reference `~/sky_workdir/` directly in your command** — that's internal SkyPilot. The compute layer will reject any command that does.

CWD precedence on the cluster: input mount > `workdir=` rsync target > image WORKDIR. The compute layer's prologue sets the cd and `$OUTPUTS_DIR` for you.

## Two flows, pick by task shape

**Ad-hoc script + outputs (hello world, plot X, small Python work):**
- file_ops: write `script.py` in the project working dir.
- compute_run(image="python:3.11", workdir=".", command="pip install -q <deps> && python script.py --out $OUTPUTS_DIR/result.json", backend="skypilot")
- bg_wait(job_id, block=True, timeout=600) — auto-fetches `/outputs/<job_id>/` to local on success.
- For hours-long jobs prefer bg_wait(job_id) (snapshot) and re-check sparsely.

**Case-files reproductions (OpenFOAM, GROMACS, paper repros):**
- file_ops: stage the case dir locally if you transformed it (e.g., `_outputs/boussinesq_case/` after copying from CaseFiles/ + edits).
- compute_run(service="openfoam-swak4foam-2012", workspace_source="<absolute-local-case-path>", command="bash Allrun && cp -r postProcessing log.* $OUTPUTS_DIR/", backend="skypilot", intent={paper, case, run}, expected_artifacts=[...])
  - workspace_source uploads the case dir → mounted at `/workspace/`. Sciagent cd's there for you; `bash Allrun` runs from the case dir.
  - The Allrun output (postProcessing/, log.*) needs an explicit `cp` to `$OUTPUTS_DIR` — that's what gets auto-fetched.
  - Use the version-tagged service entry from the registry (see Version-pinning below).
  - intent/expected_artifacts feed the verifier path; preserve them.
- bg_wait(job_id, block=True, timeout=1800) for the solver phase (10–30 min typical for 60K-grid cases).

**Multi-source workflow (BLAST query + reference DB; code + vendored data):**
- compute_run(service="...", workspace_source=[{"path":"/workspace","source":"s3://my-q/"},{"path":"/data/nr","source":"gs://public-nr/"}], command="blastn -query /workspace/q.fa -db /data/nr/nr -out $OUTPUTS_DIR/hits.tsv", backend="skypilot")
- The agent doesn't care which cloud each bucket lives on — sciagent picks the right CLI per scheme. Reference data (read-only, multi-GB) just gets its own mount path.

**Parallel sweep (N runs, varying parameters):** launch N compute_runs in parallel; distinct job_ids → distinct `/outputs/<job_id>/` prefixes; no collisions; aggregate locally by walking the per-job subdirs returned by bg_wait.

## Hard rules
- Use `pip install -q` (quiet) to keep install chatter out of bg_output.
- Pass backend="skypilot" explicitly; never leave it as auto.
- ALWAYS write results to `$OUTPUTS_DIR/...` (or `/outputs/<job_id>/...`). Relative writes (`./out.txt`) land in CWD and are NOT auto-fetched.
- For case-files workflows: pass `workspace_source=<local-case-path>`. Never reference `/workspace/<X>` without first declaring the mount.
- Never reference `~/sky_workdir/`. It's internal SkyPilot — the compute layer rejects commands that mention it.
- If you need a missing file from the bucket, use bash with the cloud's CLI matching the scheme (e.g., `aws s3 sync` for s3://, `gsutil rsync` for gs://). Bg_wait auto-fetches the per-job prefix; you only invoke the CLI manually for cross-job or mid-run peeks.
- If a job fails, fetch logs with bg_output(job_id, tail_lines=40), diagnose, and decide: fix-and-relaunch, or report up to the main agent with the error preview.

## Version-pinning (preserves source settings verbatim)
When the manuscript / README / case files name a specific tool version (e.g. "OpenFOAM.com v.2012", "GROMACS 2021.5"), pick the version-tagged service entry, not the generic one. Inspect the registry first: `grep -n "<tool>" src/sciagent/services/registry.yaml`. For OpenFOAM v2012 specifically, the right entry is `openfoam-swak4foam-2012`, NOT the generic `openfoam-swak4foam` (which floats to whatever `:latest` points at and has burned reproductions before). If no version-tagged entry exists, surface that uncertainty to the parent instead of silently picking a near-match.

## What to return to the parent
A bounded summary: status, job_id, list of local files produced, cost, total wall time. Do NOT paste script contents, install logs, or full job output — those stay in your context. Parent sees a tight result.""",
            allowed_tools=["file_ops", "bash", "compute_run", "compute_exec", "compute_cluster", "service_search", "bg_status", "bg_output", "bg_wait", "bg_kill", "web"],
            max_iterations=60,
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
            return self._spawn_background(agent_name, task, config, on_complete)

        # Create and run the sub-agent
        sub_agent = self._build_subagent(config)

        result = sub_agent.run(task)
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
            self._finalize_background(task_id, result)
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

    def _finalize_background(self, task_id: str, result: SubAgentResult) -> None:
        """Read the manifest, merge body.result + lifecycle, atomic write."""
        from .compute import task_index

        record = task_index.read_task(task_id)
        if record is None:
            return  # manifest gone — nothing to update

        record = dict(record)
        body = dict(record.get("body") or {})

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

        terminal_state = "completed" if result.success else "failed"
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
- compute: Run jobs on the cloud (SkyPilot) end-to-end and bring outputs back local. Use for any 'on sky', 'on AWS', 'in the cloud' task.
- plan: Break down complex problems into steps.
- general: Complex multi-step tasks requiring both exploration AND action.
- verifier: Independent claim verification (fresh context, adversarial). Use for final output verification.

Use 'explore' for quick local searches.
Use 'debug' when investigating errors.
Use 'research' for documentation, APIs, scientific methods.
Use 'compute' for ANY task that runs on the cloud — keeps install chatter, status polls, and job logs out of your context. Returns a tight summary with local file paths.
Use 'plan' before implementing anything non-trivial.
Use 'general' for complex tasks that need to make changes.
Use 'verifier' to independently verify claims before final output.

Default mode is synchronous (background=false): you block on the sub-agent and get its result inline. Pass background=true to run the sub-agent on a worker thread and get back a task_id immediately — then use task_wait(task_id) to block on terminal state, or task_get(task_id) for a snapshot. Background mode is right when the parent has other work to do (e.g. spawn two sub-agents in parallel and wait on both) or when the sub-agent will run for many minutes and the parent shouldn't block the whole time."""

    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the sub-agent to use",
                "enum": ["explore", "debug", "research", "compute", "plan", "general", "verifier"]
            },
            "task": {
                "type": "string",
                "description": "The task for the sub-agent to complete"
            },
            "background": {
                "type": "boolean",
                "description": "If true, run on a worker thread and return a task_id; default false."
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

    def execute(
        self,
        agent_name: str,
        task: str,
        background: bool = False,
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

            placeholder = self.orchestrator.spawn(
                agent_name, task, background=True, on_complete=_on_complete
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

        result = self.orchestrator.spawn(agent_name, task)
        self._emit_completed(plog, agent_name, spawn_event_id, result)

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
            return ToolResult(
                success=True,
                output=f"[Sub-agent '{agent_name}' completed in {result.iterations} iterations]\n\n{output}"
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
