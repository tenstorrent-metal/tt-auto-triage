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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALL_ERRORS_FILE = os.path.join(SCRIPT_DIR, "all_errors.json")
ISSUE_DUMP_FILE = os.path.join(SCRIPT_DIR, "issue_dump.json")
REPORT_JSON_FILE = os.path.join(SCRIPT_DIR, "error_report.json")
REPORT_MARKDOWN_FILE = os.path.join(SCRIPT_DIR, "error_report.md")

# Import error similarity helper
from error_similarity import find_best_matching_centroid

# Similarity thresholds (same as sync_new_errors.py)
RAPIDFUZZ_THRESHOLD = 50.0
SEMANTIC_THRESHOLD = 70.0

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
        }
    except FileNotFoundError:
        print(f"ERROR: secrets.json not found at {SECRETS_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {SECRETS_FILE}: {e}")
        sys.exit(1)

def get_centroid_to_issue_mapping(issue_dump: List[Dict[str, Any]]) -> Dict[str, int]:
    """Get mapping from centroid error to issue number by fetching GitHub issues."""
    import requests
    import re
    
    secrets = load_secrets()
    repo_owner = secrets["GITHUB_REPO_OWNER"]
    repo_name = secrets["GITHUB_REPO_NAME"]
    github_token = secrets["GITHUB_TOKEN"]
    
    if not github_token:
        print("⚠ Warning: github_token not found, cannot fetch issue URLs")
        return {}
    
    # Build a set of centroids for faster lookup (normalized for case-insensitive matching)
    centroid_lookup = {}
    for entry in issue_dump:
        centroid_error = entry.get("centroid_error", "")
        if centroid_error:
            centroid_lookup[centroid_error.strip()] = centroid_error
            centroid_lookup[centroid_error.strip().lower()] = centroid_error
    
    if not centroid_lookup:
        print("  No centroids found in issue_dump, skipping issue mapping")
        return {}
    
    # Fetch GitHub issues
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    centroid_to_issue = {}
    found_count = 0
    
    print(f"Fetching GitHub issues to map {len(issue_dump)} centroid(s) to issue URLs...")
    
    # Only fetch open issues (closed issues shouldn't be in the active issue dump)
    page = 1
    per_page = 100
    
    while True:
        try:
            params = {
                "state": "open",
                "per_page": per_page,
                "page": page
            }
            
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            issues = response.json()
            
            # Filter out pull requests
            actual_issues = [issue for issue in issues if "pull_request" not in issue]
            
            # Extract centroid from issue body and map to issue number
            for issue in actual_issues:
                issue_body = issue.get("body", "")
                pattern = r"## Error Message\s*```\s*(.+?)\s*```"
                match = re.search(pattern, issue_body, re.DOTALL)
                if match:
                    centroid_from_issue = match.group(1).strip()
                    centroid_normalized = centroid_from_issue.strip()
                    centroid_lower = centroid_normalized.lower()
                    
                    # Fast lookup using pre-built dictionary
                    matched_centroid = None
                    if centroid_normalized in centroid_lookup:
                        matched_centroid = centroid_lookup[centroid_normalized]
                    elif centroid_lower in centroid_lookup:
                        matched_centroid = centroid_lookup[centroid_lower]
                    
                    if matched_centroid and matched_centroid not in centroid_to_issue:
                        centroid_to_issue[matched_centroid] = issue["number"]
                        found_count += 1
            
            # Early exit if we've found all centroids
            if found_count >= len(issue_dump):
                print(f"  Found all {found_count} centroid(s), stopping fetch")
                break
            
            if len(issues) < per_page:
                break
            
            page += 1
            import time
            time.sleep(0.5)  # Rate limiting
            
        except Exception as e:
            print(f"⚠ Warning: Error fetching issues: {e}")
            break
    
    print(f"  Mapped {len(centroid_to_issue)} centroid(s) to issue numbers")
    return centroid_to_issue

