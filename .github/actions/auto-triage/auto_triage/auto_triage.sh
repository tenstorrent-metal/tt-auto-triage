#!/bin/bash
#
# Full triage driver: wipes old data/logs, finds boundaries, then invokes GitHub Copilot CLI.
# Usage:
#   ./auto_triage.sh <workflow_name> <subjob_name> <model>
# Example:
#   ./auto_triage.sh galaxy-quick quick-wh-glx-quick openai/gpt-5.1-codex-mini
# Note: model parameter is kept for compatibility but currently unused

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
SUMMARY_FILE="${CANON_DATA_DIR}/boundaries_summary.json"
SUBJOB_RUNS_FILE="${CANON_DATA_DIR}/subjob_runs.json"
FIND_SCRIPT="${ROOT}/find_boundaries.sh"

echo "=== Preparing auto_triage/data and auto_triage/logs ==="
mkdir -p "$CANON_DATA_DIR" "$CANON_LOGS_DIR"
rm -rf "$CANON_OUTPUT_DIR"
mkdir -p "$CANON_OUTPUT_DIR"

# Maintain convenient symlinks (./data, ./logs, ./output) pointing at canonical locations.
ln -sfn auto_triage/data "$DATA_LINK"
ln -sfn auto_triage/logs "$LOGS_LINK"
ln -sfn auto_triage/output "$OUTPUT_LINK"

# Remove find_boundaries.sh so the LLM cannot rerun it (already executed upstream).
if [ "$CI_MODE" = "ci" ]; then
    echo "CI mode detected, removing find_boundaries.sh to prevent re-execution."
    rm -f "$FIND_SCRIPT"
fi

cd "$ROOT"

echo "=== Verifying boundary artifacts ==="
if [ ! -s "$SUMMARY_FILE" ]; then
    echo "Error: boundaries summary not found at $SUMMARY_FILE" >&2
    ls -l "$CANON_DATA_DIR"
    exit 1
fi
if [ ! -s "$SUBJOB_RUNS_FILE" ]; then
    echo "Error: subjob_runs.json not found at $SUBJOB_RUNS_FILE" >&2
    ls -l "$CANON_DATA_DIR"
    exit 1
fi
SUMMARY_COUNT=$(jq '.runs | length' "$SUMMARY_FILE")
FAIL_COUNT=$(jq '[.[] | select(.status != "success")] | length' "$SUBJOB_RUNS_FILE")
echo "runs recorded: $SUMMARY_COUNT"
echo "failures recorded: $FAIL_COUNT"

if ! command -v copilot >/dev/null 2>&1; then
    echo "Error: GitHub Copilot CLI is required but not found in PATH." >&2
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

echo "=== Launching GitHub Copilot CLI (model parameter: ${MODEL}, using default model) ==="
# Ensure COPILOT_GITHUB_TOKEN is set (should be set by action.yml, but provide fallback)
# GH_TOKEN is used by bash scripts for gh api calls and should remain as github.token
if [ -z "${COPILOT_GITHUB_TOKEN:-}" ]; then
    echo "Warning: COPILOT_GITHUB_TOKEN not set, falling back to GH_TOKEN"
    export COPILOT_GITHUB_TOKEN="${GH_TOKEN:-}"
fi
# Use programmatic mode with --allow-all-tools for CI environment
copilot -p "$PROMPT" --allow-all-tools

VERIFY_SCRIPT="${ROOT}/verify_commit_metadata.sh"
if [ -x "$VERIFY_SCRIPT" ]; then
    if ! "$VERIFY_SCRIPT"; then
        exit 1
    fi
fi
