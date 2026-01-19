#!/bin/bash
#
# Retry logic for deterministic failures on supported hardware.
# This script checks if a retry should be attempted, runs the retry,
# and handles the outcomes (pass, fail-same, fail-different).
#
# Usage:
#   ./retry_on_deterministic.sh <job_name> <workflow_name>
#
# Environment variables required:
#   SLACK_BOT_TOKEN - Slack bot token for sending notifications
#   SLACK_CHANNEL_ID - Slack channel ID to post to
#   GH_TOKEN - GitHub token for API calls
#   GITHUB_TOKEN - GitHub token (fallback)
#   COPILOT_GITHUB_TOKEN - Token for Copilot CLI
#
# Outputs:
#   Sets RETRY_RESULT to one of: "no_retry", "passed", "failed_same", "failed_different"
#   Modifies slack_message.json and explanation.md as needed

set -euo pipefail

# ============================================================================
# TESTING MODE FLAG
# Set to "true" to force retry regardless of case/hardware (for testing only)
# Set to "false" for normal production behavior
# ============================================================================
TEST_MODE="true"
# ============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

if [ $# -lt 2 ]; then
    echo -e "${RED}Usage: $0 <job_name> <workflow_name>${NC}" >&2
    exit 1
fi

JOB_NAME="$1"
WORKFLOW_NAME="$2"
SLACK_TS="${3:-}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${ROOT}/auto_triage/data"
OUTPUT_DIR="${ROOT}/auto_triage/output"
LOGS_DIR="${ROOT}/auto_triage/logs"
SLACK_MSG_PATH="${OUTPUT_DIR}/slack_message.json"
EXPLANATION_PATH="${OUTPUT_DIR}/explanation.md"
SUBJOB_RUNS_PATH="${DATA_DIR}/subjob_runs.json"

# Output file to signal retry result to calling script
RETRY_RESULT_FILE="${DATA_DIR}/retry_result.json"

OWNER="tenstorrent"
REPO="tt-metal"

# Initialize retry result
echo '{"result": "no_retry", "message": ""}' > "$RETRY_RESULT_FILE"

# Check if slack_message.json exists
if [ ! -f "$SLACK_MSG_PATH" ]; then
    if [ "$TEST_MODE" = "true" ]; then
        echo -e "${YELLOW}========================================${NC}"
        echo -e "${YELLOW}TEST MODE: No slack_message.json found, but will try to get job info from subjob_runs.json${NC}"
        echo -e "${YELLOW}========================================${NC}"
        SCENARIO="(cancelled/no analysis)"
    else
        echo -e "${YELLOW}No slack_message.json found, skipping retry logic${NC}"
        exit 0
    fi
else
    # Read the scenario field
    SCENARIO=$(jq -r '.scenario // ""' "$SLACK_MSG_PATH")
fi

echo -e "${BLUE}Scenario: ${SCENARIO}${NC}"

# TEST_MODE: Skip all eligibility checks
if [ "$TEST_MODE" = "true" ]; then
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}TEST MODE ENABLED - Forcing retry regardless of case/hardware${NC}"
    echo -e "${YELLOW}========================================${NC}"
else
    # Check if this is a Case 1 or Case 4 (deterministic failure with commits)
    if [ "$SCENARIO" != "Deterministic failure with identified commit" ] && \
       [ "$SCENARIO" != "Deterministic failure with multiple plausible commits" ]; then
        echo -e "${YELLOW}Not a Case 1/4 scenario, skipping retry${NC}"
        exit 0
    fi

    # Check if job name contains supported hardware (N150, N300, P150, P300)
    # Case-insensitive check
    JOB_NAME_LOWER=$(echo "$JOB_NAME" | tr '[:upper:]' '[:lower:]')
    if ! echo "$JOB_NAME_LOWER" | grep -qiE '(n150|n300|p150|p300)'; then
        echo -e "${YELLOW}Job '$JOB_NAME' does not contain N150/N300/P150/P300, skipping retry${NC}"
        echo -e "${YELLOW}(Jobs with galaxy, T3K, or p100 are too expensive for automatic retries)${NC}"
        exit 0
    fi

    # Check for expensive hardware that should NOT be retried
    if echo "$JOB_NAME_LOWER" | grep -qiE '(galaxy|t3k|t3000|p100)'; then
        echo -e "${YELLOW}Job '$JOB_NAME' contains expensive hardware (galaxy/T3K/p100), skipping retry${NC}"
        exit 0
    fi
