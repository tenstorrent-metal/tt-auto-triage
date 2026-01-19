#!/usr/bin/env python3
"""
Generate error report JSON and markdown summary.
Creates a report with job URL, error message, ND flag, and centroid run URL.
The report is GitHub-independent and suitable for SQL database storage.
"""

import json
import os
import sys
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Tuple

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALL_ERRORS_FILE = os.path.join(SCRIPT_DIR, "all_errors.json")
ISSUE_DUMP_FILE = os.path.join(SCRIPT_DIR, "issue_dump.json")
SLACK_EXPORT_FILE = os.path.join(SCRIPT_DIR, "build_slack_export_with_threads.json")
REPORT_JSON_FILE = os.path.join(SCRIPT_DIR, "error_report.json")
REPORT_MARKDOWN_FILE = os.path.join(SCRIPT_DIR, "error_report.md")

# Import error similarity helper
from error_similarity import find_best_matching_centroid

# Similarity thresholds (same as sync_new_errors.py)
# High thresholds to prevent matching different errors that share boilerplate
RAPIDFUZZ_THRESHOLD = 70.0
SEMANTIC_THRESHOLD = 85.0

def parse_timestamp_to_utc(timestamp_str: str) -> Optional[str]:
    """Parse formatted timestamp string to UTC ISO format.
    
    Handles formats like "January 9th, 8:59am, 58.95 seconds"
    The original timestamp from extract_errors.py is a Unix timestamp (UTC),
    but the formatted version is in local time. We need to parse it back.
    
    Returns ISO 8601 format in UTC: "2026-01-09T13:59:58.950Z"
    """
    if not timestamp_str:
        return None
    
    try:
        # Try to parse the format: "January 9th, 8:59am, 58.95 seconds"
        parts = timestamp_str.split(", ")
        if len(parts) >= 3:
            date_part = parts[0]  # "January 9th"
            time_part = parts[1]  # "8:59am"
            seconds_part = parts[2]  # "58.95 seconds"
            
            # Extract seconds value
            seconds_match = re.search(r'(\d+\.?\d*)', seconds_part)
            seconds_value = float(seconds_match.group(1)) if seconds_match else 0
            
            # Remove ordinal suffix (st, nd, rd, th)
            date_part_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_part)
            
            # Parse date and time (assumes local timezone)
            try:
                dt_local = datetime.strptime(f"{date_part_clean}, {time_part}", "%B %d, %I:%M%p")
                # Add seconds
                dt_local = dt_local.replace(second=int(seconds_value), microsecond=int((seconds_value % 1) * 1000000))
                
                # Determine the correct year
                current_year = datetime.now().year
                dt_local = dt_local.replace(year=current_year)
                now = datetime.now()
                
                # If the date is more than 6 months in the future, assume it's from last year
                if dt_local > now + timedelta(days=180):
                    dt_local = dt_local.replace(year=current_year - 1)
                elif dt_local < now - timedelta(days=180):
                    # Only adjust if we're in January and the date is December (likely from previous year)
                    if now.month == 1 and dt_local.month == 12:
                        dt_local = dt_local.replace(year=current_year - 1)
                
                # Convert local time to UTC
                # Get local timezone offset
                local_tz = datetime.now().astimezone().tzinfo
                dt_aware = dt_local.replace(tzinfo=local_tz)
                dt_utc = dt_aware.astimezone(timezone.utc)
                
                # Return ISO 8601 format with milliseconds
                return dt_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt_utc.microsecond / 1000):03d}Z"
            except ValueError:
                pass
    except Exception:
        pass
    
    return None

def extract_commit_hash(error_message: str) -> Optional[str]:
    """Extract commit hash from error message.
    
    Looks for commit hashes (typically 7-40 character hex strings).
    Common patterns:
    - "commit abc1234"
    - "abc1234"
    - "abc1234567890abcdef..."
    """
    if not error_message:
        return None
    
    # Pattern for commit hash: 7-40 hex characters, possibly prefixed with "commit" or similar
    # Look for standalone hex strings of 7+ characters
    patterns = [
        r'\bcommit\s+([a-fA-F0-9]{7,40})\b',  # "commit abc1234"
        r'\b([a-fA-F0-9]{7,40})\b',  # Standalone hash
    ]
    
    for pattern in patterns:
        match = re.search(pattern, error_message)
        if match:
            commit_hash = match.group(1)
            # Prefer shorter hashes (7 chars) as they're more likely to be commit SHAs
            if 7 <= len(commit_hash) <= 40:
                return commit_hash
    
    return None


