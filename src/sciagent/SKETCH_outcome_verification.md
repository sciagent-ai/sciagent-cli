# Outcome-Based Scientific Verification - Implementation Sketch

## Problem Summary

Current verification checks **process** (did commands run?) and **output** (do files exist?)
but not **outcome** (does this answer the scientific question correctly?).

The verifier has no access to:
- Original user goal
- Scientific domain context
- Expected value ranges
- Success criteria for the actual question

---

## 1. Changes to `OrchestratorConfig` (orchestrator.py)

```python
@dataclass
class OrchestratorConfig:
    # ... existing fields ...

    # NEW: Outcome verification settings
    enable_outcome_verification: bool = True  # Verify scientific outcomes, not just process

    # Original goal tracking
    original_goal: Optional[str] = None  # User's original request/question
    scientific_domain: Optional[str] = None  # e.g., "bioinformatics/DMS", "molecular-dynamics", "genomics"

    # Domain constraint checking
    domain_constraints: Optional[Dict[str, Any]] = None  # Value ranges, expected patterns
    # Example: {"fitness_score": {"min": -5, "max": 2}, "correlation": {"min": -1, "max": 1}}

    # Outcome criteria
    outcome_criteria: Optional[str] = None  # What must be true for success
    # Example: "Determine whether buried residues are less tolerant to mutation"
```

---

## 2. Changes to `TodoItem` (tools/atomic/todo.py)

```python
@dataclass
class TodoItem:
    # ... existing fields ...

    # NEW: Scientific context fields
    scientific_question: Optional[str] = None  # What scientific question does this task answer?
    expected_outcome: Optional[str] = None  # What should we expect if analysis is correct?
    domain: Optional[str] = None  # Scientific domain for validation

    # NEW: Value constraints for outputs
    output_constraints: Optional[Dict[str, Any]] = None
    # Example: {
    #     "fitness_mean": {"min": -3, "max": 1, "typical": 0},
    #     "correlation": {"min": -1, "max": 1},
    #     "p_value": {"min": 0, "max": 1}
    # }

    # NEW: Computed vs claimed reconciliation
    claimed_values: Optional[Dict[str, float]] = None  # Values the task claims to have computed
    # Example: {"mean_fitness": -0.39, "correlation_contacts": -0.4}

    # NEW: Scientific validation requirements
    requires_domain_validation: bool = False  # If True, run domain-specific checks
    validation_checks: Optional[List[str]] = None  # Specific checks to run
    # Example: ["value_ranges", "internal_consistency", "recompute_statistics"]
```

---

## 3. Changes to `_build_verification_context` (orchestrator.py)

