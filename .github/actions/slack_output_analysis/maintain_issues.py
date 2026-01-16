#!/usr/bin/env python3
"""
Maintain GitHub issues: ensure they're sorted chronologically and have proper data.
Handles cleanup of old runs, sorting, and metadata updates.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

# ============================================================================
# File paths
# ============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, "secrets.json")
ALL_ERRORS_FILE = os.path.join(SCRIPT_DIR, "all_errors.json")
ISSUE_DUMP_FILE = os.path.join(SCRIPT_DIR, "issue_dump.json")

# ============================================================================
# CONFIGURATION - Load from secrets.json
# ============================================================================

def load_secrets():
    """Load configuration from secrets.json file."""
    try:
        with open(SECRETS_FILE, 'r') as f:
            secrets = json.load(f)
        return {
            "GITHUB_TOKEN": secrets.get("github_token", ""),
            "GITHUB_REPO_OWNER": secrets.get("github_repo_owner", ""),
            "GITHUB_REPO_NAME": secrets.get("github_repo_name", ""),
            "PROJECT_OWNER": secrets.get("project_owner", ""),
            "PROJECT_NAME": secrets.get("project_name", ""),
            "PROJECT_NUMBER": secrets.get("project_number", ""),
            "PROJECT_FIELD_ID": secrets.get("project_field_id", "")
        }
    except FileNotFoundError:
        print(f"ERROR: secrets.json not found at {SECRETS_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {SECRETS_FILE}: {e}")
        sys.exit(1)

# Load secrets
_secrets = load_secrets()
GITHUB_TOKEN = _secrets["GITHUB_TOKEN"]
GITHUB_REPO_OWNER = _secrets["GITHUB_REPO_OWNER"]
GITHUB_REPO_NAME = _secrets["GITHUB_REPO_NAME"]
PROJECT_OWNER = _secrets["PROJECT_OWNER"]
PROJECT_NAME = _secrets["PROJECT_NAME"]
PROJECT_NUMBER = _secrets["PROJECT_NUMBER"]
PROJECT_FIELD_ID = _secrets["PROJECT_FIELD_ID"]

# ============================================================================
# GitHub API Functions
# ============================================================================

def get_all_issues(open_only: bool = False) -> List[Dict[str, Any]]:
    """Get all issues from the repository.
    
    Args:
        open_only: If True, only fetch open issues. If False, fetch all issues.
    """
    import requests
    
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    all_issues = []
    page = 1
    per_page = 100
    
    while True:
        params = {
            "state": "open" if open_only else "all",
            "per_page": per_page,
            "page": page
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            issues = response.json()
            
            # Filter out pull requests
            actual_issues = [issue for issue in issues if "pull_request" not in issue]
            all_issues.extend(actual_issues)
            
            if len(issues) < per_page:
                break
            
            page += 1
            time.sleep(0.5)  # Rate limiting
            
        except Exception as e:
            print(f"ERROR: Failed to get issues: {e}")
            break
    
    return all_issues

def extract_centroid_from_issue_body(issue_body: str) -> str:
    """Extract the centroid error from the issue body."""
    # Look for the Error Message section with code block
    pattern = r"## Error Message\s*```\s*(.+?)\s*```"
    match = re.search(pattern, issue_body, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    
    return ""

def update_issue(issue_number: int, title: str, body: str) -> Dict[str, Any]:
    """Update an existing GitHub issue."""
    import requests
    
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues/{issue_number}"
    
    # Try with "Bearer" first (for fine-grained PATs), then fall back to "token"
    auth_methods = [
        ("Bearer", f"Bearer {GITHUB_TOKEN}"),
        ("token", f"token {GITHUB_TOKEN}")
    ]
    
    data = {
        "title": title,
        "body": body
    }
    
    last_error = None
    for method_name, auth_header in auth_methods:
        headers = {
            "Authorization": auth_header,
            "Accept": "application/vnd.github.v3+json"
        }
        
        try:
            response = requests.patch(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            last_error = e
            if e.response.status_code == 403:
                # Try next auth method
                continue
            else:
                print(f"\nERROR: HTTP {e.response.status_code}")
                print(f"Response: {e.response.text}")
                raise
        except Exception as e:
            last_error = e
            continue
    
    if last_error:
        raise last_error
    raise Exception("Failed to update issue")

def close_issue(issue_number: int) -> Dict[str, Any]:
    """Close an existing GitHub issue."""
    import requests
    
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues/{issue_number}"
    
    # Try with "Bearer" first (for fine-grained PATs), then fall back to "token"
    auth_methods = [
        ("Bearer", f"Bearer {GITHUB_TOKEN}"),
        ("token", f"token {GITHUB_TOKEN}")
    ]
    
    data = {
        "state": "closed"
    }
    
    last_error = None
    for method_name, auth_header in auth_methods:
        headers = {
            "Authorization": auth_header,
            "Accept": "application/vnd.github.v3+json"
        }
        
        try:
            response = requests.patch(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            last_error = e
            if e.response.status_code == 403:
                # Try next auth method
                continue
            else:
                print(f"\nERROR: HTTP {e.response.status_code}")
                print(f"Response: {e.response.text}")
                raise
        except Exception as e:
            last_error = e
            continue
    
    if last_error:
        raise last_error
    raise Exception("Failed to close issue")

def get_project_node_id() -> Optional[str]:
    """Get the GraphQL node ID for the project."""
    if not PROJECT_OWNER or not PROJECT_NUMBER:
        return None
    
    import requests
    
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Try organization first
    query_org = """
    query($owner: String!, $number: Int!) {
      organization(login: $owner) {
        projectV2(number: $number) {
          id
        }
      }
    }
    """
    
    variables = {
        "owner": PROJECT_OWNER,
        "number": int(PROJECT_NUMBER)
    }
    
    try:
        response = requests.post(url, json={"query": query_org, "variables": variables}, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        if "errors" not in result and result["data"]["organization"]:
            return result["data"]["organization"]["projectV2"]["id"]
    except:
        pass
    
    # Try user account
    query_user = """
    query($owner: String!, $number: Int!) {
      user(login: $owner) {
        projectV2(number: $number) {
          id
        }
      }
    }
    """
    
    try:
        response = requests.post(url, json={"query": query_user, "variables": variables}, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        if "errors" not in result and result["data"]["user"]:
            return result["data"]["user"]["projectV2"]["id"]
    except:
        pass
    
    return None

def get_project_item_id_for_issue(issue_number: int) -> Optional[str]:
    """Get the project item ID for an existing issue."""
    project_node_id = get_project_node_id()
    if not project_node_id:
        return None
    
    import requests
    
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Search through project items to find the one with this issue
    cursor = None
    while True:
        query = """
        query($projectId: ID!, $cursor: String) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100, after: $cursor) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
                nodes {
                  id
                  content {
                    ... on Issue {
                      number
                    }
                  }
                }
              }
            }
          }
        }
        """
        
        variables = {
            "projectId": project_node_id,
            "cursor": cursor
        }
        
        try:
            response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
            response.raise_for_status()
            result = response.json()
            
            if "errors" in result:
                return None
            
            items_data = result["data"]["node"]["items"]
            items = items_data["nodes"]
            
            for item in items:
                content = item.get("content")
                if content and content.get("number") == issue_number:
                    return item["id"]
            
            page_info = items_data["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            
            cursor = page_info["endCursor"]
            time.sleep(0.3)  # Rate limiting
            
        except Exception as e:
            return None
    
    return None

def update_project_field(project_item_id: str, count: int) -> bool:
    """Update the 'number of occurrences' field in the project."""
    if not PROJECT_FIELD_ID:
        return False
    
    project_node_id = get_project_node_id()
    if not project_node_id:
        return False
    
    import requests
    
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    query = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
      updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $fieldId
          value: $value
        }
      ) {
        projectV2Item {
          id
        }
      }
    }
    """
    
    variables = {
        "projectId": project_node_id,
        "itemId": project_item_id,
        "fieldId": PROJECT_FIELD_ID,
        "value": {
            "number": count
        }
    }
    
    try:
        response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        return "errors" not in result
    except:
        return False

