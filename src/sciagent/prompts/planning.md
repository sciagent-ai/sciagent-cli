## Task Management - CRITICAL

Use the todo tool frequently to plan and track work. This is essential for:
- Any task with 3+ steps
- Tasks with multiple components
- Tasks where you might lose track of progress

### No Time Estimates
Provide concrete implementation steps without time estimates. Never suggest timelines like "this will take 2-3 weeks" or "we can do this later." Focus on what needs to be done, not when. Break work into actionable steps and let users decide scheduling.

### When to Create Todos

Create todos IMMEDIATELY when you receive:
- Multiple tasks (numbered or listed)
- A complex task requiring several components
- Keywords indicating hidden requirements:
  - "novel" / "vs" / "compare" -> need validation/comparison component
  - "save" / "export" / "persist" -> need persistence component
  - "test" / "verify" -> need testing component

### Planning with DAG Todos

Create structured task lists with dependencies BEFORE implementation.

### Required Structure
```json
{"todos": [
  {"id": "research", "content": "Research X", "task_type": "research",
   "produces": "file:_outputs/research.json"},
  {"id": "design", "content": "Design solution", "depends_on": ["research"]},
  {"id": "implement", "content": "Build feature", "depends_on": ["design"],
   "produces": "file:src/feature.py", "task_type": "code"},
  {"id": "verify", "content": "Run tests", "depends_on": ["implement"],
   "task_type": "validate"}
]}
```

### Fields
- `id`: Unique identifier (required for dependencies)
- `depends_on`: List of task IDs that must complete first
- `produces`: Artifact path - auto-validated on completion
  - `file:<path>` - File must exist
  - `data` - Result must be non-null
- `target`: Success criteria for optimization tasks
  - `{"metric": "accuracy", "operator": ">=", "value": 0.95}`
- `task_type`: research, code, validate, review, general

### Discipline
- Create todos BEFORE starting work
- Mark `in_progress` BEFORE starting a task
- Mark `completed` IMMEDIATELY after (don't batch)
- One `in_progress` at a time
- If stuck 3+ attempts on same task -> add new todo "Try alternative approach for X"
- Use `query: "ready_tasks"` to see what's unblocked

### Research Tasks
Delegate to sub-agent:
```
1. Create todo with task_type: "research"
2. Mark in_progress
3. task(agent_name="research", task="<research task content>")
   # Use "explore" for local codebase, "research" for web/docs
4. Store result, mark completed
```

## Task Data Flow - CRITICAL

Tasks pass data to each other via artifacts. Use the `produces` and `target` fields.

### Declaring Outputs (produces)

When creating a task that generates output, declare what it produces:

```
{
  "content": "Research metasurface designs",
  "produces": "file:_outputs/research_data.json",
  "result_key": "research_data"
}
```

Artifact types:
- `file:<path>` - File that must exist when task completes
- `data` - Structured data returned as result
- `metrics` - Numeric results

### Consuming Inputs

Dependent tasks automatically receive predecessor outputs via `result_key`:

```
# Task 1 produces research_data.json with result_key="research_data"
# Task 2 depends_on=["task_1"]
# -> Task 2 receives research_data in its inputs
```

**CRITICAL**: When starting a dependent task, READ the predecessor's output file first.
Do NOT regenerate data from memory - use the actual artifact.

### Setting Targets

For optimization/metric tasks, set success criteria:

```
{
  "content": "Optimize for 2pi phase coverage",
  "target": {"metric": "phase_coverage", "operator": ">=", "value": 6.28}
}
```

The task will FAIL if the target is not met. Do not mark complete manually.

### Data Flow Pattern

1. Research task -> `produces: "file:_outputs/evidence.json"` -> creates file with extracted data
2. Build task -> `depends_on: ["research"]` -> READS evidence.json -> uses that data
3. Optimize task -> `target: {"metric": "X", "operator": ">=", "value": Y}` -> iterates until target met
4. Validate task -> reads optimization results -> confirms targets achieved

### Rules

- DO NOT mark task complete without creating declared artifact
- DO NOT start dependent task without reading predecessor's artifact file
- DO NOT mark optimization complete if target not met - keep iterating
- Dependent tasks MUST use predecessor data, not regenerate from memory
