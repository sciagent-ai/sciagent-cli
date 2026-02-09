## Error Recovery

### Spiral Detection
System tracks repeated errors. After 3 of the same type:
- You'll see: "[SYSTEM] DEBUGGING SPIRAL DETECTED"
- You MUST try a completely different approach
- Simplify until something works, then add complexity

### Response Strategy
| Error Type | First Response | If Repeats |
|------------|----------------|------------|
| Import error | Install dep, check spelling | Try different library |
| Type error | Check types, add conversion | Simplify data structures |
| Timeout | Reduce scope | Break into smaller steps |
| File not found | Check path | List directory first |

### Preemptive Checks
- Before editing: Have I read this file?
- Before deleting: List the targets first
- Before complex operation: Test with minimal input

### Preemptive Fixes
- Type errors: check types match before operations
- Serialization: ensure objects are serializable (convert arrays, handle special types)
- Imports/dependencies: verify modules exist before using them
- Async issues: handle promises/callbacks properly (JS), use await (Python/JS)

### When Stuck
1. Read FULL error message
2. Search codebase for working patterns
3. After 2 attempts: simplify drastically
4. Still stuck: ask_user for guidance

### Error Response Pattern

**First occurrence**: Try the suggested fix
**Second occurrence**: Try a DIFFERENT approach from suggestions
**Third occurrence**: STOP. Completely change strategy.

- Same error 3+ times -> STOP. Try different approach.
- Timeout -> Start simpler, add complexity
- Blocked -> Ask user or pivot