```python
def _build_verification_context(self, task: TodoItem) -> Dict[str, Any]:
    """Build context for scientific outcome verification."""

    # === CLAIM SECTION (existing + new) ===
    claim_parts = [f"Task: {task.content}"]

    # NEW: Add original goal
    if self.config.original_goal:
        claim_parts.append(f"\n## ORIGINAL USER GOAL\n{self.config.original_goal}")

    # NEW: Add scientific question this task answers
    if task.scientific_question:
        claim_parts.append(f"\n## SCIENTIFIC QUESTION\n{task.scientific_question}")

    # NEW: Add expected outcome
    if task.expected_outcome:
        claim_parts.append(f"\n## EXPECTED OUTCOME (if analysis is correct)\n{task.expected_outcome}")

    # Existing: claimed result
    if task.result:
        result_str = str(task.result)[:1000]
        claim_parts.append(f"\n## CLAIMED RESULT\n{result_str}")

    # NEW: Add claimed values for reconciliation
    if task.claimed_values:
        claim_parts.append(f"\n## CLAIMED COMPUTED VALUES\n{json.dumps(task.claimed_values, indent=2)}")

    claim = "\n".join(claim_parts)

    # === EVIDENCE SECTION (existing + new) ===
    evidence_parts = []

    # Existing: fetch logs, exec logs, file evidence
    # ... (keep existing code) ...

    # NEW: Add domain context
    domain = task.domain or self.config.scientific_domain
    if domain:
        evidence_parts.append(f"\n## SCIENTIFIC DOMAIN\n{domain}")

        # Add domain-specific expectations
        domain_info = DOMAIN_REGISTRY.get(domain, {})
        if domain_info:
            evidence_parts.append(f"Expected value ranges: {domain_info.get('value_ranges', 'unknown')}")
            evidence_parts.append(f"Common issues: {domain_info.get('common_issues', 'none documented')}")

    # NEW: Add output constraints
    if task.output_constraints:
        evidence_parts.append(f"\n## OUTPUT CONSTRAINTS (values must be in these ranges)")
        for key, constraints in task.output_constraints.items():
            evidence_parts.append(f"- {key}: {constraints}")

    # NEW: Re-compute key statistics from raw data for reconciliation
    if task.claimed_values and task.produces:
        evidence_parts.append(f"\n## RECOMPUTED VALUES (from raw data)")
        recomputed = self._recompute_statistics(task)
        for key, value in recomputed.items():
            claimed = task.claimed_values.get(key)
            match = "✓ MATCH" if claimed and abs(claimed - value) < 0.01 else "✗ MISMATCH"
            evidence_parts.append(f"- {key}: {value} (claimed: {claimed}) {match}")

    evidence = "\n".join(evidence_parts) if evidence_parts else "No evidence available"

    return {"claim": claim, "evidence": evidence}


def _recompute_statistics(self, task: TodoItem) -> Dict[str, float]:
    """Re-compute key statistics from raw data files to verify claims."""
    recomputed = {}

    # Find the source data file
    source_file = self._find_source_data(task)
    if not source_file or not os.path.exists(source_file):
        return recomputed

    try:
        import pandas as pd
        df = pd.read_csv(source_file)

        # Recompute based on domain
        domain = task.domain or self.config.scientific_domain

        if domain and "DMS" in domain.upper():
            # DMS-specific recomputation
            if "mut_score" in df.columns:
                recomputed["mean_fitness"] = float(df["mut_score"].mean())
                recomputed["std_fitness"] = float(df["mut_score"].std())
                recomputed["min_fitness"] = float(df["mut_score"].min())
                recomputed["max_fitness"] = float(df["mut_score"].max())

        # Generic statistics for numeric columns
        for col in df.select_dtypes(include=['float64', 'int64']).columns[:5]:
            recomputed[f"{col}_mean"] = float(df[col].mean())

    except Exception as e:
        recomputed["_error"] = str(e)

    return recomputed
```

---

## 4. New Prompt: `prompts/verification_scientific.md`

