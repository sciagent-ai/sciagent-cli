---
name: sci-compute
description: "Run scientific and engineering simulations using containerized services (MEEP, GROMACS, RDKit, etc.)"
triggers:
  - "simulat(e|ion)"
  - "compute|calculation"
  - "run.*(meep|gromacs|openfoam|rcwa|rdkit|ase)"
  - "scientific.*(python|computation)"
  - "molecular dynamics"
  - "electromagnetic"
  - "quantum|chemistry"
---

# Scientific Computing

## Overview

This skill enables the execution of scientific and engineering computations by leveraging a registry of containerized services. It combines **research**, **code generation**, **execution**, and **debugging** into a cohesive workflow. Use the `registry.yaml` file to discover available tools, then research documentation and resources to generate correct code and resolve issues.

## Workflow

### Phase 1: Discovery

1. **Locate the Service Registry**: The core of this skill is the `registry.yaml` file located in the `services` directory. This file contains the definitions for all available scientific computing services.

2. **Discover Services**: When the user requests a computation, parse the `registry.yaml` file to identify available services. Each service entry includes a description of its capabilities, the runtime (`python3` or `bash`), and an example of its usage.

3. **Select a Service**: Based on the user's request, select the most appropriate service. The `description` and `capabilities` fields in the `registry.yaml` will help you make this decision.

### Phase 2: Research (IMPORTANT - Do this before writing code)

Before generating any code, **always research** to ensure correctness:

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
