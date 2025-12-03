#!/bin/bash
#
# Filter stage driver: determines deterministic failures, gathers commits, and flags irrelevant ones.
# Usage:
#   ./filter_triage.sh <workflow_name> <subjob_name> <model> [ci-mode]
# Note: model parameter is kept for compatibility but currently unused
#

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
CANON_DATA_DIR="${ROOT}/auto_triage/data"
CANON_LOGS_DIR="${ROOT}/auto_triage/logs"
CANON_OUTPUT_DIR="${ROOT}/auto_triage/output"
DATA_LINK="${ROOT}/data"
LOGS_LINK="${ROOT}/logs"
OUTPUT_LINK="${ROOT}/output"
FIND_SCRIPT="${ROOT}/find_boundaries.sh"

echo "=== Filter stage: preparing directories ==="
mkdir -p "$CANON_DATA_DIR" "$CANON_LOGS_DIR" "$CANON_OUTPUT_DIR"

# Maintain convenient symlinks
ln -sfn auto_triage/data "$DATA_LINK"
ln -sfn auto_triage/logs "$LOGS_LINK"
ln -sfn auto_triage/output "$OUTPUT_LINK"

if [ "$CI_MODE" = "ci" ]; then
    echo "Filter stage CI mode detected, removing find_boundaries.sh to prevent re-execution."
    rm -f "$FIND_SCRIPT"
fi

echo "=== Verifying boundary artifacts for filter stage ==="
SUMMARY_FILE="${CANON_DATA_DIR}/boundaries_summary.json"
SUBJOB_RUNS_FILE="${CANON_DATA_DIR}/subjob_runs.json"
if [ ! -s "$SUMMARY_FILE" ] || [ ! -s "$SUBJOB_RUNS_FILE" ]; then
    echo "Error: boundary metadata missing (expected at ${SUMMARY_FILE} and ${SUBJOB_RUNS_FILE})." >&2
    ls -l "$CANON_DATA_DIR"
    exit 1
fi

if ! command -v copilot >/dev/null 2>&1; then
    echo "Error: GitHub Copilot CLI is required but not found in PATH." >&2
    exit 1
fi

INSTRUCTIONS_FILE="${ROOT}/filter_instructions_for_llm.txt"
if [ ! -f "$INSTRUCTIONS_FILE" ]; then
    echo "Error: ${INSTRUCTIONS_FILE} not found." >&2
    exit 1
fi

read -r -d '' PROMPT <<EOF || true
You are operating in a CI environment with no interactive approval. Complete the following FILTER-STAGE instructions for workflow '${WORKFLOW}' and job '${SUBJOB}':

$(cat "$INSTRUCTIONS_FILE")
EOF

echo "=== Launching GitHub Copilot CLI filter stage (model parameter: ${MODEL}, using default model) ==="
# Ensure COPILOT_GITHUB_TOKEN is set (should be set by action.yml, but provide fallback)
# GH_TOKEN is used by bash scripts for gh api calls and should remain as github.token
if [ -z "${COPILOT_GITHUB_TOKEN:-}" ]; then
    echo "Warning: COPILOT_GITHUB_TOKEN not set, falling back to GH_TOKEN"
    export COPILOT_GITHUB_TOKEN="${GH_TOKEN:-}"
fi
# Use programmatic mode with --allow-all-tools for CI environment
copilot -p "$PROMPT" --allow-all-tools