```markdown
# Scientific Outcome Verification Agent

You are a scientific peer reviewer. Your job is to verify that computational results
correctly answer the original scientific question.

## CRITICAL DISTINCTION

You are NOT just checking for fraud or fabrication.
You ARE checking whether the output is **scientifically correct and answers the question**.

## CONTEXT

### Original User Goal
{original_goal}

### Scientific Question Being Answered
{scientific_question}

### Expected Outcome (if analysis is correct)
{expected_outcome}

### Claimed Result
{claimed_result}

### Scientific Domain
{domain}

---

## YOUR VERIFICATION PROCESS

### 1. Does the output ANSWER the scientific question?

- What was the user actually trying to learn?
- Does the output provide that answer?
- Is the answer complete or partial?

### 2. Are the computed values PLAUSIBLE for this domain?

Domain: {domain}
Expected ranges: {value_ranges}

Check:
- Are values within physically/biologically possible ranges?
- Do the magnitudes make sense?
- Would a domain expert find these values suspicious?

**Example red flags:**
- DMS fitness scores outside [-5, 2] range
- Correlation coefficients outside [-1, 1]
- Negative counts or probabilities > 1
- Energies with wrong sign or magnitude

### 3. Are claimed values CONSISTENT with raw data?

Claimed values: {claimed_values}
Recomputed from raw data: {recomputed_values}

- Do the claimed statistics match what you'd get from the raw data?
- If there's a mismatch, what could explain it?
- Is the mismatch significant enough to invalidate the conclusion?

### 4. Does the REASONING follow from the EVIDENCE?

- Is the scientific conclusion justified by the data?
- Are there logical gaps between the analysis and the conclusion?
- Are alternative explanations considered?

### 5. Visualization sanity check (if applicable)

- Do axis labels match what's being plotted?
- Do value ranges in plots match the underlying data?
- Is the visualization misleading in any way?

---

## OUTPUT FORMAT

```json
{
    "outcome_verdict": "answers_question|partial_answer|does_not_answer|wrong_answer",
    "scientific_validity": "valid|questionable|invalid",
    "confidence": 0.0-1.0,

    "answers_original_question": true/false,
    "values_plausible": true/false,
    "values_consistent": true/false,
    "reasoning_sound": true/false,

    "issues": [
        "Specific scientific issues found"
    ],
    "value_mismatches": [
        {"claimed": "mean=-0.39", "actual": "mean=11.17", "severity": "critical"}
    ],
    "domain_violations": [
        "Values outside expected range for DMS data"
    ],
    "missing_for_conclusion": [
        "What additional evidence would be needed"
    ],
    "recommendation": "accept|revise|reject",
    "reasoning": "Brief explanation of verdict"
}
```

---

## VERDICT DEFINITIONS

**outcome_verdict:**
- `answers_question`: Output directly and correctly answers the original scientific question
- `partial_answer`: Output provides some insight but doesn't fully answer the question
- `does_not_answer`: Output exists but doesn't address the actual question
- `wrong_answer`: Output claims to answer but the answer is incorrect

**scientific_validity:**
- `valid`: Results are scientifically sound, values are plausible, reasoning is correct
- `questionable`: Some concerns but not clearly wrong
- `invalid`: Clear errors in values, reasoning, or methodology

---

## EXAMPLES

### Example 1: Wrong Answer (what the BRCA1 case should have caught)

Original goal: "Analyze relationship between BRCA1 mutation fitness and protein structure"
Scientific question: "Do buried residues show lower fitness (mutation intolerance)?"
Claimed result: "Mean fitness = 11.17, correlation computed"
Domain: bioinformatics/DMS

Recomputed values:
- mean_fitness: -0.39 (from raw CSV)
- claimed in plot: 11.17

Output:
```json
{
    "outcome_verdict": "wrong_answer",
    "scientific_validity": "invalid",
    "confidence": 0.95,
    "answers_original_question": false,
    "values_plausible": false,
    "values_consistent": false,
    "reasoning_sound": false,
    "issues": [
        "Plot shows mean=11.17 but raw data mean=-0.39 (30x discrepancy)",
        "DMS fitness scores should be in range [-3, 1], not [2, 17]",
        "Likely plotting wrong column (mutation count instead of fitness)"
    ],
    "value_mismatches": [
        {"claimed": "mean=11.17", "actual": "mean=-0.39", "severity": "critical"}
    ],
    "domain_violations": [
        "Fitness values 2-17 are impossible for DMS log-fold-change data"
    ],
    "recommendation": "reject",
    "reasoning": "The visualization shows completely wrong values. A 30x discrepancy between claimed and actual mean indicates the wrong data column was plotted. Results cannot answer the scientific question."
}
```

### Example 2: Valid Result

Original goal: "Find genes differentially expressed in cancer vs normal"
Scientific question: "Which genes show significant fold-change?"
Claimed result: "Found 127 genes with |log2FC| > 2 and FDR < 0.05"
Domain: genomics/RNAseq

Recomputed:
- Gene count with criteria: 127 ✓
- FDR range: [0.0001, 0.049] ✓
- log2FC range: [-4.2, 5.1] ✓

Output:
```json
{
    "outcome_verdict": "answers_question",
    "scientific_validity": "valid",
    "confidence": 0.9,
    "answers_original_question": true,
    "values_plausible": true,
    "values_consistent": true,
    "reasoning_sound": true,
    "issues": [],
    "value_mismatches": [],
    "domain_violations": [],
    "recommendation": "accept",
    "reasoning": "Gene counts match, FDR values are in valid range, fold-changes are biologically plausible. Analysis correctly answers the differential expression question."
}
```
```

---