def get_slack_message_link(timestamp_str: str, channel_id: str, slack_token: Optional[str] = None) -> Optional[str]:
    """Construct Slack message link from timestamp and channel ID.
    
    Args:
        timestamp_str: Unix timestamp string (e.g., "1768333360.325209")
        channel_id: Slack channel ID (e.g., "C08SJ7MGESY")
        slack_token: Optional Slack token to fetch workspace info
    
    Returns:
        Slack message URL or None if timestamp is invalid
    """
    if not timestamp_str or not channel_id:
        return None
    
    try:
        # Try to use Slack API to get permalink if token is available (most reliable)
        if slack_token:
            try:
                # Use chat.getPermalink API to get the correct permalink
                response = requests.post(
                    "https://slack.com/api/chat.getPermalink",
                    headers={
                        "Authorization": f"Bearer {slack_token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "channel": channel_id,
                        "message_ts": timestamp_str
                    }
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") and "permalink" in data:
                        return data["permalink"]
            except Exception as e:
                print(f"  ⚠ Warning: Could not fetch Slack permalink via API: {e}")
        
        # Fallback: construct URL manually
        # Parse timestamp - Slack wants format: p{seconds}{microseconds} (16 digits total)
        timestamp_float = float(timestamp_str)
        seconds = int(timestamp_float)
        microseconds = int((timestamp_float - seconds) * 1000000)
        
        # Format: p{seconds}{microseconds padded to 6 digits} = 16 digits total
        # Example: 1768333360.325209 -> p1768333360325209
        timestamp_padded = f"{seconds}{microseconds:06d}"
        
        # Try to get workspace domain from Slack API
        workspace_domain = None
        if slack_token:
            try:
                # Get team info to get workspace domain
                response = requests.get(
                    "https://slack.com/api/team.info",
                    headers={"Authorization": f"Bearer {slack_token}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") and "team" in data:
                        workspace_domain = data["team"].get("domain")
            except Exception:
                pass
        
        if workspace_domain:
            return f"https://{workspace_domain}.slack.com/archives/{channel_id}/p{timestamp_padded}"
        else:
            # Return None if we can't get workspace domain (better than broken link with placeholder)
            return None
        
    except (ValueError, TypeError) as e:
        print(f"  ⚠ Warning: Could not parse Slack timestamp: {e}")
        return None

def parse_timestamp_to_datetime(timestamp_str: str) -> Optional[datetime]:
    """Parse formatted timestamp string to datetime object.
    
    Handles formats like "January 9th, 8:59am, 58.95 seconds"
    Returns datetime object in UTC timezone.
    """
    if not timestamp_str:
        return None
    
    try:
        # Try to parse the format: "January 9th, 8:59am, 58.95 seconds"
        parts = timestamp_str.split(", ")
        if len(parts) >= 2:
            date_part = parts[0]  # "January 9th"
            time_part = parts[1]  # "8:59am"
            
            # Remove ordinal suffix (st, nd, rd, th)
            date_part_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_part)
            
            # Parse date and time (assumes local timezone)
            try:
                dt_local = datetime.strptime(f"{date_part_clean}, {time_part}", "%B %d, %I:%M%p")
                # Add seconds if available
                if len(parts) >= 3:
                    seconds_part = parts[2]  # "58.95 seconds"
                    seconds_match = re.search(r'(\d+\.?\d*)', seconds_part)
                    if seconds_match:
                        seconds_value = float(seconds_match.group(1))
                        dt_local = dt_local.replace(second=int(seconds_value), microsecond=int((seconds_value % 1) * 1000000))
                
                # Determine the correct year
                current_year = datetime.now().year
                dt_local = dt_local.replace(year=current_year)
                now = datetime.now()
                
                # If the date is more than 6 months in the future, assume it's from last year
                if dt_local > now + timedelta(days=180):
                    dt_local = dt_local.replace(year=current_year - 1)
                elif dt_local < now - timedelta(days=180):
                    # Only adjust if we're in January and the date is December (likely from previous year)
                    if now.month == 1 and dt_local.month == 12:
                        dt_local = dt_local.replace(year=current_year - 1)
                
                # Convert local time to UTC
                local_tz = datetime.now().astimezone().tzinfo
                dt_aware = dt_local.replace(tzinfo=local_tz)
                dt_utc = dt_aware.astimezone(timezone.utc)
                
                return dt_utc
            except ValueError:
                pass
    except Exception:
        pass
    
    return None

def is_within_last_month(timestamp_str: str) -> bool:
    """Check if a timestamp string represents a date within the last month."""
    if not timestamp_str:
        return False
    
    dt = parse_timestamp_to_datetime(timestamp_str)
    if dt is None:
        return False
    
    one_month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    return dt >= one_month_ago

def find_oldest_run_in_centroid(entry: Dict[str, Any], all_errors: List[List], url_to_timestamp: Dict[str, str], url_to_error_message: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Find the oldest run in the same centroid group.
    
    Returns dict with: timestamp_utc, commit_hash, run_url, error_message
    """
    failing_runs = entry.get("failing_runs", [])
    if not failing_runs:
        return None
    
    oldest_run = None
    oldest_timestamp = None
    
    for url in failing_runs:
        # Get timestamp for this URL
        timestamp_str = url_to_timestamp.get(url, "")
        if not timestamp_str:
            continue
        
        # Parse timestamp to datetime for comparison
        dt = None
        try:
            parts = timestamp_str.split(", ")
            if len(parts) >= 2:
                date_part = parts[0]
                time_part = parts[1]
                date_part_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_part)
                dt_local = datetime.strptime(f"{date_part_clean}, {time_part}", "%B %d, %I:%M%p")
                current_year = datetime.now().year
                dt_local = dt_local.replace(year=current_year)
                now = datetime.now()
                if dt_local > now + timedelta(days=180):
                    dt_local = dt_local.replace(year=current_year - 1)
                elif dt_local < now - timedelta(days=180):
                    if now.month == 1 and dt_local.month == 12:
                        dt_local = dt_local.replace(year=current_year - 1)
                
                # Convert to UTC for comparison
                local_tz = datetime.now().astimezone().tzinfo
                dt_aware = dt_local.replace(tzinfo=local_tz)
                dt = dt_aware.astimezone(timezone.utc)
        except Exception:
            continue
        
        if dt and (oldest_timestamp is None or dt < oldest_timestamp):
            oldest_timestamp = dt
            # Find error message for this URL
            error_message = url_to_error_message.get(url)
            commit_hash = None
            if error_message:
                commit_hash = extract_commit_hash(error_message)
            
            oldest_run = {
                "timestamp_utc": parse_timestamp_to_utc(timestamp_str),
                "commit_hash": commit_hash,
                "run_url": url,
                "error_message": error_message
            }
    
    return oldest_run

def load_secrets():
    """Load configuration from secrets.json file."""
    SECRETS_FILE = os.path.join(SCRIPT_DIR, "secrets.json")
    try:
        with open(SECRETS_FILE, 'r') as f:
            secrets = json.load(f)
        return {
            "GITHUB_REPO_OWNER": secrets.get("github_repo_owner", ""),
            "GITHUB_REPO_NAME": secrets.get("github_repo_name", ""),
            "GITHUB_TOKEN": secrets.get("github_token", ""),
            "CHANNEL_ID": secrets.get("channel_id", ""),
        }
    except FileNotFoundError:
        print(f"ERROR: secrets.json not found at {SECRETS_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {SECRETS_FILE}: {e}")
        sys.exit(1)

def generate_error_report() -> tuple[List[Dict[str, Any]], str]:
    """
    Generate error report from all_errors.json and issue_dump.json.
    
    Returns:
        Tuple of (report_data, markdown_summary)
    """
    # Check GitHub API rate limit at start
    secrets = load_secrets()
    github_token = secrets.get("GITHUB_TOKEN", "")
    start_rate_limit = None
    if github_token:
        from github_api_utils import log_rate_limit_status, check_github_rate_limit
        log_rate_limit_status(github_token, "start")
        start_rate_limit = check_github_rate_limit(github_token)
    
    print("Loading data files...")
    # Load all errors
    try:
        with open(ALL_ERRORS_FILE, 'r', encoding='utf-8') as f:
            all_errors = json.load(f)
        print(f"  ✓ Loaded {len(all_errors)} error(s) from {ALL_ERRORS_FILE}")
    except FileNotFoundError:
        print(f"ERROR: {ALL_ERRORS_FILE} not found")
        sys.exit(1)
    
    # Refresh issue dump from GitHub before loading (ensures we have latest data after rebuild)
    print(f"\nRefreshing issue dump from GitHub...")
    try:
        import download_issue_dump
        download_issue_dump.main()
        print(f"  ✓ Refreshed issue dump from GitHub")
    except Exception as e:
        print(f"  ⚠ Warning: Could not refresh issue dump: {e}")
        print(f"  Will use existing {ISSUE_DUMP_FILE} if available")
    
    # Load issue dump (already filtered to only open issues by download_issue_dump.py)
    try:
        with open(ISSUE_DUMP_FILE, 'r', encoding='utf-8') as f:
            issue_dump = json.load(f)
        print(f"  ✓ Loaded {len(issue_dump)} open issue(s) from {ISSUE_DUMP_FILE}")
    except FileNotFoundError:
        print(f"⚠ Warning: {ISSUE_DUMP_FILE} not found, centroid URLs will be missing")
        issue_dump = []
    
    # Build reverse lookup: URL -> issue entry (much faster than similarity matching)
    print(f"\nBuilding URL to issue mapping...")
    url_to_entry = {}
    existing_urls = set()
    for entry in issue_dump:
        failing_runs = entry.get("failing_runs", [])
        for url in failing_runs:
            if url:
                url_to_entry[url] = entry
                existing_urls.add(url)
    print(f"  ✓ Mapped {len(url_to_entry)} URL(s) to issue entries")
    print(f"  ✓ Found {len(existing_urls)} existing URL(s) in issue_dump")
    
    # Build URL to timestamp and error message mappings from all_errors (for finding oldest runs)
    print(f"\nBuilding URL to timestamp and error message mappings from all errors...")
    url_to_timestamp = {}
    url_to_error_message = {}
    for error_entry in all_errors:
        if len(error_entry) > 1:
            job_url = error_entry[1]
            if job_url:
                if len(error_entry) > 2:
                    timestamp_str = error_entry[2]
                    if timestamp_str:
                        url_to_timestamp[job_url] = timestamp_str
                if len(error_entry) > 0:
                    error_message = error_entry[0]
                    if error_message:
                        url_to_error_message[job_url] = error_message
    print(f"  ✓ Mapped {len(url_to_timestamp)} URL(s) to timestamps")
    print(f"  ✓ Mapped {len(url_to_error_message)} URL(s) to error messages")
    
    # Load Slack export to get original timestamps for Slack message links
    print(f"\nLoading Slack export for message links...")
    url_to_slack_timestamp = {}
    slack_token = secrets.get("slack_token", "")
    channel_id = secrets.get("CHANNEL_ID", "")
    try:
        with open(SLACK_EXPORT_FILE, 'r', encoding='utf-8') as f:
            slack_data = json.load(f)
        print(f"  ✓ Loaded {len(slack_data)} Slack message(s)")
        
        # Map failing_run URLs to Slack timestamps
        # Try multiple extraction patterns to catch different URL formats
        for entry in slack_data:
            failing_run = entry.get("failing_run", "")
            slack_timestamp = entry.get("timestamp", "")
            if failing_run and slack_timestamp:
                url = None
                
                # Try pattern 1: URL in parentheses: "Run #123 (https://...)"
                url_pattern1 = r"\(https?://[^\)]+\)"
                match = re.search(url_pattern1, failing_run)
                if match:
                    url = match.group(0)[1:-1]  # Remove parentheses
                
                # Try pattern 2: Plain URL in the text
                if not url:
                    url_pattern2 = r"https?://[^\s\)]+"
                    match = re.search(url_pattern2, failing_run)
                    if match:
                        url = match.group(0).rstrip('.,;:')  # Remove trailing punctuation
                
                # Try pattern 3: Markdown link format: [text](url)
                if not url:
                    link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
                    match = re.search(link_pattern, failing_run)
                    if match:
                        url = match.group(2)  # Get URL from markdown link
                
                if url:
                    # Normalize URL (remove trailing slash, etc.) for consistent lookup
                    url_normalized = url.rstrip('/')
                    url_to_slack_timestamp[url_normalized] = slack_timestamp
                    # Also store with trailing slash for lookup flexibility
                    if url_normalized != url:
                        url_to_slack_timestamp[url] = slack_timestamp
                    # Store original URL as well
                    url_to_slack_timestamp[url] = slack_timestamp
        
        print(f"  ✓ Mapped {len(url_to_slack_timestamp)} URL(s) to Slack timestamps")
    except FileNotFoundError:
        print(f"  ⚠ Warning: {SLACK_EXPORT_FILE} not found, Slack message links will be missing")
    except Exception as e:
        print(f"  ⚠ Warning: Could not load Slack export: {e}")
    
    # Filter to only include errors from the last month
    print(f"\nFiltering errors to only include those from the last month...")
    recent_errors = []
    skipped_no_url = 0
    skipped_old = 0
    skipped_no_timestamp = 0
    
    for error_entry in all_errors:
        job_url = error_entry[1] if len(error_entry) > 1 else None
        timestamp_str = error_entry[2] if len(error_entry) > 2 else ""
        
        if not job_url:
            skipped_no_url += 1
            continue
        
        if not timestamp_str:
            skipped_no_timestamp += 1
            continue
        
        # Only include errors from the last month
        if not is_within_last_month(timestamp_str):
            skipped_old += 1
            continue
        
        recent_errors.append(error_entry)
    
    print(f"  Total errors in all_errors.json: {len(all_errors)}")
    print(f"  Skipped (no URL): {skipped_no_url}")
    print(f"  Skipped (no timestamp): {skipped_no_timestamp}")
    print(f"  Skipped (older than 1 month): {skipped_old}")
    print(f"  Errors from last month: {len(recent_errors)}")
    
    # Generate report entries (for all errors from last month)
    print(f"\nGenerating report entries for {len(recent_errors)} error(s) from last month...")
    report_entries = []
    matched_count = 0
    unmatched_count = 0
    
    for idx, error_entry in enumerate(recent_errors, 1):
        if idx % 50 == 0 or idx == len(recent_errors):
            print(f"  Processing error {idx}/{len(recent_errors)}...")
        error_message = error_entry[0] if len(error_entry) > 0 else ""
        job_url = error_entry[1] if len(error_entry) > 1 else None
        timestamp_str = error_entry[2] if len(error_entry) > 2 else ""
        is_nd = error_entry[5] if len(error_entry) > 5 else False
        
        if not error_message or not job_url:
            if not job_url:
                unmatched_count += 1
            continue
        
        # Extract timestamp for this error
        timestamp_utc = parse_timestamp_to_utc(timestamp_str) if timestamp_str else None
        
        # Get commit hash from issue_dump (already extracted from issue body by download_issue_dump.py)
        # No need to fetch from GitHub API - commit hashes are stored in the issue body when we create/update issues
        commit_hash = None
        if job_url in url_to_entry:
            entry = url_to_entry[job_url]
            run_metadata = entry.get("run_metadata", {})
            if job_url in run_metadata:
                commit_hash = run_metadata[job_url].get("commit_hash", "") or None
        
        # Get Slack message link - try multiple URL formats
        slack_message_link = None
        slack_timestamp = url_to_slack_timestamp.get(job_url, "")
        
        # Also try matching with trailing slash or other variations
        if not slack_timestamp:
            # Try variations of the URL
            url_variations = [
                job_url.rstrip('/'),
                job_url + '/',
                job_url.replace('/job/', '/jobs/'),  # Some URLs might use /jobs/ instead
            ]
            for url_var in url_variations:
                if url_var in url_to_slack_timestamp:
                    slack_timestamp = url_to_slack_timestamp[url_var]
                    break
        
        if slack_timestamp and channel_id:
            slack_message_link = get_slack_message_link(slack_timestamp, channel_id, slack_token)
            # If link generation failed, log a warning
            if not slack_message_link and idx % 100 == 0:
                print(f"    ⚠ Warning: Could not generate Slack link for error {idx} (timestamp: {slack_timestamp[:20]}...)")
        
        # Look up URL in issue_dump to get centroid info (if available)
        # Note: Errors may or may not be in issue_dump - include them either way
        centroid_run_url = None
        centroid_error_message = None
        centroid_timestamp_utc = None
        centroid_commit_hash = None
        oldest_run = None
        
        if job_url in url_to_entry:
            matched_count += 1
            entry = url_to_entry[job_url]
            centroid_error_message = entry.get("centroid_error", "")
            
            # Get centroid run URL (first URL in failing_runs list)
            failing_runs = entry.get("failing_runs", [])
            if failing_runs:
                # Use the first URL as the centroid run URL
                centroid_run_url = failing_runs[0]
                # Get timestamp for centroid run
                centroid_timestamp_str = url_to_timestamp.get(centroid_run_url, "")
                if centroid_timestamp_str:
                    centroid_timestamp_utc = parse_timestamp_to_utc(centroid_timestamp_str)
                
                # Get commit hash for centroid run from issue_dump (already stored in issue body)
                run_metadata = entry.get("run_metadata", {})
                if centroid_run_url in run_metadata:
                    centroid_commit_hash = run_metadata[centroid_run_url].get("commit_hash", "") or None
            
            # Find oldest run in this centroid group
            oldest_run = find_oldest_run_in_centroid(entry, all_errors, url_to_timestamp, url_to_error_message)
            
            # Get commit hash for oldest run from issue_dump (already stored in issue body)
            if oldest_run and oldest_run.get("run_url"):
                oldest_url = oldest_run["run_url"]
                if oldest_url in url_to_entry:
                    entry = url_to_entry[oldest_url]
                    run_metadata = entry.get("run_metadata", {})
                    if oldest_url in run_metadata:
                        oldest_commit_hash = run_metadata[oldest_url].get("commit_hash", "")
                        if oldest_commit_hash:
                            oldest_run["commit_hash"] = oldest_commit_hash
        else:
            unmatched_count += 1
        
        report_entry = {
            "job_url": job_url,
            "error_message": error_message,
            "is_nd": is_nd,
            "timestamp_utc": timestamp_utc,
            "commit_hash": commit_hash,  # Full 40-char commit SHA from GitHub API
            "slack_message_link": slack_message_link,
            "centroid_run_url": centroid_run_url,
            "centroid_error_message": centroid_error_message,
            "centroid_timestamp_utc": centroid_timestamp_utc,
            "centroid_commit_hash": centroid_commit_hash,  # Full 40-char commit SHA from GitHub API
            "oldest_run": oldest_run  # Includes full commit_hash if available
        }
        report_entries.append(report_entry)
    
    print(f"\n  ✓ Generated {len(report_entries)} report entries")
    print(f"    - Matched to centroids: {matched_count}")
    print(f"    - Unmatched: {unmatched_count}")
    print(f"    - With centroid run URLs: {sum(1 for e in report_entries if e['centroid_run_url'])}")
    
    # Check GitHub API rate limit at end
    end_rate_limit = None
    api_used = None
    api_remaining = None
    if github_token:
        from github_api_utils import log_rate_limit_status, check_github_rate_limit
        log_rate_limit_status(github_token, "end")
        end_rate_limit = check_github_rate_limit(github_token)
        
        # Calculate API usage
        if start_rate_limit and end_rate_limit:
            start_remaining = start_rate_limit.get("remaining", 0)
            end_remaining = end_rate_limit.get("remaining", 0)
            api_used = start_remaining - end_remaining
            api_remaining = end_remaining
    
    # Generate markdown summary
    print("\nGenerating markdown summary...")
    total_errors = len(report_entries)
    nd_errors = sum(1 for entry in report_entries if entry["is_nd"])
    errors_with_centroid = sum(1 for entry in report_entries if entry["centroid_run_url"])
    
    nd_percentage = (nd_errors/total_errors*100) if total_errors > 0 else 0
    centroid_percentage = (errors_with_centroid/total_errors*100) if total_errors > 0 else 0
    
    # Build API usage section
    api_section = ""
    if api_used is not None and api_remaining is not None:
        api_section = f"""
- **GitHub API Used**: {api_used:,} requests
- **GitHub API Remaining**: {api_remaining:,} requests"""
    
    markdown = f"""# Error Report Summary

## Statistics

- **Total Errors**: {total_errors}
- **ND (Non-Deterministic) Errors**: {nd_errors} ({nd_percentage:.1f}% of total)
- **Errors with Centroid Runs**: {errors_with_centroid} ({centroid_percentage:.1f}% of total)
- **Matched to Centroids**: {matched_count}
- **Unmatched Errors**: {unmatched_count}{api_section}

## Error Breakdown

### ND Errors by Status

- **ND Errors with Centroid Runs**: {sum(1 for e in report_entries if e['is_nd'] and e['centroid_run_url'])}
- **ND Errors without Centroid Runs**: {sum(1 for e in report_entries if e['is_nd'] and not e['centroid_run_url'])}

### Non-ND Errors by Status

- **Non-ND Errors with Centroid Runs**: {sum(1 for e in report_entries if not e['is_nd'] and e['centroid_run_url'])}
- **Non-ND Errors without Centroid Runs**: {sum(1 for e in report_entries if not e['is_nd'] and not e['centroid_run_url'])}

## Sample Errors

"""
    
    # Add sample entries (first 10)
    for i, entry in enumerate(report_entries[:10], 1):
        nd_badge = "🟡 ND" if entry["is_nd"] else "⚪"
        centroid_link = f"[Centroid Run]({entry['centroid_run_url']})" if entry["centroid_run_url"] else "*No centroid run*"
        job_link = f"[Job URL]({entry['job_url']})" if entry["job_url"] else "*No job URL*"
        
        error_preview = entry["error_message"][:100].replace("\n", " ") + "..." if len(entry["error_message"]) > 100 else entry["error_message"]
        centroid_preview = ""
        if entry.get("centroid_error_message"):
            centroid_msg = entry["centroid_error_message"]
            centroid_preview = centroid_msg[:100].replace("\n", " ") + "..." if len(centroid_msg) > 100 else centroid_msg
            centroid_preview = f"\n- **Centroid Error Message**: `{centroid_preview}`"
        
        markdown += f"""### Error {i} {nd_badge}

- **Job**: {job_link}
- **Centroid Run**: {centroid_link}
- **Error Message**: `{error_preview}`{centroid_preview}

"""
    
    if len(report_entries) > 10:
        markdown += f"\n*... and {len(report_entries) - 10} more errors (see artifact for full list)*\n"
    
    return report_entries, markdown

def main():
    """Main function."""
    print("="*80)
    print("Generating error report...")
    print("="*80)
    
    report_entries, markdown_summary = generate_error_report()
    
    # Save JSON report
    print(f"\nSaving JSON report to {REPORT_JSON_FILE}...")
    with open(REPORT_JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(report_entries, f, indent=2, ensure_ascii=False)
    print(f"✓ Saved {len(report_entries)} error entries to {REPORT_JSON_FILE}")
    
    # Save markdown report
    print(f"\nSaving markdown report to {REPORT_MARKDOWN_FILE}...")
    with open(REPORT_MARKDOWN_FILE, 'w', encoding='utf-8') as f:
        f.write(markdown_summary)
    print(f"✓ Saved markdown report to {REPORT_MARKDOWN_FILE}")
    
    # Also write to GitHub Actions summary if GITHUB_STEP_SUMMARY is set
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        print(f"\nWriting to GitHub Actions summary...")
        with open(github_summary, 'a', encoding='utf-8') as f:
            f.write(markdown_summary)
        print(f"✓ Added summary to GitHub Actions step summary")

if __name__ == "__main__":
    main()
