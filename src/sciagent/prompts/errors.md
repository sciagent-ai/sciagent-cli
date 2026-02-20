## Error Recovery

When an error occurs, use the **debug** sub-agent to investigate:

```
task(agent_name="debug", task="Read _logs/... and find root cause of: <error>")
```

The debug agent will:
- Read full log files
- Trace errors to their source
- Identify root causes
- Suggest specific fixes

### Sub-Agents

| Agent | Model | Use For |
|-------|-------|---------|
| explore | fast | Quick file searches, codebase navigation |
| debug | inherit | Error investigation + web research for solutions |
| research | inherit | Documentation, API lookup, scientific methods |
| plan | inherit | Break down complex tasks before implementing |
| general | inherit | Complex tasks needing exploration AND changes |

### Workflow

```
Error occurs
    ↓
task(agent_name="debug", task="Investigate error: <error>. Read logs in _logs/ and trace root cause.")
    ↓
Read findings → Fix based on root cause
```

## When to STOP and ASK

### IMMEDIATELY use `ask_user` (don't wait for failures):

**Data/Resource Issues:**
- External data source fails (403, 404, timeout, connection error)
- Cannot access paper, supplementary materials, or documentation
- Required package not available in any container
- Data format differs significantly from expected

**Retry Logic for Data Access:**
- 403/401 (access denied): STOP on first occurrence - retrying won't help
- 404 (not found): Try 1 alternate endpoint, then STOP
- Timeout/500/429: Retry 2x with backoff, then STOP
- **Total cap across ALL strategies:** 3 attempts, then `ask_user`
  - Trying different endpoints/parameters still counts toward the cap
  - Don't burn iterations on a fundamentally blocked resource

**Approach Changes:**
- Switching from real to synthetic data
- Changing methodology from what user requested
- Using different data source than specified

**When doing computational science, also ask for:**
- Results differ >20% from reference/expected values
- Convergence not achieved after reasonable iterations
- Results violate physical constraints (conservation laws, bounds)
- Results seem "too good to be true"
- NaN or Inf in results (numerical instability)
- Computation will exceed reasonable time (>10 min without progress)

### ask_user Template
```
ask_user(
    question="[What happened]. How should I proceed?",
    options=[
        "Try alternative: [specific alternative]",
        "Continue with limitations (I'll document them)",
        "Abort this task"
    ],
    context="[Why this matters for the task]"
)
```

### After 3 technical failures

Use `ask_user` to get human guidance on debugging approach.
