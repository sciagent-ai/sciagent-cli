#!/usr/bin/env bash
# Cross-provider sanity check.
# - Fresh project dir per provider (under a single mktemp base) so runs don't
#   touch each other or the source tree.
# - Captures per-run: status, tool_call count (proxy for agentic iterations),
#   tokens in/out, cost in USD.
# - Prints a summary table at the end.
# Assumes ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / XAI_API_KEY
# are exported in the current shell. Missing keys → that row shows FAIL.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROLLUP="$REPO_ROOT/scripts/cost_rollup.py"
SMOKE_DIR="$(mktemp -d "/tmp/sciagent-smoke.XXXXXX")"
echo "Smoke workspace: $SMOKE_DIR"
echo

MODELS=(
  "anthropic/claude-haiku-4-5-20251001"
  "openai/gpt-4.1-nano"
  "gemini/gemini-2.5-flash"
  "xai/grok-3-mini"
)

# Per-row results.
ROW_MODEL=()
ROW_STATUS=()
ROW_TOOLS=()
ROW_TIN=()
ROW_TOUT=()
ROW_COST=()

for m in "${MODELS[@]}"; do
  echo "=== $m ==="
  slug="$(echo "$m" | tr '/' '_')"
  pdir="$SMOKE_DIR/$slug"
  mkdir -p "$pdir"

  prev_log="$(ls -t ~/.sciagent/sessions/*/provenance.jsonl 2>/dev/null | head -1)"
  status="OK"
  if ! sciagent run --model "$m" --project-dir "$pdir" "say hello in 3 words"; then
    status="FAIL"
  fi

  new_log="$(ls -t ~/.sciagent/sessions/*/provenance.jsonl 2>/dev/null | head -1)"
  tool_calls=0
  tokens_in=0
  tokens_out=0
  cost="0.0000"

  if [[ -n "$new_log" && "$new_log" != "$prev_log" ]]; then
    echo "  log: $new_log"
    tool_calls=$(grep -c '"event_kind": "tool_call"' "$new_log" 2>/dev/null || echo 0)
    read tokens_in tokens_out cost < <(
      python "$ROLLUP" "$new_log" 2>/dev/null \
        | awk -F',' 'NR>1 {tin+=$5; tout+=$6; cost+=$7}
                     END {printf "%d %d %.4f\n", tin+0, tout+0, cost+0}'
    )
    echo "  → tool_calls=$tool_calls  tokens_in=$tokens_in  tokens_out=$tokens_out  cost_usd=\$$cost"
  else
    echo "  (no new provenance log written)"
  fi

  ROW_MODEL+=("$m")
  ROW_STATUS+=("$status")
  ROW_TOOLS+=("$tool_calls")
  ROW_TIN+=("$tokens_in")
  ROW_TOUT+=("$tokens_out")
  ROW_COST+=("$cost")
  echo
done

# Summary table.
echo "============================== Summary =============================="
printf "%-40s  %-6s  %-10s  %-9s  %-10s  %-10s\n" \
  "Model" "Status" "ToolCalls" "TokensIn" "TokensOut" "CostUSD"
printf "%-40s  %-6s  %-10s  %-9s  %-10s  %-10s\n" \
  "----------------------------------------" "------" "----------" "---------" "----------" "----------"
for i in "${!ROW_MODEL[@]}"; do
  printf "%-40s  %-6s  %-10s  %-9s  %-10s  \$%-9s\n" \
    "${ROW_MODEL[$i]}" \
    "${ROW_STATUS[$i]}" \
    "${ROW_TOOLS[$i]}" \
    "${ROW_TIN[$i]}" \
    "${ROW_TOUT[$i]}" \
    "${ROW_COST[$i]}"
done

echo
echo "Workspaces left at: $SMOKE_DIR"
echo "Delete with: rm -rf $SMOKE_DIR"