# ============================================================================
# Helper Functions
# ============================================================================

def format_issue_body(centroid_error: str, failing_runs: List[str], timestamps: Dict[str, str], run_metadata: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Format the issue body with centroid error and all URLs.
    
    Args:
        centroid_error: The centroid error message
        failing_runs: List of failing run URLs
        timestamps: Dictionary mapping URLs to timestamps
        run_metadata: Optional dictionary mapping URLs to dicts with 'job_name', 'workflow_name', and 'is_nd'
    """
    body_parts = []
    
    count = len(failing_runs)
    
    # Add number of occurrences at the top
    body_parts.append(f"**Number of Occurrences:** {count}")
    body_parts.append("")
    
    # Add centroid link at the top (first URL in failing_runs)
    if failing_runs:
        centroid_url = failing_runs[0]
        body_parts.append(f"**Centroid Run:** [{centroid_url}]({centroid_url})")
        body_parts.append("")
    
    # Add centroid error as the main description
    body_parts.append("## Error Message\n")
    body_parts.append("```")
    body_parts.append(centroid_error)
    body_parts.append("```")
    body_parts.append("")
    
    # Add all run URLs (sorted chronologically)
    body_parts.append("## All Occurrences")
    body_parts.append(f"This error has occurred {count} time(s):")
    body_parts.append("")
    
    # Create list with timestamps and metadata for sorting
    url_list = []
    for url in failing_runs:
        timestamp = timestamps.get(url, "")
        label = timestamp if timestamp else "Link"
        
        # Get job/workflow info and ND flag if available
        job_workflow_suffix = ""
        is_nd = False
        if run_metadata and url in run_metadata:
            meta = run_metadata[url]
            job_name = meta.get("job_name", "")
            workflow_name = meta.get("workflow_name", "")
            is_nd = meta.get("is_nd", False)
            if job_name or workflow_name:
                parts = []
                if workflow_name:
                    parts.append(workflow_name)
                if job_name:
                    parts.append(job_name)
                job_workflow_suffix = f" - {' / '.join(parts)}"
        
        # Add ND marker if applicable
        if is_nd:
            label += " (marked as ND)"
        
        # Parse timestamp for proper chronological sorting
        dt = parse_timestamp(timestamp) if timestamp else None
        url_list.append((dt, label, url, job_workflow_suffix))
    
    # Sort chronologically (newest first), items without timestamps go to the end
    url_list.sort(key=lambda x: (x[0] is not None, x[0] if x[0] is not None else datetime.max), reverse=True)
    
    # Format the list
    for idx, (dt, label, url, job_workflow_suffix) in enumerate(url_list, 1):
        body_parts.append(f"{idx}. [{label}]({url}){job_workflow_suffix}")
    
    return "\n".join(body_parts)

def create_title_from_count(count: int, error_message: str = "", group_num: Optional[int] = None) -> str:
    """Create a title with occurrence count prefix and truncated error message."""
    count_str = f"{count:05d}"
    
    # Calculate available space for error message
    prefix_len = len(f"[{count_str}] ")
    if group_num is not None:
        prefix_len += len(f"Group {group_num}: ")
    max_error_len = 256 - prefix_len - 3  # 3 for "..."
    
    # Truncate error message if needed
    if error_message and len(error_message) > max_error_len:
        truncated = error_message[:max_error_len].rsplit(' ', 1)[0]
        if len(truncated) < max_error_len - 20:
            truncated = error_message[:max_error_len]
        truncated += "..."
    elif error_message:
        truncated = error_message
    else:
        truncated = "Error"
    
    if group_num is not None:
        return f"[{count_str}] Group {group_num}: {truncated}"
    else:
        return f"[{count_str}] {truncated}"

def extract_group_num_from_title(title: str) -> Optional[int]:
    """Extract group number from an existing issue title."""
    pattern = r"\[.*?\]\s*Group\s+(\d+)"
    match = re.search(pattern, title)
    if match:
        return int(match.group(1))
    return None

def get_issue_title(issue_number: int) -> Optional[str]:
    """Get the title of an existing issue."""
    import requests
    
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues/{issue_number}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        issue = response.json()
        return issue.get("title", "")
    except Exception as e:
        print(f"  ⚠ Warning: Could not fetch issue title: {e}")
        return None

def parse_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse timestamp string to datetime object."""
    if not timestamp_str:
        return None
    
    try:
        parts = timestamp_str.split(", ")
        if len(parts) >= 2:
            date_part = parts[0]  # "January 9th"
            time_part = parts[1]  # "8:59am"
            
            # Remove ordinal suffix (st, nd, rd, th)
            date_part_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_part)
            
            try:
                dt = datetime.strptime(f"{date_part_clean}, {time_part}", "%B %d, %I:%M%p")
                current_year = datetime.now().year
                dt = dt.replace(year=current_year)
                now = datetime.now()
                
                if dt > now + timedelta(days=180):
                    dt = dt.replace(year=current_year - 1)
                elif dt < now - timedelta(days=180):
                    if now.month == 1 and dt.month == 12:
                        dt = dt.replace(year=current_year - 1)
                
                return dt
            except ValueError:
                pass
    except Exception:
        pass
    
    return None