## 5. Domain Registry: `domains.py` (new file)

```python
"""
Domain-specific validation constraints and expectations.
"""

DOMAIN_REGISTRY = {
    "bioinformatics/DMS": {
        "description": "Deep Mutational Scanning fitness data",
        "value_ranges": {
            "fitness_score": {"min": -5, "max": 2, "typical_mean": 0, "typical_std": 1},
            "log_fold_change": {"min": -5, "max": 2},
        },
        "common_columns": ["mut_score", "fitness", "log2_enrichment"],
        "common_issues": [
            "Plotting mutation counts instead of fitness scores",
            "Confusing site-level vs mutation-level statistics",
            "Missing normalization to wildtype"
        ],
        "sanity_checks": [
            "fitness_mean_near_zero",  # DMS scores typically centered around 0
            "no_extreme_outliers",  # |score| > 5 is suspicious
        ]
    },

    "bioinformatics/RNAseq": {
        "description": "RNA sequencing differential expression",
        "value_ranges": {
            "log2_fold_change": {"min": -15, "max": 15},
            "p_value": {"min": 0, "max": 1},
            "fdr": {"min": 0, "max": 1},
            "tpm": {"min": 0, "max": None},  # No upper bound
            "fpkm": {"min": 0, "max": None},
        },
        "common_issues": [
            "Not correcting for multiple testing",
            "Using raw p-values instead of FDR",
            "Comparing non-normalized counts"
        ]
    },

    "molecular-dynamics": {
        "description": "Molecular dynamics simulations",
        "value_ranges": {
            "rmsd": {"min": 0, "max": 50, "unit": "Angstrom"},
            "temperature": {"min": 0, "max": 1000, "unit": "K"},
            "energy": {"min": None, "max": None},  # Domain-specific
            "pressure": {"min": 0, "max": None, "unit": "bar"},
        },
        "common_issues": [
            "Insufficient equilibration",
            "Energy drift indicating instability",
            "Non-physical conformations"
        ]
    },

    "statistics/correlation": {
        "description": "Correlation analysis",
        "value_ranges": {
            "pearson_r": {"min": -1, "max": 1},
            "spearman_rho": {"min": -1, "max": 1},
            "r_squared": {"min": 0, "max": 1},
            "p_value": {"min": 0, "max": 1},
        },
        "common_issues": [
            "Correlation outside [-1, 1]",
            "R-squared negative or > 1",
            "Not reporting confidence intervals"
        ]
    }
}


def get_domain_validator(domain: str):
    """Get validation function for a domain."""

    def validate(values: Dict[str, float]) -> List[str]:
        """Validate values against domain constraints."""
        issues = []

        if domain not in DOMAIN_REGISTRY:
            return [f"Unknown domain: {domain}"]

        constraints = DOMAIN_REGISTRY[domain].get("value_ranges", {})

        for key, value in values.items():
            if key in constraints:
                c = constraints[key]
                if c.get("min") is not None and value < c["min"]:
                    issues.append(f"{key}={value} below minimum {c['min']}")
                if c.get("max") is not None and value > c["max"]:
                    issues.append(f"{key}={value} above maximum {c['max']}")

        return issues

    return validate


def infer_domain(task_content: str, file_contents: str = "") -> Optional[str]:
    """Attempt to infer scientific domain from task description and data."""

    content = (task_content + " " + file_contents).lower()

    if any(x in content for x in ["dms", "deep mutational", "fitness score", "mut_score"]):
        return "bioinformatics/DMS"

    if any(x in content for x in ["rnaseq", "rna-seq", "differential expression", "deseq", "fold change"]):
        return "bioinformatics/RNAseq"

    if any(x in content for x in ["molecular dynamics", "rmsd", "trajectory", "gromacs", "amber"]):
        return "molecular-dynamics"

    if any(x in content for x in ["correlation", "pearson", "spearman", "r-squared"]):
        return "statistics/correlation"

    return None
```

---

## 6. Integration: Updated `run_llm_verification_gate` (orchestrator.py)

