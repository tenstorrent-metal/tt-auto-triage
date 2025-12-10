#!/bin/bash

# Utility script: download metadata for a single commit using the same
# schema as download_data_between_commits_batch.sh.
#
# Usage:
#   ./download_data_for_single_commit.sh <commit_sha> [output_file]
#
# If output_file is omitted, it defaults to auto_triage/data/commit_info.json
# and the entry is appended to the JSON array (creating it as [] if needed).

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

if [ $# -lt 1 ]; then
  echo -e "${RED}Error: Missing required arguments${NC}" >&2
  echo "Usage: $0 <commit_sha> [output_file]" >&2
  exit 1
fi

COMMIT_SHA="$1"
OUTPUT_FILE="${2:-auto_triage/data/commit_info.json}"

if ! git rev-parse --verify "$COMMIT_SHA" >/dev/null 2>&1; then
  echo -e "${RED}Error: commit '$COMMIT_SHA' not found${NC}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BATCH_SCRIPT="${SCRIPT_DIR}/download_data_between_commits_batch.sh"

if [ ! -x "$BATCH_SCRIPT" ]; then
  echo -e "${RED}Error: helper script '$BATCH_SCRIPT' is missing or not executable${NC}" >&2
  exit 1
fi

OUTPUT_DIR="$(dirname "$OUTPUT_FILE")"
mkdir -p "$OUTPUT_DIR"

# Ensure the output file exists and is a JSON array before appending.
if [ ! -f "$OUTPUT_FILE" ]; then
  echo "[]" > "$OUTPUT_FILE"
fi

echo -e "${GREEN}Downloading metadata for single commit ${COMMIT_SHA}${NC}"
"$BATCH_SCRIPT" "$COMMIT_SHA" "$COMMIT_SHA" 0 "$OUTPUT_FILE"