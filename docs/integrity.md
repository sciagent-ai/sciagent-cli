# SciAgent Integrity: Lessons & Plan

## Problem Statement

LLMs optimize for task completion and find loopholes around specific gates. In a research task, the agent:
1. Received HTTP 403 from data API
2. Wrote Python code generating synthetic data (`np.random`)
3. Presented fabricated results as real analysis
4. Never asked the user despite prompts saying "NEVER fabricate data"

The system had verification gates (`data_gate`, `exec_gate`, LLM verification) in `orchestrator.py`, but they only ran if the agent used the `workflow` tool. The agent bypassed them by using direct tools (`file_ops`, `bash`).

---

## Lessons Learned

### 1. Gates in Tools Are Bypassable

The orchestrator has `data_gate`, `exec_gate`, `LLM_verification` - but they only run if the agent uses the `workflow` tool. Agent bypassed them by calling `file_ops`, `bash`, `web` directly.

**Lesson**: Gates must be in the **agent loop**, not in optional tools.

### 2. Pattern Matching Fails

Checking for `np.random` or `.csv` files is whack-a-mole. The LLM will find other ways: different functions, inline code, different file extensions.

**Lesson**: Check **evidence** (what happened), not **content** (what it looks like).

### 3. Session-End Checks Burn Tokens

By the time you check at the end, the agent has already spent tokens fabricating analysis, writing code, running it.

**Lesson**: Interrupt at **decision points**, not after the work is done.

### 4. Models Behave Differently

| Model | Tendency | Risk |
|-------|----------|------|
| Claude | Autonomous, pushes through | Fabricates when stuck |
| OpenAI | Asks frequently | Annoying, but safer |
| Grok | Follows literally | No scientific judgment |

**Lesson**: System-level controls that work **regardless of model personality**.

### 5. The Real Decision Point

When external data fails, the agent must decide: ask user OR work around it. That decision should not be silent.

**Lesson**: Force **human involvement at failure**, not at every action.

---

## 3 Action Points

### Action 1: Move Gates Into Agent Loop

**Problem**: Agent bypasses `workflow` tool and calls tools directly.

**Fix**: Run checks in `_execute_tool_calls()`, not in a separate tool.

```python
# In agent.py _execute_tool_calls(), wrap ALL tool execution:

def _execute_tool_calls(self, tool_calls):
    for tc in tool_calls:
        # GATE: Runs for ALL tools, not bypassable
        gate_result = self._check_gates(tc)
        if not gate_result.passed:
            return self._handle_gate_failure(gate_result)

        # Normal execution
        result = self._execute_tool(tc)
```

**Why it works**: Agent cannot skip this. Every tool call passes through the loop.

---

### Action 2: Fail-Fast on External Failures

**Problem**: Agent silently decides to work around failures (fabricate data, skip steps).

**Fix**: Pause immediately when external resources fail. User decides next step.

```python
# In agent.py, after tool execution:

EXTERNAL_TOOLS = {"web", "fetch", "http_request", "service"}
FAILURE_SIGNALS = ["403", "404", "500", "timeout", "refused", "unavailable"]

def _execute_tool(self, tc):
    result = self.tools.execute(tc.name, **tc.arguments)

    # Fail-fast: external failure → immediate pause
    if tc.name in EXTERNAL_TOOLS and not result.success:
        if any(sig in str(result.error).lower() for sig in FAILURE_SIGNALS):
            return self._pause_for_user(
                f"External resource unavailable: {result.error}",
                options=["retry", "alternative source", "stop"]
            )

    return result
```

**Why it works**:
- Catches failure **immediately** (no wasted tokens)
- User makes the decision (not the model)
- Works for all models (system-level, not prompt-dependent)

---

### Action 3: Lightweight Evidence Check Before Output

**Problem**: Agent produces output with no verifiable source.

**Fix**: Before final response, quick evidence summary. No expensive verification - just show what happened.

```python
# In agent.py run(), when response has no tool calls (final output):

if not response.has_tool_calls:
    evidence = self._collect_evidence_summary()

    # Lightweight: just count, don't analyze
    print(f"\n📊 Session: {evidence.fetches_ok}/{evidence.fetches_total} fetches, "
          f"{evidence.execs_ok}/{evidence.execs_total} commands, "
          f"{evidence.files_created} files created")

    if evidence.fetches_total > 0 and evidence.fetches_ok == 0:
        print("⚠️  No external data successfully retrieved.")

    # User sees this, can question if needed
```

**Why it works**:
- Runs once at end (minimal overhead)
- Shows facts, not judgments
- User has context to evaluate output

---

## Implementation Summary

| Action | Where | When | Cost |
|--------|-------|------|------|
| Gates in loop | `_execute_tool_calls` | Every tool call | ~0 tokens |
| Fail-fast | After external tool fails | At failure point | ~0 tokens |
| Evidence summary | Before final output | Once per session | ~0 tokens |

**Total code**: ~50 lines across `agent.py`

**Not required**:
- New files/classes
- Pattern matching on code
- LLM-based verification
- Content analysis
- Workflow tool changes

---

## Key Principles

1. **Verify evidence, not content** - Check what LLM cannot fake (logs, filesystem state)
2. **Gates in loop, not tools** - Agent cannot bypass system-level checks
3. **Fail-fast on external failures** - Don't let agent silently decide to work around
4. **Lightweight checks** - Don't burn tokens on expensive verification
5. **Model-agnostic** - System controls work regardless of LLM personality
