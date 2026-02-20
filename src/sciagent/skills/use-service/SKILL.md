---
name: sci-compute
description: "Run scientific and engineering computations using containerized services"
triggers:
  # === BROAD INTENT (catch any computational task) ===
  - "run|execute|compute|calculate|simulate|analyze|process|optimize|solve|model"
  - "plot|visualize|graph|figure"
  - "service|registry|container|docker|package|library|install"
  - "scientific|engineering|numerical|mathematical"
  - "dataset"  # For data analysis, not fetching - downloading uses direct bash

  # === GENERAL SCIENCE/ENGINEERING ===
  - "simulat|analys|optimi|minimi|maximi"
  - "equation|formula|algorithm|method"
  - "physics|chemistry|biology|materials"
  - "molecule|protein|sequence|structure"
  - "circuit|chip|semiconductor|VLSI"
  - "mesh|grid|element|solver"
  - "wave|field|particle|atom"
  - "network|graph|path|flow"
  - "signal|spectrum|frequency|filter"
---

# Scientific Computing

## Overview

This skill enables the execution of scientific and engineering computations by leveraging a registry of containerized services. It combines **research**, **code generation**, **execution**, and **debugging** into a cohesive workflow. Use the `registry.yaml` file to discover available tools, then research documentation and resources to generate correct code and resolve issues.

## Workflow

### Phase 0: Service Selection (BEFORE writing any code)

**CRITICAL**: Each Docker service is an **isolated container** with its own packages. Do NOT assume a package exists—verify it.

1. **Identify required packages** for your task:
   - "optimize with Bayesian optimization" → needs `optuna` or `scipy`
   - "RCWA simulation" → needs `S4`
   - "molecular dynamics" → needs `gromacs` or `lammps`

2. **Read registry and check `packages:` field**:
   ```
   file_ops(action="read", path="{registry_path}")
   ```
   The registry path is provided above. Find a service whose `packages:` list contains ALL your requirements.

3. **If no single service has all packages → ASK THE USER**:

   Use `ask_user` to present options:
   ```
   ask_user(
     question="Your task requires [X] and [Y], but no single container has both. How should we proceed?",
     options=[
       "Sequential: Run in separate containers, communicate via files (fastest)",
       "Build container: Create new image with all packages via build-service skill (reusable)",
       "Install at runtime: pip install in container (slower each run, not persistent)",
       "Other"
     ],
     context="Building a new container takes a few minutes but is reusable. Sequential is fastest for one-off tasks."
   )
   ```

   **Option A: Sequential execution** (fastest for one-off)
   - Container A writes to `_outputs/params.json`
   - Container B reads params, writes `_outputs/results.json`
   - Container A reads results, continues

   **Option B: Build combined container** (recommended for repeated use)
   - Use `skill(skill_name="build-service")` to create a new container
   - Check `extends:` field in registry for compatible base images
   - New image is pushed to GHCR and available for future use

   **Option C: Install at runtime** (quick but not persistent)
   - Add `pip install <package>` before main script
   - Slower on each run, not saved to image
   - Use only for quick tests, not production workflows

4. **Verify before coding**:
   ```bash
   docker run --rm <image> python3 -c "import <package>; print('OK')"
   ```

### Phase 1: Discovery

1. **Locate the Service Registry**: Read the registry at `{registry_path}` to see all available services.

2. **Discover Services**: Each service entry includes:
   - `packages`: Libraries installed (verify your needs here)
   - `extends`: Base image (for composition decisions)
   - `capabilities`: What it does
   - `runtime`: How to run (`python3`, `bash`, `julia`)
   - `example`: Sample code

3. **Select a Service**: Based on Phase 0 analysis, select the service with all required packages.

### Phase 2: Research (IMPORTANT - Do this before writing code)

Before generating any code, **always research** to ensure correctness.

**Local first**: When referencing external resources (papers, docs, datasets), check the project folder before web searching.

4. **Search Official Documentation**: Use `WebSearch` to find the official documentation for the selected package.
   - Search: `"{package_name} documentation API reference"`
   - Search: `"{package_name} {specific_task} tutorial"`
   - Example: `"GROMACS molecular dynamics tutorial"` or `"RDKit SMILES parsing documentation"`

5. **Find Working Examples**: Search for examples of similar computations.
   - Search: `"{package_name} {task} example code"`
   - Search: `"{package_name} {task} tutorial github"`
   - Look for official examples, tutorials, and community notebooks

6. **Lookup Scientific Methods** (when applicable): If the user mentions a specific algorithm, method, or technique:
   - Search: `"{method_name} algorithm {package_name}"`
   - Search: `"{method_name} paper"` for original literature
   - Understand required parameters, assumptions, and limitations
   - Example: `"SHAKE algorithm molecular dynamics"` or `"DFT B3LYP basis set selection"`

7. **Check Version-Specific Details**: Scientific software APIs change between versions.
   - Search: `"{package_name} {version} changelog"` if version issues suspected
   - Note deprecated functions or new alternatives

