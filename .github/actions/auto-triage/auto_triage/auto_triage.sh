#!/bin/bash
#
# Full triage driver: wipes old data/logs, finds boundaries, then invokes OpenCode.
# Usage:
#   ./auto_triage.sh <workflow_name> <subjob_name> <model>
# Example:
#   ./auto_triage.sh galaxy-quick quick-wh-glx-quick openai/gpt-5.1-codex-mini

set -euo pipefail

if [ $# -lt 3 ]; then
    echo "Usage: $0 <workflow_name> <subjob_name> <model> [ci-mode]" >&2
    exit 1
fi

WORKFLOW="$1"
SUBJOB="$2"
MODEL="$3"
CI_MODE="${4:-}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${ROOT}/data"
SUMMARY_FILE="${ROOT}/data/boundaries_summary.json"
SUBJOB_RUNS_FILE="${ROOT}/data/subjob_runs.json"
LOGS_DIR="${ROOT}/logs"
FIND_SCRIPT="${ROOT}/find_boundaries.sh"

echo "=== Cleaning auto_triage/data and auto_triage/logs ==="
if [ -d "$DATA_DIR" ]; then
    find "$DATA_DIR" -mindepth 1 ! -name 'boundaries_summary.json' ! -name 'subjob_runs.json' -exec rm -rf {} +
else
    mkdir -p "$DATA_DIR"
fi
rm -rf "$LOGS_DIR"
mkdir -p "$LOGS_DIR"
rm -rf "$ROOT/output"
mkdir -p "$ROOT/output"

# Remove find_boundaries.sh so the LLM cannot rerun it (already executed upstream).
if [ "$CI_MODE" = "ci" ]; then
    echo "CI mode detected, removing find_boundaries.sh to prevent re-execution."
    rm -f "$FIND_SCRIPT"
fi

cd "$ROOT"

echo "=== Verifying boundary artifacts ==="
if [ ! -s "$SUMMARY_FILE" ]; then
    echo "Error: boundaries summary not found at $SUMMARY_FILE" >&2
    ls -l "$DATA_DIR"
    exit 1
fi
if [ ! -s "$SUBJOB_RUNS_FILE" ]; then
    echo "Error: subjob_runs.json not found at $SUBJOB_RUNS_FILE" >&2
    ls -l "$DATA_DIR"
    exit 1
fi
SUMMARY_COUNT=$(jq '.runs | length' "$SUMMARY_FILE")
FAIL_COUNT=$(jq '[.[] | select(.status != "success")] | length' "$SUBJOB_RUNS_FILE")
echo "runs recorded: $SUMMARY_COUNT"
echo "failures recorded: $FAIL_COUNT"

if ! command -v opencode >/dev/null 2>&1; then
    echo "Error: opencode CLI is required but not found in PATH." >&2
    exit 1
fi

INSTRUCTIONS_FILE="${ROOT}/instructions_for_opencode.txt"
if [ ! -f "$INSTRUCTIONS_FILE" ]; then
    echo "Error: ${INSTRUCTIONS_FILE} not found." >&2
    exit 1
fi

read -r -d '' PROMPT <<EOF || true
You are operating in a CI environment with no interactive approval. Complete the following instructions for workflow '${WORKFLOW}' and job '${SUBJOB}':

$(cat "$INSTRUCTIONS_FILE")
EOF

echo "=== Launching OpenCode (model: ${MODEL}) ==="
opencode run -m "$MODEL" "$PROMPT"
