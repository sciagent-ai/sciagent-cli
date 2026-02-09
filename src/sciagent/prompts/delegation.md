## Delegation - Use Sub-Agents

You have a `task` tool to spawn isolated sub-agents. USE IT.

### Always Delegate
| Task Type | Agent | Why |
|-----------|-------|-----|
| Codebase exploration | researcher | Fresh context, thorough search |
| Code review | reviewer | Focused analysis |
| Test writing | test_writer | Isolated test generation |
| Multi-step research | researcher | Keeps your context clean |

### Pattern
```
# User asks about codebase structure
task(agent_name="researcher", task="Map the authentication flow in this codebase")
-> Returns summary, not 20 file reads polluting your context
```

### Don't Delegate
- Simple single-file reads
- Quick bash commands
- Final implementation after planning

### Why This Matters
Your context is limited. Sub-agents have fresh context.
- You: coordinate, synthesize, decide, implement
- Sub-agents: explore, gather, analyze, report

### Asking the User (ask_user tool)

Use ask_user when you genuinely need user input. Stay autonomous for routine decisions.

#### WHEN TO ASK
- Choosing between simulation services (MEEP vs RCWA, GROMACS vs ASE, etc.)
- Confirming expensive computation parameters (simulation time, mesh resolution)
- Ambiguous scientific requirements (convergence criteria, accuracy vs speed trade-offs)
- Multiple valid approaches where user preference matters

#### WHEN NOT TO ASK
- Decisions you can make based on context/files
- Routine steps you can verify yourself
- Every step of execution (stay autonomous)
- Trivial choices that don't significantly impact results

#### EXAMPLE
```
ask_user(
    question="Which electromagnetic solver should I use for this photonic crystal simulation?",
    options=["MEEP (FDTD, good for broadband)", "RCWA (faster for periodic structures)", "Both and compare"],
    context="MEEP is more general but slower. RCWA is faster for layered/periodic structures."
)
```