def generate_error_report() -> tuple[List[Dict[str, Any]], str]:
    """
    Generate error report from all_errors.json and issue_dump.json.
    
    Returns:
        Tuple of (report_data, markdown_summary)
    """
    print("Loading data files...")
    # Load all errors
    try:
        with open(ALL_ERRORS_FILE, 'r', encoding='utf-8') as f:
            all_errors = json.load(f)
        print(f"  ✓ Loaded {len(all_errors)} error(s) from {ALL_ERRORS_FILE}")
    except FileNotFoundError:
        print(f"ERROR: {ALL_ERRORS_FILE} not found")
        sys.exit(1)
    
    # Load issue dump
    try:
        with open(ISSUE_DUMP_FILE, 'r', encoding='utf-8') as f:
            issue_dump = json.load(f)
        print(f"  ✓ Loaded {len(issue_dump)} issue(s) from {ISSUE_DUMP_FILE}")
    except FileNotFoundError:
        print(f"⚠ Warning: {ISSUE_DUMP_FILE} not found, centroid URLs will be missing")
        issue_dump = []
    
    # Filter issue_dump to only include entries from open issues
    # We need to check GitHub to see which issues are open
    print("\nFiltering issue_dump to exclude closed issues...")
    secrets = load_secrets()
    repo_owner = secrets["GITHUB_REPO_OWNER"]
    repo_name = secrets["GITHUB_REPO_NAME"]
    github_token = secrets.get("GITHUB_TOKEN", "")
    
    if github_token:
        # Get mapping to check which centroids belong to open issues
        centroid_to_issue = get_centroid_to_issue_mapping(issue_dump)
        original_count = len(issue_dump)
        issue_dump = [
            entry for entry in issue_dump
            if entry.get("centroid_error", "") in centroid_to_issue
        ]
        filtered_count = original_count - len(issue_dump)
        if filtered_count > 0:
            print(f"  Removed {filtered_count} entry/entries from closed issues")
        print(f"  {len(issue_dump)} open issue(s) remaining")
    else:
        print("  ⚠ Warning: No GitHub token - cannot filter closed issues")
    
    print(f"  ✓ Found {len(issue_dump)} issue group(s) to match against")
    
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
    
    # Filter to only include new errors (not already in issue_dump)
    print(f"\nFiltering errors to only include new ones (not already in issues)...")
    new_errors = []
    skipped_existing = 0
    skipped_no_url = 0
    
    for error_entry in all_errors:
        job_url = error_entry[1] if len(error_entry) > 1 else None
        
        if not job_url:
            skipped_no_url += 1
            continue
        
        if job_url in existing_urls:
            skipped_existing += 1
            continue
        
        new_errors.append(error_entry)
    
    print(f"  Total errors in all_errors.json: {len(all_errors)}")
    print(f"  Skipped (no URL): {skipped_no_url}")
    print(f"  Skipped (already exists in issue_dump): {skipped_existing}")
    print(f"  New errors to include in report: {len(new_errors)}")
    
    # Generate report entries (only for new errors)
    print(f"\nGenerating report entries for {len(new_errors)} new error(s)...")
    report_entries = []
    matched_count = 0
    unmatched_count = 0
    
    for idx, error_entry in enumerate(new_errors, 1):
        if idx % 50 == 0 or idx == len(new_errors):
            print(f"  Processing error {idx}/{len(new_errors)}...")
        error_message = error_entry[0] if len(error_entry) > 0 else ""
        job_url = error_entry[1] if len(error_entry) > 1 else None
        timestamp_str = error_entry[2] if len(error_entry) > 2 else ""
        is_nd = error_entry[5] if len(error_entry) > 5 else False
        
        if not error_message or not job_url:
            if not job_url:
                unmatched_count += 1
            continue
        
        # Extract timestamp and commit hash for this error
        timestamp_utc = parse_timestamp_to_utc(timestamp_str) if timestamp_str else None
        commit_hash = extract_commit_hash(error_message)
        
        # Look up URL directly in issue_dump (no similarity matching needed)
        # Note: Since we filtered out existing URLs, this should only match if the error
        # was just added in this run but hasn't been saved to issue_dump yet
        centroid_run_url = None
        centroid_error_message = None
        centroid_timestamp_utc = None
        centroid_commit_hash = None
        oldest_run = None
        
        if job_url in url_to_entry:
            matched_count += 1
            entry = url_to_entry[job_url]
            centroid_error_message = entry.get("centroid_error", "")
            
            # Extract commit hash from centroid error message
            if centroid_error_message:
                centroid_commit_hash = extract_commit_hash(centroid_error_message)
            
            # Get centroid run URL (first URL in failing_runs list)
            failing_runs = entry.get("failing_runs", [])
            if failing_runs:
                # Use the first URL as the centroid run URL
                centroid_run_url = failing_runs[0]
                # Get timestamp for centroid run
                centroid_timestamp_str = url_to_timestamp.get(centroid_run_url, "")
                if centroid_timestamp_str:
                    centroid_timestamp_utc = parse_timestamp_to_utc(centroid_timestamp_str)
            
            # Find oldest run in this centroid group
            oldest_run = find_oldest_run_in_centroid(entry, all_errors, url_to_timestamp, url_to_error_message)
        else:
            unmatched_count += 1
        
        report_entry = {
            "job_url": job_url,
            "error_message": error_message,
            "is_nd": is_nd,
            "timestamp_utc": timestamp_utc,
            "commit_hash": commit_hash,
            "centroid_run_url": centroid_run_url,
            "centroid_error_message": centroid_error_message,
            "centroid_timestamp_utc": centroid_timestamp_utc,
            "centroid_commit_hash": centroid_commit_hash,
            "oldest_run": oldest_run
        }
        report_entries.append(report_entry)
    
    print(f"\n  ✓ Generated {len(report_entries)} report entries")
    print(f"    - Matched to centroids: {matched_count}")
    print(f"    - Unmatched: {unmatched_count}")
    print(f"    - With centroid run URLs: {sum(1 for e in report_entries if e['centroid_run_url'])}")
    
    # Generate markdown summary
    print("\nGenerating markdown summary...")
    total_errors = len(report_entries)
    nd_errors = sum(1 for entry in report_entries if entry["is_nd"])
    errors_with_centroid = sum(1 for entry in report_entries if entry["centroid_run_url"])
    
    nd_percentage = (nd_errors/total_errors*100) if total_errors > 0 else 0
    centroid_percentage = (errors_with_centroid/total_errors*100) if total_errors > 0 else 0
    
    markdown = f"""# Error Report Summary

## Statistics

- **Total Errors**: {total_errors}
- **ND (Non-Deterministic) Errors**: {nd_errors} ({nd_percentage:.1f}% of total)
- **Errors with Centroid Runs**: {errors_with_centroid} ({centroid_percentage:.1f}% of total)
- **Matched to Centroids**: {matched_count}
- **Unmatched Errors**: {unmatched_count}

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
