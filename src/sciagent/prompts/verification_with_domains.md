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

## Universal Checklists

### Data Integrity

1. [ ] **Source verified?** Actual source used, provenance documented
2. [ ] **All results reported?** Including failures and negative results
3. [ ] **Uncertainty quantified?** Error bars, confidence intervals, proper sig figs
4. [ ] **Reproducible?** Seeds saved, parameters documented, intermediates preserved

### Uncertainty Reporting

**Correct:** `0.89 ± 0.03 (n=10)` · `Energy: -45.2 ± 0.5 kJ/mol` · `3/5 runs succeeded`

**Wrong:** `0.8923847561` · No uncertainty · Hiding failed runs

---

## Domain Checklists

### Machine Learning

1. [ ] **Splits correct?** Train/val/test documented, no leakage, temporal splits for time series
2. [ ] **Evaluation honest?** Test set held out, variance reported, multiple metrics
3. [ ] **Hyperparameters documented?** Tuned on validation only, search method noted
4. [ ] **Preprocessing documented?** Outlier handling justified, fit on train only

### Molecular Dynamics

1. [ ] **System setup valid?** Force field appropriate, box size sufficient, no clashes
2. [ ] **Equilibration complete?** Temperature/pressure stabilized before production
3. [ ] **Sampling sufficient?** Multiple trajectories, correlation time considered
4. [ ] **Conservation laws?** Energy drift acceptable, no exploding atoms

### CFD

1. [ ] **Mesh quality?** Grid independence verified, y+ appropriate for turbulence model
2. [ ] **Convergence?** Residuals below threshold, monitors stabilized
3. [ ] **Boundary conditions physical?** Inlet/outlet/wall conditions match problem
4. [ ] **Conservation?** Mass/momentum/energy balanced across domain

### Electromagnetics

1. [ ] **Resolution adequate?** At least 10-20 points per wavelength
2. [ ] **Boundaries correct?** PML/periodic/symmetric as needed, no spurious reflections
3. [ ] **Source valid?** Proper normalization, bandwidth covers frequencies of interest
4. [ ] **Results physical?** T + R ≤ 1, fields decay in absorbers

### Quantum Chemistry

1. [ ] **Basis set adequate?** Convergence tested, appropriate for property
2. [ ] **Method appropriate?** DFT functional or post-HF method justified
3. [ ] **Geometry optimized?** Forces below threshold, correct spin state
4. [ ] **Results validated?** Compare to experimental or higher-level theory

### Bioinformatics

1. [ ] **Data source verified?** Accession numbers, database versions documented
2. [ ] **Alignment quality?** Coverage and identity thresholds appropriate
3. [ ] **Statistical correction?** Multiple testing correction applied
4. [ ] **Reproducible?** Software versions, parameters documented

---

## Anti-Patterns (NEVER DO)

```
✗ Write code → mark complete without running
✗ Simplify physics/problem to avoid errors → debug instead
✗ Results "too good" → report without investigating
✗ Cherry-pick successful runs → report all runs
✗ Use synthetic data silently → ask user first
```