fi

echo -e "${GREEN}Retry conditions met: proceeding with retry${NC}"

# Get the failing job URL and extract IDs
# First try slack_message.json, then fall back to subjob_runs.json
FAILING_RUN_URL=""
if [ -f "$SLACK_MSG_PATH" ]; then
    FAILING_RUN_URL=$(jq -r '.failing_run_url // ""' "$SLACK_MSG_PATH")
fi

# If no URL from slack_message.json, try subjob_runs.json (for cancelled runs or TEST_MODE)
if [ -z "$FAILING_RUN_URL" ] && [ -f "$SUBJOB_RUNS_PATH" ]; then
    echo -e "${BLUE}Getting failing job URL from subjob_runs.json...${NC}"
    # Get the most recent failure (highest run_number with status "failure")
    FAILING_RUN_URL=$(jq -r '
        (if type == "array" then . else (.runs // []) end) |
        map(select(.status == "failure")) |
        sort_by(.run_number // 0) |
        last |
        .job_url // .run_url // ""
    ' "$SUBJOB_RUNS_PATH" 2>/dev/null || echo "")
    
    if [ -n "$FAILING_RUN_URL" ] && [ "$FAILING_RUN_URL" != "null" ]; then
        echo -e "${GREEN}Found failing job URL from subjob_runs.json: ${FAILING_RUN_URL}${NC}"
    fi
fi

if [ -z "$FAILING_RUN_URL" ] || [ "$FAILING_RUN_URL" = "null" ]; then
    echo -e "${RED}No failing_run_url found in slack_message.json or subjob_runs.json${NC}"
    exit 0
fi

# Parse run ID and job ID from URL
# Format: https://github.com/tenstorrent/tt-metal/actions/runs/RUN_ID/job/JOB_ID
RUN_ID=$(echo "$FAILING_RUN_URL" | sed -n 's#.*/runs/\([0-9]\+\)/job/.*#\1#p')
JOB_ID=$(echo "$FAILING_RUN_URL" | sed -n 's#.*/job/\([0-9]\+\).*#\1#p')

if [ -z "$RUN_ID" ] || [ -z "$JOB_ID" ]; then
    echo -e "${RED}Could not parse run_id/job_id from URL: $FAILING_RUN_URL${NC}"
    exit 0
fi

echo -e "${BLUE}Run ID: $RUN_ID, Job ID: $JOB_ID${NC}"

# Save the original error message for comparison
ORIGINAL_ERROR=""
if [ -f "$SLACK_MSG_PATH" ]; then
    ORIGINAL_ERROR=$(jq -r '.failure_message // ""' "$SLACK_MSG_PATH")
fi
# Also try error_message.txt from filter stage if no error in slack_message.json
if [ -z "$ORIGINAL_ERROR" ] && [ -f "${DATA_DIR}/error_message.txt" ]; then
    ORIGINAL_ERROR=$(cat "${DATA_DIR}/error_message.txt" 2>/dev/null || echo "")
    echo -e "${BLUE}Got error message from error_message.txt${NC}"
fi
if [ -z "$ORIGINAL_ERROR" ]; then
    ORIGINAL_ERROR="(error message not available)"
fi
mkdir -p "$DATA_DIR"
echo "$ORIGINAL_ERROR" > "${DATA_DIR}/original_error.txt"

# Re-run the failed job
echo -e "${GREEN}Re-running failed job...${NC}"

# Get the current run_attempt BEFORE triggering rerun
RUN_INFO_BEFORE=$(gh api "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}" 2>/dev/null || echo "{}")
OLD_ATTEMPT=$(echo "$RUN_INFO_BEFORE" | jq -r '.run_attempt // 1')
echo -e "${BLUE}Current run_attempt before rerun: ${OLD_ATTEMPT}${NC}"

# GitHub API to re-run failed jobs in a workflow run
# POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs
# NOTE: This API returns 201 with empty body on success
# NOTE: Requires 'actions: write' permission on GITHUB_TOKEN
RERUN_RESPONSE=$(gh api \
    --method POST \
    "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}/rerun-failed-jobs" \
    -i 2>&1 || echo "API_ERROR")

# Extract HTTP status code from response headers
RERUN_HTTP_CODE=$(echo "$RERUN_RESPONSE" | head -1 | awk '{print $2}')
RERUN_HTTP_CODE="${RERUN_HTTP_CODE:-000}"

echo -e "${BLUE}Rerun API response code: ${RERUN_HTTP_CODE}${NC}"

if [ "$RERUN_HTTP_CODE" != "201" ] && [ "$RERUN_HTTP_CODE" != "200" ]; then
    # Show error details
    echo -e "${RED}Failed to re-run failed jobs (HTTP ${RERUN_HTTP_CODE})${NC}"
    ERROR_MSG=$(echo "$RERUN_RESPONSE" | grep -A5 '"message"' | head -3 || echo "")
    if [ -n "$ERROR_MSG" ]; then
        echo -e "${RED}Error details: ${ERROR_MSG}${NC}"
    fi
    
    if [ "$RERUN_HTTP_CODE" = "403" ]; then
        echo -e "${YELLOW}NOTE: 403 Forbidden usually means the GITHUB_TOKEN needs 'actions: write' permission${NC}"
        echo -e "${YELLOW}Add 'permissions: actions: write' to your workflow file${NC}"
    fi
    
    # Try the alternative: re-run entire workflow run
    echo -e "${YELLOW}Trying to re-run entire workflow run...${NC}"
    RERUN_RESPONSE=$(gh api \
        --method POST \
        "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}/rerun" \
        -i 2>&1 || echo "API_ERROR")
    
    RERUN_HTTP_CODE=$(echo "$RERUN_RESPONSE" | head -1 | awk '{print $2}')
    RERUN_HTTP_CODE="${RERUN_HTTP_CODE:-000}"
    
    echo -e "${BLUE}Full rerun API response code: ${RERUN_HTTP_CODE}${NC}"
    
    if [ "$RERUN_HTTP_CODE" != "201" ] && [ "$RERUN_HTTP_CODE" != "200" ]; then
        echo -e "${RED}Failed to re-run workflow (HTTP ${RERUN_HTTP_CODE})${NC}"
        if [ "$RERUN_HTTP_CODE" = "403" ]; then
            echo -e "${YELLOW}NOTE: 403 Forbidden - GITHUB_TOKEN needs 'actions: write' permission${NC}"
        fi
        echo -e "${YELLOW}Proceeding without retry${NC}"
        exit 0
    fi
fi

echo -e "${GREEN}Re-run triggered successfully${NC}"

# Wait for the new attempt to be created and become visible
echo -e "${BLUE}Waiting for new run attempt to start...${NC}"
NEW_ATTEMPT="$OLD_ATTEMPT"
WAIT_FOR_START=0
MAX_WAIT_FOR_START=120  # 2 minutes max to wait for new attempt to appear

while [ "$NEW_ATTEMPT" = "$OLD_ATTEMPT" ] && [ $WAIT_FOR_START -lt $MAX_WAIT_FOR_START ]; do
    sleep 10
    WAIT_FOR_START=$((WAIT_FOR_START + 10))
    RUN_INFO=$(gh api "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}" 2>/dev/null || echo "{}")
    NEW_ATTEMPT=$(echo "$RUN_INFO" | jq -r '.run_attempt // 1')
    RUN_STATUS=$(echo "$RUN_INFO" | jq -r '.status // "unknown"')
    echo -e "${BLUE}  Waited ${WAIT_FOR_START}s - run_attempt: ${NEW_ATTEMPT}, status: ${RUN_STATUS}${NC}"
done

if [ "$NEW_ATTEMPT" = "$OLD_ATTEMPT" ]; then
    echo -e "${RED}New run attempt did not start within ${MAX_WAIT_FOR_START}s${NC}"
    echo -e "${YELLOW}Proceeding without retry${NC}"
    exit 0
fi

NEW_RUN_URL="https://github.com/${OWNER}/${REPO}/actions/runs/${RUN_ID}/attempts/${NEW_ATTEMPT}"

echo -e "${GREEN}New run attempt started: ${NEW_ATTEMPT}${NC}"
echo -e "${GREEN}Retry run URL: ${NEW_RUN_URL}${NC}"

# Send quick Slack notification about the retry
send_retry_notification() {
    local message="$1"
    local payload
    
    if [ -n "$SLACK_TS" ]; then
        payload=$(jq -n --arg text "$message" --arg ts "$SLACK_TS" '{text: $text, thread_ts: $ts}')
    else
        payload=$(jq -n --arg text "$message" '{text: $text}')
    fi
    
    if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_CHANNEL_ID:-}" ]; then
        echo -e "${BLUE}Sending Slack notification...${NC}"
        SLACK_RESPONSE=$(curl -s -X POST "https://slack.com/api/chat.postMessage" \
            -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "$(echo "$payload" | jq --arg channel "$SLACK_CHANNEL_ID" '. + {channel: $channel}')" \
            2>&1)
        
        SLACK_OK=$(echo "$SLACK_RESPONSE" | jq -r '.ok // false' 2>/dev/null || echo "false")
        if [ "$SLACK_OK" = "true" ]; then
            echo -e "${GREEN}Slack notification sent successfully${NC}"
        else
            SLACK_ERROR=$(echo "$SLACK_RESPONSE" | jq -r '.error // "unknown"' 2>/dev/null || echo "unknown")
            echo -e "${YELLOW}Warning: Slack notification failed: ${SLACK_ERROR}${NC}"
        fi
    else
        echo -e "${YELLOW}Slack credentials not set, skipping notification${NC}"
    fi
}

# Send notification about retry
# Use printf to create actual newlines (not literal \n)
if [ "$TEST_MODE" = "true" ]; then
    RETRY_MSG=$(printf ':test_tube: *[TEST MODE]* Re-running job for testing:\n<%s|View retry run>\n\n_Workflow:_ %s\n_Job:_ %s' "$NEW_RUN_URL" "$WORKFLOW_NAME" "$JOB_NAME")
else
    RETRY_MSG=$(printf ':arrows_counterclockwise: *Deterministic failure suspected.* Re-running job to confirm:\n<%s|View retry run>\n\n_Workflow:_ %s\n_Job:_ %s' "$NEW_RUN_URL" "$WORKFLOW_NAME" "$JOB_NAME")
fi
send_retry_notification "$RETRY_MSG"

# Wait for job to complete
echo -e "${BLUE}Waiting for retry job to complete...${NC}"
MAX_WAIT_MINUTES=180  # 3 hours max wait
WAIT_INTERVAL=60  # Check every minute
WAITED=0

while [ $WAITED -lt $((MAX_WAIT_MINUTES * 60)) ]; do
    sleep $WAIT_INTERVAL
    WAITED=$((WAITED + WAIT_INTERVAL))
    
    echo -e "${BLUE}Checking job status (waited ${WAITED}s)...${NC}"
    
    # Get the run status
    RUN_STATUS=$(gh api "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}" 2>/dev/null || echo "{}")
    STATUS=$(echo "$RUN_STATUS" | jq -r '.status // "unknown"')
    CONCLUSION=$(echo "$RUN_STATUS" | jq -r '.conclusion // "null"')
    
    echo -e "  Status: ${STATUS}, Conclusion: ${CONCLUSION}"
    
    if [ "$STATUS" = "completed" ]; then
        echo -e "${GREEN}Retry job completed with conclusion: ${CONCLUSION}${NC}"
        break
    fi
    
    if [ "$STATUS" = "unknown" ]; then
        echo -e "${RED}Failed to get run status${NC}"
        exit 0
    fi
done

if [ "$STATUS" != "completed" ]; then
    echo -e "${RED}Timeout waiting for retry job to complete${NC}"
    exit 0
fi

# Find the specific job in the retry attempt
echo -e "${BLUE}Finding retry job results...${NC}"
RETRY_JOBS=$(gh api "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}/attempts/${NEW_ATTEMPT}/jobs?per_page=100" 2>&1 || echo "{}")

# Debug: Check what we got back
echo -e "${BLUE}API response type check...${NC}"
RESPONSE_TYPE=$(echo "$RETRY_JOBS" | jq -r 'type' 2>/dev/null || echo "invalid")
echo -e "${BLUE}Response type: ${RESPONSE_TYPE}${NC}"

if [ "$RESPONSE_TYPE" = "invalid" ] || [ "$RESPONSE_TYPE" = "string" ]; then
    echo -e "${RED}API returned invalid response or error: ${RETRY_JOBS}${NC}"
    echo -e "${YELLOW}Proceeding with original analysis${NC}"
    exit 0
fi

# Check if jobs array exists
JOBS_COUNT=$(echo "$RETRY_JOBS" | jq '.jobs | length' 2>/dev/null || echo "0")
echo -e "${BLUE}Found ${JOBS_COUNT} jobs in retry attempt${NC}"

if [ "$JOBS_COUNT" = "0" ]; then
    echo -e "${YELLOW}No jobs found in retry attempt, checking main jobs endpoint...${NC}"
    # Try the main jobs endpoint instead
    RETRY_JOBS=$(gh api "repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}/jobs?per_page=100" 2>&1 || echo "{}")
    JOBS_COUNT=$(echo "$RETRY_JOBS" | jq '.jobs | length' 2>/dev/null || echo "0")
    echo -e "${BLUE}Found ${JOBS_COUNT} jobs from main endpoint${NC}"
fi

# Normalize the job name for matching (handle unicode dashes, lowercase)
normalize_name() {
    echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[–—−‐‑‒]/-/g'
}

JOB_NAME_NORMALIZED=$(normalize_name "$JOB_NAME")
echo -e "${BLUE}Looking for job matching: ${JOB_NAME_NORMALIZED}${NC}"

# Find the matching job by name - try exact match first, then partial
RETRY_JOB=$(echo "$RETRY_JOBS" | jq --arg name "$JOB_NAME_NORMALIZED" '
    def normalize: ascii_downcase | gsub("[–—−‐‑‒]"; "-");
    .jobs // [] | 
    map(select((.name | normalize) == $name or (.name | normalize | contains($name)) or ($name | contains(.name | normalize)))) |
    sort_by(.status == "completed" | not) |
    first // null
' 2>/dev/null || echo "null")

if [ "$RETRY_JOB" = "null" ] || [ -z "$RETRY_JOB" ]; then
    echo -e "${YELLOW}Could not find job by name match, trying to find any failed job...${NC}"
    # Try to find any failed job in the retry attempt
    RETRY_JOB=$(echo "$RETRY_JOBS" | jq '
        .jobs // [] | 
        map(select(.status == "completed" and .conclusion == "failure")) |
        first // null
    ' 2>/dev/null || echo "null")
fi

if [ "$RETRY_JOB" = "null" ] || [ -z "$RETRY_JOB" ]; then
    echo -e "${YELLOW}No failed jobs found, trying any completed job...${NC}"
    # Fall back to any completed job
    RETRY_JOB=$(echo "$RETRY_JOBS" | jq '
        .jobs // [] | 
        map(select(.status == "completed")) |
        first // null
    ' 2>/dev/null || echo "null")
fi

# List all job names for debugging
echo -e "${BLUE}All jobs in response:${NC}"
echo "$RETRY_JOBS" | jq -r '.jobs // [] | .[].name' 2>/dev/null || echo "(none)"

RETRY_JOB_ID=$(echo "$RETRY_JOB" | jq -r '.id // ""')
RETRY_JOB_CONCLUSION=$(echo "$RETRY_JOB" | jq -r '.conclusion // "unknown"')

if [ -z "$RETRY_JOB_ID" ] || [ "$RETRY_JOB_ID" = "null" ]; then
    echo -e "${RED}Could not find retry job ID${NC}"
    echo -e "${YELLOW}Proceeding with original analysis${NC}"
    exit 0
fi

RETRY_JOB_URL="https://github.com/${OWNER}/${REPO}/actions/runs/${RUN_ID}/job/${RETRY_JOB_ID}"

echo -e "${BLUE}Retry job ID: ${RETRY_JOB_ID}${NC}"
echo -e "${BLUE}Retry job conclusion: ${RETRY_JOB_CONCLUSION}${NC}"
echo -e "${BLUE}Retry job URL: ${RETRY_JOB_URL}${NC}"

# TEST_MODE: Skip all outcome handling, just send notification and proceed with original message
if [ "$TEST_MODE" = "true" ]; then
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}TEST MODE: Retry completed with conclusion: ${RETRY_JOB_CONCLUSION}${NC}"
    echo -e "${YELLOW}TEST MODE: Skipping message modifications, will send original message${NC}"
    echo -e "${YELLOW}========================================${NC}"
    
    # Send a test notification about the result
    if [ "$RETRY_JOB_CONCLUSION" = "success" ]; then
        send_retry_notification "$(printf ':test_tube: *[TEST MODE]* Retry completed: *PASSED*\n\nRetry run: <%s|link>\n\n_Original message will be sent unchanged._' "$RETRY_JOB_URL")"
    elif [ "$RETRY_JOB_CONCLUSION" = "failure" ]; then
        send_retry_notification "$(printf ':test_tube: *[TEST MODE]* Retry completed: *FAILED*\n\nRetry run: <%s|link>\n\n_Original message will be sent unchanged._' "$RETRY_JOB_URL")"
    else
        send_retry_notification "$(printf ':test_tube: *[TEST MODE]* Retry completed: *%s*\n\nRetry run: <%s|link>\n\n_Original message will be sent unchanged._' "$RETRY_JOB_CONCLUSION" "$RETRY_JOB_URL")"
    fi
    
    echo -e "${GREEN}TEST MODE: Retry logic completed, proceeding with original analysis${NC}"
    exit 0
fi

# Handle the three outcomes (normal mode)
if [ "$RETRY_JOB_CONCLUSION" = "success" ]; then
    # ========================================
    # CASE: Retry PASSED - Convert to Case 3
    # ========================================
    echo -e "${GREEN}Retry passed! Converting to Case 3 (non-deterministic)${NC}"
    
    jq -n --arg result "passed" --arg msg "Retry passed, failure is non-deterministic" \
        '{result: $result, message: $msg}' > "$RETRY_RESULT_FILE"
    
    # Update slack_message.json to Case 3
    FAILURE_MSG=$(jq -r '.failure_message // "Unknown error"' "$SLACK_MSG_PATH")
    
    jq --arg scenario "Failure likely outside tt-metal" \
       --arg case_num "3" \
       --arg failing_url "$FAILING_RUN_URL" \
       --arg retry_url "$RETRY_JOB_URL" \
       --arg slack_msg "Failure is non-deterministic. The job passed on retry. Please investigate flakiness or infrastructure issues." \
       '. + {
           scenario: $scenario,
           case: $case_num,
           notes: ("This failure passed on automatic retry, indicating a non-deterministic/flaky issue rather than a code regression. Original failure: " + $failing_url + " | Successful retry: " + $retry_url),
           slack_message: $slack_msg,
           commits: []
       }' "$SLACK_MSG_PATH" > "${SLACK_MSG_PATH}.tmp"
    mv "${SLACK_MSG_PATH}.tmp" "$SLACK_MSG_PATH"
    
    # Update explanation.md
    cat > "$EXPLANATION_PATH" << EOF
# Auto Triage Explanation: ${JOB_NAME}

## Failure is Non-Deterministic (Passed on Retry)

The original analysis suspected a deterministic failure, but an automatic retry of the job **passed successfully**.

This indicates the failure is likely:
- A flaky test
- An infrastructure/timing issue
- A transient environmental problem

### Original Failure
- **Link:** [${FAILING_RUN_URL}](${FAILING_RUN_URL})
- **Error Message:**
\`\`\`
${ORIGINAL_ERROR}
\`\`\`

### Successful Retry
- **Link:** [${RETRY_JOB_URL}](${RETRY_JOB_URL})

### Recommendation
Investigate test flakiness or infrastructure stability. No code changes appear to be required.

---
_This analysis was performed automatically by the auto-triage system._
EOF

    send_retry_notification "$(printf ':white_check_mark: *Retry passed!* Failure appears to be non-deterministic.\n\nOriginal failure: <%s|link>\nSuccessful retry: <%s|link>' "$FAILING_RUN_URL" "$RETRY_JOB_URL")"

elif [ "$RETRY_JOB_CONCLUSION" = "failure" ]; then
    # ========================================
    # CASE: Retry FAILED - Need to compare errors
    # ========================================
    echo -e "${YELLOW}Retry also failed. Comparing error messages...${NC}"
    
    # Download retry job logs/annotations
    RETRY_LOGS_DIR="${LOGS_DIR}/retry_job_${RETRY_JOB_ID}"
    mkdir -p "$RETRY_LOGS_DIR"
    
    # Get annotations for retry job
    echo -e "${BLUE}Fetching retry job annotations...${NC}"
    "${ROOT}/get_annotations.sh" "$RETRY_JOB_URL" "${RETRY_LOGS_DIR}/annotations.json" 2>/dev/null || true
    
    # Extract error message from retry
    RETRY_ERROR=""
    if [ -f "${RETRY_LOGS_DIR}/annotations.json" ]; then
        # Annotation levels might be capitalized or lowercase
        RETRY_ERROR=$(jq -r '
            [.[] | select((.annotation_level | ascii_downcase) == "failure" or (.annotation_level | ascii_downcase) == "error")] |
            map(.message // .raw_details // "") |
            map(select(. != "")) |
            first // ""
        ' "${RETRY_LOGS_DIR}/annotations.json" 2>/dev/null || echo "")
    fi
    
    # If no error from annotations, try to get from logs
    if [ -z "$RETRY_ERROR" ]; then
        echo -e "${YELLOW}No annotations found, trying logs...${NC}"
        "${ROOT}/get_logs.sh" "$RETRY_JOB_URL" "${LOGS_DIR}/retry" 2>/dev/null || true
        # Try to extract error from logs (simplified)
        if [ -d "${LOGS_DIR}/retry/job_${RETRY_JOB_ID}" ]; then
            RETRY_ERROR=$(find "${LOGS_DIR}/retry/job_${RETRY_JOB_ID}" -name "*.txt" -exec grep -h -A5 -E "(ERROR|FAIL|Exception|AssertionError)" {} \; 2>/dev/null | head -20 || echo "")
        fi
    fi
    
    if [ -z "$RETRY_ERROR" ]; then
        RETRY_ERROR="Could not extract error message from retry job"
    fi
    
    # Save retry error for comparison
    echo "$RETRY_ERROR" > "${DATA_DIR}/retry_error.txt"
    
    # Call Copilot to compare errors
    echo -e "${BLUE}Calling Copilot to compare error messages...${NC}"
    
    COMPARE_INSTRUCTIONS="${ROOT}/compare_errors_instructions.txt"
    if [ ! -f "$COMPARE_INSTRUCTIONS" ]; then
        echo -e "${RED}compare_errors_instructions.txt not found${NC}"
        # Default to assuming different errors if we can't compare
        SAME_FAILURE="false"
    else
        read -r -d '' COMPARE_PROMPT <<EOF || true
You are operating in a CI environment. Compare two error messages and determine if they represent the same failure.

$(cat "$COMPARE_INSTRUCTIONS")
EOF
        
        # Ensure COPILOT_GITHUB_TOKEN is set
        if [ -z "${COPILOT_GITHUB_TOKEN:-}" ]; then
            export COPILOT_GITHUB_TOKEN="${GH_TOKEN:-}"
        fi
        
        # Run Copilot comparison
        cd "$ROOT"
        copilot -p "$COMPARE_PROMPT" --allow-all-tools 2>/dev/null || true
        
        # Read the comparison result
        COMPARISON_FILE="${DATA_DIR}/error_comparison.json"
        if [ -f "$COMPARISON_FILE" ]; then
            SAME_FAILURE=$(jq -r '.same_failure // false' "$COMPARISON_FILE")
        else
            echo -e "${YELLOW}No comparison result, assuming different failures${NC}"
            SAME_FAILURE="false"
        fi
    fi
    
    echo -e "${BLUE}Same failure: ${SAME_FAILURE}${NC}"
    
    if [ "$SAME_FAILURE" = "true" ]; then
        # ========================================
        # SUB-CASE: Failed with SAME error
        # ========================================
        echo -e "${RED}Retry failed with SAME error - confirming deterministic failure${NC}"
        
        jq -n --arg result "failed_same" --arg msg "Retry failed with same error, confirming deterministic issue" \
            '{result: $result, message: $msg}' > "$RETRY_RESULT_FILE"
        
        # Add note to original slack message about confirmed failure
        jq --arg retry_url "$RETRY_JOB_URL" '
            .notes = ((.notes // "") + "\n\n*RETRY CONFIRMED DETERMINISTIC ISSUE:* The job was automatically retried and failed with the same error.\n- Retry link: " + $retry_url + "\n- _Failing retry indicates this is a genuine deterministic issue, not flakiness._")
        ' "$SLACK_MSG_PATH" > "${SLACK_MSG_PATH}.tmp"
        mv "${SLACK_MSG_PATH}.tmp" "$SLACK_MSG_PATH"
        
        # Prepend note to explanation.md
        EXISTING_EXPLANATION=$(cat "$EXPLANATION_PATH" 2>/dev/null || echo "")
        cat > "$EXPLANATION_PATH" << EOF
## Failure Was Repeatable (Confirmed Deterministic)

The job was automatically retried and **failed with the same error**, confirming this is a deterministic issue.

- **First failure:** [${FAILING_RUN_URL}](${FAILING_RUN_URL})
- **Retry failure:** [${RETRY_JOB_URL}](${RETRY_JOB_URL})

---

${EXISTING_EXPLANATION}
EOF

        send_retry_notification "$(printf ':x: *Retry also failed with the same error.* Deterministic failure confirmed.\n\nFirst failure: <%s|link>\nRetry failure: <%s|link>' "$FAILING_RUN_URL" "$RETRY_JOB_URL")"
        
    else
        # ========================================
        # SUB-CASE: Failed with DIFFERENT error
        # ========================================
        echo -e "${YELLOW}Retry failed with DIFFERENT error - both failures appear non-deterministic${NC}"
        
        jq -n --arg result "failed_different" --arg msg "Retry failed with different error, both appear non-deterministic" \
            --arg retry_url "$RETRY_JOB_URL" --arg retry_error "$RETRY_ERROR" \
            '{result: $result, message: $msg, retry_url: $retry_url, retry_error: $retry_error}' > "$RETRY_RESULT_FILE"
        
        # Update original slack message to Case 3
        ORIGINAL_FAILURE_MSG=$(jq -r '.failure_message // "Unknown error"' "$SLACK_MSG_PATH")
        
        # Use jq's proper string escaping for the error messages
        jq --arg scenario "Failure likely outside tt-metal" \
           --arg case_num "3" \
           --arg failing_url "$FAILING_RUN_URL" \
           --arg orig_error "$ORIGINAL_ERROR" \
           --arg retry_url "$RETRY_JOB_URL" \
           --arg retry_err "$RETRY_ERROR" \
           --arg slack_msg "Failure appears non-deterministic. Two consecutive runs failed with different errors. Please investigate test flakiness or infrastructure issues." \
           '. + {
               scenario: $scenario,
               case: $case_num,
               notes: ("Two consecutive failures with DIFFERENT error messages suggest non-deterministic issues rather than a code regression.\n\nOriginal failure: " + $failing_url + "\nOriginal error: " + $orig_error + "\n\nRetry failure: " + $retry_url + "\nRetry error: " + $retry_err),
               slack_message: $slack_msg,
               commits: []
           }' "$SLACK_MSG_PATH" > "${SLACK_MSG_PATH}.tmp"
        mv "${SLACK_MSG_PATH}.tmp" "$SLACK_MSG_PATH"
        
        # Update explanation.md
        cat > "$EXPLANATION_PATH" << EOF
# Auto Triage Explanation: ${JOB_NAME}

## Failure Seems Non-Deterministic (Different Errors on Retry)

The original analysis suspected a deterministic failure, but an automatic retry failed with a **different error message**.

This suggests both failures may be:
- Flaky tests with multiple failure modes
- Infrastructure instability
- Race conditions or timing issues

### First Failure
- **Link:** [${FAILING_RUN_URL}](${FAILING_RUN_URL})
- **Error Message:**
\`\`\`
${ORIGINAL_ERROR}
\`\`\`

### Second Failure (Retry)
- **Link:** [${RETRY_JOB_URL}](${RETRY_JOB_URL})
- **Error Message:**
\`\`\`
${RETRY_ERROR}
\`\`\`

### Recommendation
Both failures should be investigated as potential flakiness issues. The different error messages suggest this is not a simple code regression.

---
_This analysis was performed automatically by the auto-triage system._
EOF

        send_retry_notification "$(printf ':warning: *Retry failed with a DIFFERENT error.* Both failures appear non-deterministic.\n\nFirst failure: <%s|link>\nRetry failure: <%s|link>\n\n_Different error messages suggest flakiness rather than a code regression._' "$FAILING_RUN_URL" "$RETRY_JOB_URL")"
    fi
else
    # Unknown conclusion (cancelled, skipped, etc.)
    echo -e "${YELLOW}Retry job had unexpected conclusion: ${RETRY_JOB_CONCLUSION}${NC}"
    echo -e "${YELLOW}Proceeding with original analysis${NC}"
fi

echo -e "${GREEN}Retry logic completed${NC}"
