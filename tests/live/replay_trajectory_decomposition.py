"""Live LLM static-replay test against the failed Boussinesq trajectory.

Single API call. Fast-forwards past the read-only exploration phase
(which was correct in the original trajectory too) and asks the model
to produce the DECOMPOSITION decision that the original trajectory got
wrong: did it route the figure to compute (the failure path) or to
analyze with declared produces_uris (the new contract)?

Run:
    ANTHROPIC_API_KEY=sk-ant-... python tests/live/replay_trajectory_decomposition.py

Cost: ~$0.05 on sonnet-4. No cloud, no clusters.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from sciagent.prompts import build_system_prompt
from sciagent.subagent import SubAgentOrchestrator, TaskTool
from sciagent.defaults import CODING_MODEL


# Verbatim from the trajectory's user prompt.
TRAJECTORY_TASK = (
    "Can you reproduce temperature distribution for the typical Boussinesq "
    "case, with 62K grid size (Fig3)? The manuscript and casefiles are "
    "provided in the project folder. Identify the right environment, "
    "boundary conditions. You can find the relevant docker images in the "
    "registry and compute through sky. Save all files and output in the "
    "same folder."
)

# Synthesized "exploration findings" — fast-forwards turn 1 so the model
# is forced to make the decomposition decision the original trajectory
# got wrong. Mirrors what the original trajectory's exploration produced
# (lines 38-95 of trajectory.txt, condensed).
EXPLORATION_CONTEXT = """\
I've explored the project folder. Here's what's there:

- Manuscript.pdf is the source paper. Figure 3 is a "volume-weighted KDE
  of cell temperature" — a probability-density curve showing the
  distribution of temperature T across the domain, with each cell
  weighted by its volume V.
- CaseFiles/README documents three grid configurations; "c: 62k" maps to
  cell size 140mm with Nx=47, Ny=24, Nz=50.
- CaseFiles/steady_incompressible/ has the OpenFOAM case template;
  blockMeshDict needs Nx/Ny/Nz updated to (47,24,50) for the 62K grid.
- CaseFiles/kdePlot.py shows the KDE methodology (volume-weighted, scipy
  gaussian_kde with weights=V).
- service_search('openfoam') returns 'openfoam-swak4foam-2012' as the
  matching version pinned in README.
- The OpenFOAM container has the solver + post-processing utilities
  (writeCellVolumes etc) but does NOT have matplotlib/scipy/pandas.
- service_search('python') returns 'scipy-base' which has numpy/scipy/
  matplotlib/pandas.

Now I need to plan the actual work and dispatch sub-agents."""


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.")
        return 2

    import litellm

    sys_prompt = build_system_prompt(
        working_dir="/tmp/boussinesq-replay",
        skill_descriptions=(
            "- sci-compute: Run scientific computations using containerized services\n"
            "- code-review: Code review with security/quality/coverage analysis\n"
            "- build-service: Build and publish Docker services to GHCR"
        ),
        registry_path="/Users/shrutibadhwar/Documents/2026/testpackage/"
                      "sciagent-cli/src/sciagent/services/registry.yaml",
    )
    orch = SubAgentOrchestrator(working_dir="/tmp/boussinesq-replay")
    tt = TaskTool(orch)
    task_tool_schema = {
        "type": "function",
        "function": {
            "name": "task",
            "description": tt.description,
            "parameters": TaskTool.parameters,
        },
    }

    print(f"system prompt: {len(sys_prompt):,} chars")
    print(f"user task    : {TRAJECTORY_TASK[:80]}…")
    print(f"context      : fast-forwarded past exploration ({len(EXPLORATION_CONTEXT)} chars)")
    print(f"model        : {CODING_MODEL}")
    print("calling …\n")

    resp = litellm.completion(
        model=CODING_MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": TRAJECTORY_TASK},
            {"role": "assistant", "content": EXPLORATION_CONTEXT},
            {"role": "user", "content": "Good — now dispatch the right "
             "sub-agents to actually produce Figure 3."},
        ],
        tools=[task_tool_schema],
        max_tokens=2500,
    )

    msg = resp.choices[0].message
    print("--- assistant reply ---")
    print((msg.content or "(no prose)")[:1200])

    tcs = list(getattr(msg, "tool_calls", []) or [])
    parsed = []
    if tcs:
        print(f"\n--- {len(tcs)} tool call(s) ---")
        for i, tc in enumerate(tcs, 1):
            args = json.loads(tc.function.arguments)
            parsed.append((tc.function.name, args))
            print(f"\n[{i}] {tc.function.name}(")
            for k, v in args.items():
                if k == "task":
                    disp = (v[:200] + "…") if len(v) > 200 else v
                else:
                    disp = v
                print(f"      {k}={disp!r}")
            print("    )")
    else:
        print("\n--- NO tool calls (model answered conversationally) ---")

    # Structural checks — what we care about for the trajectory's failure mode.
    #
    # Note: parents naturally dispatch SEQUENTIALLY (compute first, wait for
    # its URIs to materialize, then analyze) — that's the correct shape.
    # On a single-turn capture we therefore check the FIRST dispatch's
    # correctness + intent in prose for the next step, not parallel
    # dispatch of both compute+analyze in one shot.
    print("\n--- assertions ---")
    task_calls = [(n, a) for n, a in parsed if n == "task"]
    compute_calls = [a for _, a in task_calls if a.get("agent_name") == "compute"]
    analyze_calls = [a for _, a in task_calls if a.get("agent_name") == "analyze"]

    def _produces_are_sim_outputs(uris):
        """produces_uris should point at sim fields (T, V, fields, ...),
        not at a figure (.pdf, .png, .jpg)."""
        if not uris:
            return False
        joined = " ".join(uris).lower()
        figure_endings = (".pdf", ".png", ".jpg", ".jpeg", ".svg")
        return not any(joined.endswith(end) or end + " " in joined
                       or end + "'" in joined or end + '"' in joined
                       for end in figure_endings)

    prose = (msg.content or "").lower()
    mentions_analyze_step = any(
        marker in prose
        for marker in [
            "analyze", "two step", "then ", "next step",
            "kde plot", "figure", "after the simulation",
        ]
    )

    checks = [
        ("at least one task() dispatch",
            len(task_calls) >= 1),
        ("compute is dispatched (for the sim)",
            len(compute_calls) >= 1),
        ("compute declares produces_uris pointing at SIM OUTPUTS (not figures)",
            any(_produces_are_sim_outputs(a.get("produces_uris", []))
                for a in compute_calls)),
        ("compute does NOT also receive the figure as part of its produces_uris",
            not any(
                any(end in u.lower() for u in a.get("produces_uris", [])
                    for end in [".pdf", ".png", ".jpg"])
                for a in compute_calls
            )),
        ("multi-step intent: prose announces analyze / next-step OR "
         "analyze is dispatched in same turn",
            mentions_analyze_step or len(analyze_calls) >= 1),
    ]

    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")

    n_pass = sum(1 for _, ok in checks if ok)
    print(f"\n{n_pass}/{len(checks)} checks pass")
    if n_pass == len(checks):
        print("\n✓ The new prompts steer the LLM away from the trajectory's failure mode.")
    else:
        print("\n✗ Decomposition diverged from the new contract — review the call above.")
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
