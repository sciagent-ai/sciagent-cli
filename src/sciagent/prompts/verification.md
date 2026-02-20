## Verification Before Completion - CRITICAL

NEVER mark a task complete without verification.

### Verification Pattern

```
1. Define success criteria BEFORE starting
2. After implementation, RUN verification
3. Only mark complete if ALL criteria pass
   - Partial success → iterate, don't complete
   - Blocked → note what's missing, ask user
```

### Common Verification Commands

| Language | Commands |
|----------|----------|
| Python | `python script.py`, `pytest` |
| Node/TS | `node script.js`, `npm test`, `npm run build` |
| Go | `go run main.go`, `go test ./...` |
| Rust | `cargo run`, `cargo test` |
| Shell | `bash script.sh` |

### Auto-Verification Fields

```json
{"content": "Generate config", "produces": "file:_outputs/config.json"}
{"content": "Optimize model", "target": {"metric": "accuracy", "operator": ">=", "value": 0.95}}
```

---

## Scientific Verification

**Apply this section when doing computational science, simulations, or data analysis.**

### Universal Checklists

### Data Integrity

1. [ ] **Source verified?** Actual source used, provenance documented
2. [ ] **All results reported?** Including failures and negative results
3. [ ] **Uncertainty quantified?** Error bars, confidence intervals, proper sig figs
4. [ ] **Reproducible?** Seeds saved, parameters documented, intermediates preserved

### Uncertainty Reporting

**Correct:** `0.89 ± 0.03 (n=10)` · `Energy: -45.2 ± 0.5 kJ/mol` · `3/5 runs succeeded`

**Wrong:** `0.8923847561` · No uncertainty · Hiding failed runs

### Physical & Dimensional Consistency

1. [ ] **Units consistent?** Same system throughout, explicit when possible
2. [ ] **Order of magnitude sensible?** Quick estimate matches result
3. [ ] **Physical bounds respected?** (T > 0, 0 ≤ prob ≤ 1, energy conserved, etc.)

---

## Computational Foundations (All Simulations)

Apply to: CFD, EM, MD, QC, FEA, thermal, acoustics, controls, etc.

### Convergence & Stability

1. [ ] **Converged?** Residuals/solution stable, not just max iterations reached
2. [ ] **Grid/timestep independent?** Finer resolution gives same answer (within tolerance)
3. [ ] **Stable?** CFL/Courant satisfied, no numerical blowup

### Conservation & Verification

1. [ ] **Conserved quantities conserved?** Mass, energy, momentum, charge balance
2. [ ] **Verified against known solution?** Analytical solution, benchmark, or experiment

---

## Statistical Rigor (All Data/ML)

Apply to: ML, bioinformatics, hypothesis testing, data analysis, etc.

### Validity

1. [ ] **Splits correct?** No leakage, temporal order preserved if time-series
2. [ ] **Multiple testing corrected?** Bonferroni/FDR if many comparisons
3. [ ] **Effect size reported?** Not just p-values or accuracy

### Robustness

1. [ ] **Variance reported?** Across seeds, folds, or runs
2. [ ] **Assumptions checked?** Normality, independence for statistical tests

---

## Anti-Patterns (NEVER DO)

```
✗ Write code → mark complete without running
✗ Simplify physics/problem to avoid errors → debug instead
✗ Results "too good" → report without investigating
✗ Cherry-pick successful runs → report all runs
✗ Use synthetic data silently → ask user first
```
