## Verification Before Completion - CRITICAL

NEVER mark a task complete without verification. This is the #1 cause of failed tasks.

### What Requires Verification

| Task Type | Verification Required |
|-----------|----------------------|
| Code that produces output | Run it, check output is correct |
| Scripts/programs | Execute and verify no errors |
| Tests | Run tests, verify they pass |
| Builds | Build succeeds without errors |
| Data processing | Output data exists and is valid |
| Optimization | Results meet stated targets |
| File creation | File exists with expected content |

### Verification Pattern

```
1. Define success criteria BEFORE starting
   - What output should exist?
   - What values should be achieved?
   - What should NOT happen (no errors, no crashes)?

2. After implementation, RUN verification
   - Execute: python script.py, npm test, etc.
   - Check output against criteria
   - Parse results for key metrics

3. Only mark complete if ALL criteria pass
   - If partial success -> iterate, don't complete
   - If blocked -> note what's missing, ask user
```

### Simulation Results - PHYSICAL REASONING REQUIRED
- Code that runs without errors != correct results
- Validate outputs against expected ranges or reference values
- **Simplified code that "works" but ignores physics = FAILURE**

#### Scientific Validation Checklist
Before marking a simulation task complete:
1. **Physical units**: Are units consistent? (nm vs μm, Hz vs rad/s)
2. **Sanity checks**: Do results make physical sense?
   - Efficiencies > 100% = wrong
   - Negative energies where impossible = wrong
   - Phases outside expected range = wrong
3. **Boundary conditions**: Are they physical?
4. **Geometry**: Does the model match the physical system?
   - Don't simplify geometry just to avoid errors
   - If S4/MEEP throws errors, FIX THE SETUP, don't remove features
5. **Convergence**: Have numerical parameters been validated?

#### Anti-Pattern: Avoiding Physics
```
X Error in complex geometry → simplify to trivial case → "runs without errors" → complete
✓ Error in complex geometry → debug root cause → fix API usage → validate physical results
```

**If you find yourself simplifying the physics to avoid errors, STOP and debug instead.**

### Common Verification Commands

**By Language:**
- Python: `python script.py`, `pytest`, `python -c "from X import Y"`
- JavaScript/Node: `node script.js`, `npm test`, `npm run build`
- TypeScript: `npx ts-node script.ts`, `npm test`
- Go: `go run main.go`, `go test ./...`
- Rust: `cargo run`, `cargo test`
- Java: `mvn test`, `gradle test`
- Shell: `bash script.sh`, `./script.sh`

**General:**
- File exists: `ls -la path/to/file` or `test -f path/to/file`
- JSON valid: `cat file.json | head` (check structure)
- Build succeeds: Check exit code is 0
- Output correct: Compare against expected values

### Use `produces` Field
```json
{"content": "Generate config", "produces": "file:_outputs/config.json"}
```
System auto-fails if file doesn't exist after "completion".

### Use `target` Field
```json
{"content": "Optimize model", "target": {"metric": "accuracy", "operator": ">=", "value": 0.95}}
```
System auto-fails if target not met.

### Anti-Pattern: DO NOT DO THIS

```
X Write code -> mark complete (without running)
X "Should work" -> mark complete (without testing)
X Partial results -> mark complete (targets not met)
```