### Phase 3: Code Generation

8. **Generate Code Using Researched Context**: Write code based on what you learned:
   - Use the correct API patterns from official docs
   - Follow best practices from tutorials
   - Include proper imports and initialization
   - Set parameters appropriately for the scientific method

9. **Prepare the Execution Environment**: All computations run inside Docker containers. Mount the user's current working directory as a volume to `/workspace` for input/output access.

### Phase 4: Execution

10. **Run the Computation**:
    - **For `python3` runtimes**: Execute the Python script within the container
    - **For `bash` runtimes**: Execute shell commands within the container

### Phase 5: Debug (When errors occur)

11. **Search for Error Solutions**: When a computation fails:
    - Search: `"{package_name} {exact_error_message}"`
    - Search: `"{package_name} {error_type} fix"`
    - Search: `"site:github.com {package_name} issues {error_keywords}"`
    - Search: `"site:stackoverflow.com {package_name} {error}"`

12. **Check Common Issues**: Look for known pitfalls:
    - Search: `"{package_name} common errors"`
    - Search: `"{package_name} troubleshooting"`
    - Check if it's a dependency, version, or configuration issue

13. **Apply Fix and Retry**: Based on research, modify the code and re-run.

---

## Research Guidelines

### When to Search

| Situation | What to Search |
|-----------|----------------|
| Unfamiliar package | Official docs, getting started guide |
| Specific scientific method | Method paper, algorithm explanation |
| Complex workflow | Step-by-step tutorials, example pipelines |
| Error occurs | Error message + package name |
| Performance issues | Optimization guides, best practices |
| Parameter selection | Parameter tuning guides, benchmarks |

### Search Query Patterns

```
# Documentation
"{package} documentation"
"{package} API reference {module}"
"{package} {function_name} parameters"

# Tutorials & Examples
"{package} {task} tutorial"
"{package} {task} example python"
"{package} getting started"

# Scientific Methods
"{method} algorithm explained"
"{method} {package} implementation"
"{method} parameters meaning"

# Debugging
"{package} {error_message}"
"{package} {error_type} solution"
"site:github.com/{package_repo}/issues {error}"

# Papers & Theory
"{method} original paper"
"{algorithm} computational chemistry"
"{technique} molecular dynamics theory"
```

### Using WebFetch for Documentation

When you find a relevant documentation page, use `WebFetch` to retrieve detailed information:

```
WebFetch(url="https://docs.package.org/api/module", prompt="Extract the function signature and parameters for X")
```

---

## Running Computations

When a user wants to run a computation, construct a `docker run` command.

**Example `docker run` command structure:**

```bash
docker run --rm -v "$(pwd)":/workspace -w /workspace {image_name} {runtime} -c "{user_code_or_command}"
```

### Python Runtime Example

If the user wants to use `rdkit` to analyze a molecule, and the `registry.yaml` defines `rdkit` with a `python3` runtime:

**User Request:** "Tell me the molecular weight of ethanol (CCO)."

**Research Step:**
- Search: `"RDKit molecular weight calculation"`
- Find: Use `Descriptors.MolWt()` from `rdkit.Chem.Descriptors`

**Generated Command:**

```bash
docker run --rm -v "$(pwd)":/workspace -w /workspace ghcr.io/sciagent-ai/rdkit python3 -c "from rdkit import Chem; from rdkit.Chem import Descriptors; mol = Chem.MolFromSmiles('CCO'); print(f'Molecular Weight: {Descriptors.MolWt(mol)}')"
```

### Bash Runtime Example

If the user wants to run a `gromacs` simulation, and the `registry.yaml` defines `gromacs` with a `bash` runtime:

**User Request:** "Run the gromacs energy minimization workflow."

**Research Step:**
- Search: `"GROMACS energy minimization tutorial"`
- Find: Standard workflow is `gmx grompp` → `gmx mdrun`
- Search: `"GROMACS grompp parameters minim.mdp"`

**Generated Command (assuming the necessary files are in the current directory):**

```bash
docker run --rm -v "$(pwd)":/workspace -w /workspace ghcr.io/sciagent-ai/gromacs bash -c "gmx grompp -f minim.mdp -c solvated.gro -p topol.top -o em.tpr && gmx mdrun -v -deffnm em"
```

### Debug Example

**Error:** `Fatal error: No such file: minim.mdp`

**Debug Steps:**
1. Search: `"GROMACS minim.mdp template"`
2. Find: Standard minimization parameters
3. Create the missing file with correct parameters
4. Retry the computation

---

## Best Practices

1. **Always research before coding** - Don't guess APIs; look them up
2. **Cite your sources** - Tell the user where you found the information
3. **Start simple** - Run basic examples before complex workflows
4. **Verify outputs** - Check that results are scientifically reasonable
5. **Save research context** - Note useful docs found for follow-up questions
6. **Explain the science** - Help users understand what the code does, not just run it

By following this workflow, you provide users with research-backed, correct scientific computations in a consistent and reproducible manner.
