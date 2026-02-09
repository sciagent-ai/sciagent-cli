---
name: code-review
description: "Perform thorough code review with security, quality, and test coverage analysis"
triggers:
  - "review.*(code|pr|pull)"
  - "check.*(code|quality)"
  - "audit"
  - "security.*review"
  - "code.*analysis"
---

# Code Review Workflow

Perform a comprehensive code review covering security, quality, and test coverage.

## Phase 1: Scope Definition

### Identify Review Targets

1. **If reviewing a PR/diff**: Focus on changed files
   ```
   # Get list of changed files
   search(command="glob", pattern="**/*.py")  # or relevant extensions
   ```

2. **If reviewing a directory**: Identify key files
   - Entry points (main.py, index.js, etc.)
   - Core business logic
   - Security-sensitive code (auth, API, data handling)

3. **Create a review todo list**:
   ```json
   {"todos": [
     {"id": "scope", "content": "Define review scope", "status": "in_progress"},
     {"id": "security", "content": "Security review", "task_type": "review", "depends_on": ["scope"]},
     {"id": "quality", "content": "Code quality review", "depends_on": ["security"]},
     {"id": "tests", "content": "Test coverage review", "depends_on": ["quality"]},
     {"id": "report", "content": "Write review report", "depends_on": ["tests"], "produces": "file:_outputs/code_review.md"}
   ]}
   ```

## Phase 2: Security Review

### Check for Common Vulnerabilities

**Injection Vulnerabilities:**
- SQL injection: Look for raw SQL queries with string concatenation
- Command injection: Check shell command construction
- XSS: Look for unescaped user input in HTML output
- Path traversal: Check file path handling

**Authentication & Authorization:**
- Hard-coded credentials or API keys
- Weak password handling
- Missing auth checks on sensitive endpoints
- Session management issues

**Data Exposure:**
- Sensitive data in logs
- Information leakage in error messages
- Insecure data storage

**Search patterns:**
```
# Find potential SQL injection
search(command="grep", pattern="execute.*\\+|format.*SELECT|f\"SELECT")

# Find potential command injection
search(command="grep", pattern="subprocess|os.system|shell=True")

# Find hardcoded secrets
search(command="grep", pattern="password.*=.*[\"']|api_key.*=.*[\"']|secret.*=.*[\"']")
```

### Delegate Deep Security Analysis

For thorough security review, use a sub-agent:
```
task(agent_name="reviewer", task="Security review of <files>. Check for: injection vulnerabilities, auth issues, data exposure, insecure dependencies. Report findings with file:line references.")
```

## Phase 3: Code Quality Review

### Check for Quality Issues

**Code Clarity:**
- Clear, descriptive naming
- Appropriate comments (not too few, not too many)
- Consistent formatting and style
- Functions/methods not too long (>50 lines is a warning)

**DRY Violations:**
- Repeated code blocks
- Copy-pasted logic
- Missing abstractions for common patterns

**Error Handling:**
- Bare except clauses
- Swallowed exceptions
- Missing error handling on I/O
- Unclear error messages

**Edge Cases:**
- Null/None checks
- Empty collection handling
- Boundary conditions
- Race conditions (concurrent code)

**Performance:**
- N+1 queries
- Unnecessary loops
- Memory leaks (unclosed resources)
- Blocking calls in async code

## Phase 4: Test Coverage Review

### Evaluate Test Quality

1. **Check test existence**:
   ```
   search(command="glob", pattern="**/test_*.py|**/*_test.py|**/tests/*.py")
   ```

2. **Assess coverage**:
   - Are critical paths tested?
   - Edge cases covered?
   - Error conditions tested?
   - Integration vs unit test balance?

3. **Test quality indicators**:
   - Assertions are specific (not just `assertTrue`)
   - Tests are isolated (no shared state)
   - Mock usage is appropriate
   - Test names describe behavior

## Phase 5: Generate Report

### Format Your Review

Create `_outputs/code_review.md` with this structure:

```markdown
# Code Review: [Scope]

**Reviewed:** [date]
**Files:** [count]

## Critical Issues

These MUST be fixed before merge:

- [ ] **[CRITICAL]** [file:line] - [Issue description]
  - Impact: [What could go wrong]
  - Fix: [Suggested fix]

## Warnings

Should be addressed:

- [ ] **[WARNING]** [file:line] - [Issue description]
  - Why: [Reason this matters]

## Suggestions

Nice to have improvements:

- **[SUGGESTION]** [file:line] - [Improvement idea]

## Test Coverage

- [ ] Critical paths covered: [Yes/No]
- [ ] Edge cases tested: [Yes/No]
- [ ] Missing tests: [List]

## Positive Notes

What's done well:
- [Good practice 1]
- [Good practice 2]

## Summary

[Overall assessment and recommendation: Approve / Request Changes / Needs Discussion]
```

## Best Practices

1. **Be specific**: Always include file:line references
2. **Explain why**: Don't just say "bad", explain the impact
3. **Suggest fixes**: Provide actionable solutions
4. **Balance feedback**: Note positives too
5. **Prioritize**: Critical > Warning > Suggestion
6. **Stay objective**: Focus on code, not the author
