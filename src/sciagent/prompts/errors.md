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
| explore | fast (Haiku) | Quick file searches, codebase navigation |
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

### After 3 failures

Use `ask_user` to get human guidance.