```python
def run_llm_verification_gate(self, tasks: List[TodoItem] = None) -> Dict[str, Any]:
    """Run outcome-based scientific verification."""

    # ... existing setup code ...

    for task in tasks_to_verify:
        # Build SCIENTIFIC verification context (not just fraud detection)
        context = self._build_verification_context(task)

        # Use scientific verification prompt
        verification_prompt = load_prompt("verification_scientific").format(
            original_goal=self.config.original_goal or "Not specified",
            scientific_question=task.scientific_question or task.content,
            expected_outcome=task.expected_outcome or "Not specified",
            claimed_result=task.result or "No result recorded",
            domain=task.domain or self.config.scientific_domain or "general",
            value_ranges=self._get_domain_ranges(task),
            claimed_values=json.dumps(task.claimed_values or {}, indent=2),
            recomputed_values=json.dumps(self._recompute_statistics(task), indent=2),
        )

        # Add evidence
        verification_prompt += f"\n\n## EVIDENCE\n{context['evidence']}"

        # Spawn verifier with scientific focus
        verifier_result = self.subagent.spawn("scientific_verifier", verification_prompt)

        # Process result with outcome-based criteria
        if verifier_result.success:
            verification = parse_verification_json(verifier_result.output)

            # NEW: Check outcome verdict, not just fraud verdict
            outcome = verification.get("outcome_verdict", "unknown")
            validity = verification.get("scientific_validity", "unknown")

            if outcome == "answers_question" and validity == "valid":
                verified_count += 1
            elif outcome == "wrong_answer" or validity == "invalid":
                failed_count += 1
                # NEW: Log specific scientific issues
                for issue in verification.get("value_mismatches", []):
                    print(f"    VALUE MISMATCH: {issue}")
            else:
                # partial_answer or questionable
                if self.config.verification_strict:
                    failed_count += 1
                else:
                    verified_count += 1  # Warn but pass
```

---

## 7. Usage Example

```python
from sciagent.orchestrator import create_orchestrator

# Create orchestrator with scientific context
orchestrator, todo = create_orchestrator(
    # Existing flags
    enable_data_gate=True,
    enable_exec_gate=True,
    enable_verification=True,

    # NEW: Outcome verification
    enable_outcome_verification=True,

    # NEW: Original goal
    original_goal="Analyze how BRCA1 mutations affect protein function based on structural position",

    # NEW: Scientific domain
    scientific_domain="bioinformatics/DMS",

    # NEW: Outcome criteria
    outcome_criteria="Determine if buried residues are less tolerant to mutation than surface residues",

    # NEW: Domain constraints
    domain_constraints={
        "fitness_score": {"min": -5, "max": 2},
        "correlation": {"min": -1, "max": 1}
    }
)

# Add task with scientific context
todo.add_task(
    content="Compute correlation between fitness and burial depth",
    task_type="analysis",
    scientific_question="Do buried residues have lower fitness scores?",
    expected_outcome="Negative correlation (-0.3 to -0.5) between contacts and fitness",
    domain="bioinformatics/DMS",
    output_constraints={
        "mean_fitness": {"min": -3, "max": 1},
        "correlation": {"min": -1, "max": 1}
    },
    claimed_values={},  # Filled in after task completes
    requires_domain_validation=True
)

# Execute - verification will now check OUTCOMES
result = orchestrator.execute_all()
```

---

## Summary of Changes

| Component | Current | New |
|-----------|---------|-----|
| **OrchestratorConfig** | Process/fraud flags only | + original_goal, scientific_domain, outcome_criteria |
| **TodoItem** | task_type, produces, target | + scientific_question, expected_outcome, output_constraints, claimed_values |
| **_build_verification_context** | Logs + file existence | + Original goal, domain context, recomputed statistics |
| **Verification prompt** | "Find fraud" | "Does this answer the scientific question correctly?" |
| **Verdict criteria** | verified/refuted/insufficient | answers_question/partial/wrong + scientific_validity |
| **Domain validation** | None | Domain registry with value ranges and sanity checks |

## Key Insight

The fundamental shift is from:

**"Is this fabricated?"** → **"Is this scientifically correct?"**

The verifier needs to be a **peer reviewer**, not just a **fraud detector**.