def is_older_than_one_month(timestamp_str: str) -> bool:
    """Check if a timestamp string represents a date older than 1 month."""
    dt = parse_timestamp(timestamp_str)
    if dt is None:
        return False
    
    one_month_ago = datetime.now() - timedelta(days=30)
    return dt < one_month_ago

def is_older_than_three_months(timestamp_str: str) -> bool:
    """Check if a timestamp string represents a date older than 3 months."""
    dt = parse_timestamp(timestamp_str)
    if dt is None:
        return False
    
    three_months_ago = datetime.now() - timedelta(days=90)
    return dt < three_months_ago

# ============================================================================
# Maintenance Functions
# ============================================================================

def cleanup_old_runs(issue_dump: List[Dict[str, Any]], all_timestamps: Dict[str, str], centroid_to_issue: Dict[str, int], all_metadata: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Delete entire issues if their newest run is older than 3 months.
    No longer removes individual runs from issues.
    
    Returns:
        Tuple of (updated_issue_dump, issues_updated_count, issues_closed_count)
    """
    print(f"\n{'='*80}")
    print("Cleaning up old issues (newest run older than 3 months)...")
    print(f"{'='*80}")
    
    issues_closed = 0
    entries_to_remove = []
    
    for idx, entry in enumerate(issue_dump):
        failing_runs = entry.get("failing_runs", [])
        centroid_error = entry.get("centroid_error", "")
        
        if not failing_runs:
            continue
        
        # Find the newest run timestamp
        newest_timestamp = None
        newest_timestamp_str = ""
        
        for url in failing_runs:
            timestamp_str = all_timestamps.get(url, "")
            if not timestamp_str:
                continue
            
            dt = parse_timestamp(timestamp_str)
            if dt and (newest_timestamp is None or dt > newest_timestamp):
                newest_timestamp = dt
                newest_timestamp_str = timestamp_str
        
        # If no timestamps found, skip this entry
        if newest_timestamp is None:
            continue
        
        # Check if newest run is older than 3 months
        if is_older_than_three_months(newest_timestamp_str):
            print(f"\n  Issue entry {idx + 1}: Newest run is older than 3 months, will close issue")
            entries_to_remove.append(idx)
            issues_closed += 1
            
            # Close the issue on GitHub
            issue_number = None
            for centroid, issue_num in centroid_to_issue.items():
                if centroid.strip() == centroid_error.strip():
                    issue_number = issue_num
                    break
            
            if issue_number:
                try:
                    print(f"    Closing issue #{issue_number}...")
                    close_issue(issue_number)
                    print(f"    ✓ Closed issue #{issue_number}")
                    if centroid_error in centroid_to_issue:
                        del centroid_to_issue[centroid_error]
                    time.sleep(0.5)  # Rate limiting
                except Exception as e:
                    print(f"    ✗ Error closing issue #{issue_number}: {e}")
    
    # Remove entries that were closed (newest run older than 3 months)
    for idx in reversed(entries_to_remove):
        issue_dump.pop(idx)
    
    print(f"\n  Summary:")
    print(f"    Issues closed (newest run older than 3 months): {issues_closed}")
    print(f"    Remaining entries: {len(issue_dump)}")
    
    return issue_dump, 0, issues_closed  # No issues updated, only closed

def ensure_all_issues_sorted(issue_dump: List[Dict[str, Any]], all_timestamps: Dict[str, str], centroid_to_issue: Dict[str, int], all_metadata: Optional[Dict[str, Dict[str, Any]]] = None, open_issues_only: bool = True) -> int:
    """
    Ensure all issues have their runs sorted chronologically.
    Updates GitHub issues even if nothing else changed.
    
    Args:
        issue_dump: List of issue entries
        all_timestamps: Dictionary mapping URLs to timestamps
        centroid_to_issue: Dictionary mapping centroids to issue numbers
        all_metadata: Optional dictionary mapping URLs to metadata
        open_issues_only: If True, only update open issues. If False, update all issues.
    
    Returns:
        Number of issues updated
    """
    # Rebuild metadata for any missing entries
    if all_metadata:
        for entry in issue_dump:
            run_metadata = entry.get("run_metadata", {})
            failing_runs = entry.get("failing_runs", [])
            
            for url in failing_runs:
                if url not in run_metadata or (not run_metadata[url].get("job_name") and not run_metadata[url].get("workflow_name")):
                    if url in all_metadata:
                        run_metadata[url] = all_metadata[url]
            
            entry["run_metadata"] = run_metadata
    
    print(f"\n{'='*80}")
    print("Ensuring all issues have runs sorted chronologically...")
    if open_issues_only:
        print("(Only updating open issues)")
    print(f"{'='*80}")
    
    # Get open issues
    if open_issues_only:
        print("Fetching open issues...")
        open_issues = get_all_issues(open_only=True)
        print(f"Found {len(open_issues)} open issue(s)")
    else:
        open_issues = get_all_issues(open_only=False)
    
    # Build mapping from centroid to issue_dump entry for fast lookup
    centroid_to_entry = {}
    for idx, entry in enumerate(issue_dump):
        centroid_error = entry.get("centroid_error", "")
        if centroid_error:
            centroid_to_entry[centroid_error.strip().lower()] = (idx, entry)
    
    issues_updated = 0
    skipped_no_match = 0
    matched_entries = set()
    
    # Iterate through open issues and match them to issue_dump entries
    for issue in open_issues:
        issue_number = issue["number"]
        issue_body = issue.get("body", "")
        centroid_from_issue = extract_centroid_from_issue_body(issue_body)
        
        if not centroid_from_issue:
            continue
        
        # Find matching entry in issue_dump
        entry = None
        entry_idx = None
        
        centroid_key = centroid_from_issue.strip().lower()
        if centroid_key in centroid_to_entry:
            entry_idx, entry = centroid_to_entry[centroid_key]
            matched_entries.add(entry_idx)
        else:
            # Try fuzzy match with existing centroids
            for centroid, issue_num in centroid_to_issue.items():
                if centroid.strip().lower() == centroid_key:
                    for idx, e in enumerate(issue_dump):
                        if e.get("centroid_error", "").strip().lower() == centroid_key:
                            entry_idx, entry = idx, e
                            matched_entries.add(entry_idx)
                            break
                    break
        
        if not entry:
            skipped_no_match += 1
            if issue_number == 274 or skipped_no_match <= 10:
                print(f"  ⚠ Skipping issue #{issue_number}: No matching entry found for centroid (first 100 chars): {centroid_from_issue[:100] if len(centroid_from_issue) > 100 else centroid_from_issue}...")
            continue
        
        failing_runs = entry.get("failing_runs", [])
        run_metadata = entry.get("run_metadata", {})
        centroid_error = entry.get("centroid_error", "")
        
        if not failing_runs:
            continue
        
        try:
            count = len(failing_runs)
            existing_title = get_issue_title(issue_number)
            group_num = None
            if existing_title:
                group_num = extract_group_num_from_title(existing_title)
            
            # Format body - this will sort the runs chronologically
            body = format_issue_body(centroid_error, failing_runs, all_timestamps, run_metadata)
            title = create_title_from_count(count, centroid_error, group_num)
            
            # Always update to ensure sorting is correct
            print(f"  Updating issue #{issue_number} to ensure proper sorting...")
            update_issue(issue_number, title, body)
            print(f"  ✓ Updated issue #{issue_number}")
            
            if PROJECT_FIELD_ID:
                project_item_id = get_project_item_id_for_issue(issue_number)
                if project_item_id:
                    update_project_field(project_item_id, count)
            
            issues_updated += 1
            time.sleep(0.5)  # Rate limiting
        except Exception as e:
            print(f"  ✗ Error updating issue #{issue_number}: {e}")
            if issue_number == 274:
                import traceback
                print(f"  Full traceback for issue #274:")
                traceback.print_exc()
    
    unmatched_entries = len(issue_dump) - len(matched_entries)
    
    print(f"\n  Summary:")
    print(f"    Issues updated for sorting: {issues_updated}")
    if skipped_no_match > 0:
        print(f"    Open issues skipped (no matching entry): {skipped_no_match}")
    if unmatched_entries > 0:
        print(f"    Issue dump entries not matched to open issues: {unmatched_entries}")
    
    return issues_updated

def update_all_issues_with_metadata(issue_dump: List[Dict[str, Any]], all_timestamps: Dict[str, str], centroid_to_issue: Dict[str, int], open_issues_only: bool = True) -> int:
    """
    Update all GitHub issues with rebuilt metadata from issue_dump.
    This ensures that metadata restored from all_errors.json is reflected in GitHub issues.
    
    Args:
        issue_dump: List of issue entries (with rebuilt metadata)
        all_timestamps: Dictionary mapping URLs to timestamps
        centroid_to_issue: Dictionary mapping centroids to issue numbers
        open_issues_only: If True, only update open issues. If False, update all issues.
    
    Returns:
        Number of issues updated
    """
    print(f"\n{'='*80}")
    print("Updating GitHub issues with rebuilt metadata...")
    if open_issues_only:
        print("(Only updating open issues)")
    print(f"{'='*80}")
    
    # Get open issues
    if open_issues_only:
        print("Fetching open issues...")
        open_issues = get_all_issues(open_only=True)
        print(f"Found {len(open_issues)} open issue(s)")
    else:
        open_issues = get_all_issues(open_only=False)
    
    # Build mapping from centroid to issue_dump entry
    centroid_to_entry = {}
    for idx, entry in enumerate(issue_dump):
        centroid_error = entry.get("centroid_error", "")
        if centroid_error:
            centroid_to_entry[centroid_error.strip().lower()] = (idx, entry)
    
    issues_updated = 0
    skipped_no_match = 0
    
    # Iterate through open issues and update them
    for issue in open_issues:
        issue_number = issue["number"]
        issue_body = issue.get("body", "")
        centroid_from_issue = extract_centroid_from_issue_body(issue_body)
        
        if not centroid_from_issue:
            continue
        
        # Find matching entry in issue_dump
        entry = None
        centroid_key = centroid_from_issue.strip().lower()
        if centroid_key in centroid_to_entry:
            _, entry = centroid_to_entry[centroid_key]
        else:
            skipped_no_match += 1
            if skipped_no_match <= 10:
                print(f"  ⚠ Skipping issue #{issue_number}: No matching entry found for centroid")
            continue
        
        failing_runs = entry.get("failing_runs", [])
        run_metadata = entry.get("run_metadata", {})
        centroid_error = entry.get("centroid_error", "")
        
        if not failing_runs:
            continue
        
        try:
            count = len(failing_runs)
            existing_title = get_issue_title(issue_number)
            group_num = None
            if existing_title:
                group_num = extract_group_num_from_title(existing_title)
            
            # Format body with rebuilt metadata
            body = format_issue_body(centroid_error, failing_runs, all_timestamps, run_metadata)
            title = create_title_from_count(count, centroid_error, group_num)
            
            print(f"  Updating issue #{issue_number} with rebuilt metadata...")
            update_issue(issue_number, title, body)
            print(f"  ✓ Updated issue #{issue_number}")
            
            if PROJECT_FIELD_ID:
                project_item_id = get_project_item_id_for_issue(issue_number)
                if project_item_id:
                    update_project_field(project_item_id, count)
            
            issues_updated += 1
            time.sleep(0.5)  # Rate limiting
        except Exception as e:
            print(f"  ✗ Error updating issue #{issue_number}: {e}")
    
    print(f"\n  Summary:")
    print(f"    Issues updated: {issues_updated}")
    if skipped_no_match > 0:
        print(f"    Issues skipped (no matching entry): {skipped_no_match}")
    
    return issues_updated

# ============================================================================
# Main Function
# ============================================================================

def main():
    """Main function."""
    print("="*80)
    print("Maintaining GitHub issues (sorting and cleanup)")
    print("="*80)
    
    # Load issue dump
    print(f"\nLoading issue dump from {ISSUE_DUMP_FILE}...")
    try:
        with open(ISSUE_DUMP_FILE, 'r', encoding='utf-8') as f:
            issue_dump = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File not found: {ISSUE_DUMP_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {ISSUE_DUMP_FILE}: {e}")
        sys.exit(1)
    
    print(f"Found {len(issue_dump)} issue(s)")
    
    # Load all errors for metadata and timestamps
    print(f"\nLoading errors from {ALL_ERRORS_FILE}...")
    try:
        with open(ALL_ERRORS_FILE, 'r', encoding='utf-8') as f:
            all_errors = json.load(f)
    except FileNotFoundError:
        print(f"WARNING: File not found: {ALL_ERRORS_FILE}")
        all_errors = []
    except json.JSONDecodeError as e:
        print(f"WARNING: Invalid JSON in {ALL_ERRORS_FILE}: {e}")
        all_errors = []
    
    # Build timestamp map and metadata map from all_errors
    all_timestamps = {}
    all_metadata = {}
    for error_entry in all_errors:
        if len(error_entry) > 1 and error_entry[1]:
            url = error_entry[1]
            if len(error_entry) > 2:
                all_timestamps[url] = error_entry[2]
            job_name = error_entry[3] if len(error_entry) > 3 and error_entry[3] else ""
            workflow_name = error_entry[4] if len(error_entry) > 4 and error_entry[4] else ""
            is_nd = error_entry[5] if len(error_entry) > 5 and error_entry[5] is not None else False
            all_metadata[url] = {
                "job_name": job_name,
                "workflow_name": workflow_name,
                "is_nd": is_nd
            }
    
    # Get all GitHub issues and map to centroids
    print(f"\nFetching GitHub issues to map centroids...")
    issues = get_all_issues(open_only=False)
    print(f"Found {len(issues)} issue(s) in repository")
    
    # Build centroid_to_issue mapping
    centroid_to_issue = {}
    for issue in issues:
        issue_body = issue.get("body", "")
        centroid_from_issue = extract_centroid_from_issue_body(issue_body)
        if centroid_from_issue:
            for entry in issue_dump:
                centroid_error = entry.get("centroid_error", "")
                if centroid_error and centroid_error.strip().lower() == centroid_from_issue.strip().lower():
                    centroid_to_issue[centroid_error] = issue["number"]
                    break
    
    print(f"Mapped {len(centroid_to_issue)} centroid(s) to issue numbers")
    
    # Rebuild run_metadata from all_errors.json for any missing entries
    print(f"\nRebuilding metadata from all_errors.json for missing entries...")
    metadata_rebuilt = 0
    for entry in issue_dump:
        run_metadata = entry.get("run_metadata", {})
        failing_runs = entry.get("failing_runs", [])
        
        for url in failing_runs:
            if url not in run_metadata or (not run_metadata[url].get("job_name") and not run_metadata[url].get("workflow_name")):
                if url in all_metadata:
                    run_metadata[url] = all_metadata[url]
                    metadata_rebuilt += 1
        
        entry["run_metadata"] = run_metadata
    
    if metadata_rebuilt > 0:
        print(f"  ✓ Rebuilt metadata for {metadata_rebuilt} run(s)")
        metadata_updates = update_all_issues_with_metadata(issue_dump, all_timestamps, centroid_to_issue, open_issues_only=True)
        print(f"  ✓ Updated {metadata_updates} GitHub issue(s) with rebuilt metadata")
    else:
        print(f"  ✓ All runs already have metadata")
    
    # Clean up old runs (older than 1 month)
    issue_dump, cleanup_updated, cleanup_closed = cleanup_old_runs(issue_dump, all_timestamps, centroid_to_issue, all_metadata)
    
    # Ensure all issues have runs sorted chronologically
    sorting_updated = ensure_all_issues_sorted(issue_dump, all_timestamps, centroid_to_issue, all_metadata, open_issues_only=True)
    
    # Save updated issue dump
    if cleanup_updated > 0 or cleanup_closed > 0 or metadata_rebuilt > 0:
        print(f"\n{'='*80}")
        print("Saving updated issue dump...")
        print(f"{'='*80}")
        
        with open(ISSUE_DUMP_FILE, 'w', encoding='utf-8') as f:
            json.dump(issue_dump, f, indent=2, ensure_ascii=False)
        
        print(f"✓ Saved {len(issue_dump)} issue(s) to {ISSUE_DUMP_FILE}")
    
    # Summary
    print(f"\n{'='*80}")
    print("Summary:")
    print(f"  Issues updated with rebuilt metadata: {metadata_rebuilt}")
    print(f"  Issues closed (newest run older than 3 months): {cleanup_closed}")
    print(f"  Issues updated for sorting: {sorting_updated}")
    print(f"  Total issues in dump: {len(issue_dump)}")
    print(f"{'='*80}")

if __name__ == "__main__":
    main()
