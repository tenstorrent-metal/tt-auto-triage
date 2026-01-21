#!/usr/bin/env python3
"""
Sync new errors from all_errors.json to existing GitHub issues or create new ones.
Compares new errors to existing centroids and either adds them to existing issues
or creates new issues if no match is found.

Assumes issues are already sorted and formatted properly.
"""

import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

# ============================================================================
# File paths
# ============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, "secrets.json")
ALL_ERRORS_FILE = os.path.join(SCRIPT_DIR, "all_errors.json")
ISSUE_DUMP_FILE = os.path.join(SCRIPT_DIR, "issue_dump.json")

# Date range filtering (from environment variables)
DATE_RANGE_START = os.environ.get("DATE_RANGE_START", "")
DATE_RANGE_END = os.environ.get("DATE_RANGE_END", "")


def parse_date_to_datetime(date_str: str) -> Optional[datetime]:
    """Convert date string like 'January 1, 2026' to datetime."""
    if not date_str or not date_str.strip():
        return None
    try:
        return datetime.strptime(date_str.strip(), "%B %d, %Y")
    except ValueError:
        return None

# Import error similarity helper
from error_similarity import find_best_matching_centroid, compare_errors
from github_api_utils import log_rate_limit_status, get_commit_hash_from_github

# Similarity thresholds
# These must be high enough to prevent matching different errors that share boilerplate
# e.g., "TT_THROW @ path1: Device init failed" vs "TT_THROW @ path2: Device timeout"
# should NOT match even though they share "TT_THROW", "device", etc.
RAPIDFUZZ_THRESHOLD = 60.0  # Raised from 50 - requires more token overlap
SEMANTIC_THRESHOLD = 85.0   # Raised from 70 - requires stronger semantic similarity

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
    """Get all issues from the repository and map them to centroids.
    
    Args:
        open_only: If True, only fetch open issues. If False, fetch all issues.
    """
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

def extract_urls_from_issue_body(issue_body: str) -> List[str]:
    """Extract all GitHub Actions run URLs from an issue body.
    
    This extracts URLs from markdown links in the "All Occurrences" section.
    """
    urls = set()
    
    # Extract URLs from markdown links: [text](url)
    link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
    matches = re.findall(link_pattern, issue_body)
    
    for text, url in matches:
        # Only include GitHub Actions run URLs
        if "github.com" in url and ("actions/runs" in url or "/job/" in url):
            urls.add(url)
    
    # Also extract plain URLs
    url_pattern = r"https://github\.com/[^/]+/[^/]+/actions/runs/\d+/job/\d+"
    plain_urls = re.findall(url_pattern, issue_body)
    urls.update(plain_urls)
    
    return list(urls)

def normalize_centroid(centroid: str) -> str:
    """Normalize a centroid string for comparison (whitespace, case)."""
    if not centroid:
        return ""
    # Normalize whitespace: collapse multiple spaces/newlines to single space
    normalized = " ".join(centroid.split())
    return normalized.lower()

def map_issues_to_centroids(issues: List[Dict[str, Any]], issue_dump: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Map centroid errors to issue numbers by comparing issue bodies to centroids.
    Only maps OPEN issues - closed issues are ignored entirely.
    
    Uses normalized comparison to handle whitespace/formatting differences.
    
    Returns:
        Dictionary mapping centroid_error string to issue_number (only for open issues)
    """
    centroid_to_issue = {}
    issues_without_centroids = 0
    centroids_not_found = 0
    closed_issues_skipped = 0
    
    # Build normalized lookup for issue_dump centroids
    normalized_to_original = {}
    for entry in issue_dump:
        centroid_error = entry.get("centroid_error", "")
        if centroid_error:
            normalized = normalize_centroid(centroid_error)
            normalized_to_original[normalized] = centroid_error
    
    for issue in issues:
        # Skip closed issues entirely - they don't exist for our purposes
        issue_state = issue.get("state", "open")
        if issue_state == "closed":
            closed_issues_skipped += 1
            continue
        
        issue_body = issue.get("body", "")
        centroid_from_issue = extract_centroid_from_issue_body(issue_body)
        
        if not centroid_from_issue:
            issues_without_centroids += 1
            continue
        
        # Find matching centroid using normalized comparison
        normalized_from_issue = normalize_centroid(centroid_from_issue)
        
        if normalized_from_issue in normalized_to_original:
            # Found match - use the original centroid from issue_dump as key
            original_centroid = normalized_to_original[normalized_from_issue]
            centroid_to_issue[original_centroid] = issue["number"]
        else:
            # If not in issue_dump, still map using the centroid from the issue body
            # This handles cases where issue_dump might be stale
            centroid_to_issue[centroid_from_issue.strip()] = issue["number"]
            centroids_not_found += 1
    
    if closed_issues_skipped > 0:
        print(f"  Note: Skipped {closed_issues_skipped} closed issue(s) - they are ignored")
    if issues_without_centroids > 0:
        print(f"  Note: {issues_without_centroids} issue(s) had no extractable centroid")
    if centroids_not_found > 0:
        print(f"  Note: {centroids_not_found} issue(s) had centroids not in issue_dump (added directly)")
    
    return centroid_to_issue

def update_issue(issue_number: int, title: str, body: str) -> Dict[str, Any]:
    """Update an existing GitHub issue."""
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

def verify_repository_access() -> bool:
    """Verify that the repository exists and is accessible."""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
    
    auth_methods = [
        ("Bearer", f"Bearer {GITHUB_TOKEN}"),
        ("token", f"token {GITHUB_TOKEN}")
    ]
    
    for method_name, auth_header in auth_methods:
        headers = {
            "Authorization": auth_header,
            "Accept": "application/vnd.github.v3+json"
        }
        
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                repo_data = response.json()
                has_issues = repo_data.get("has_issues", True)
                if not has_issues:
                    print(f"\n⚠ Warning: Issues are disabled for repository {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
                    print("  Enable issues in repository settings to create issues.")
                    return False
                return True
            elif response.status_code == 404:
                print(f"\n✗ ERROR: Repository {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME} not found (404)")
                print("  Please verify:")
                print(f"  1. The repository exists at https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
                print("  2. The repository name is spelled correctly")
                print("  3. The GitHub token has access to this repository")
                return False
            elif response.status_code == 403:
                # Try next auth method
                continue
            else:
                print(f"\n✗ ERROR: Failed to verify repository access (HTTP {response.status_code})")
                print(f"  Response: {response.text}")
                return False
        except Exception as e:
            if method_name == "token":  # Last method
                print(f"\n✗ ERROR: Failed to verify repository: {e}")
                return False
            continue
    
    print(f"\n✗ ERROR: Failed to verify repository access with any authentication method")
    return False

def create_issue(title: str, body: str) -> Dict[str, Any]:
    """Create a new GitHub issue."""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues"
    
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
            response = requests.post(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            last_error = e
            if e.response.status_code == 403:
                # Try next auth method
                continue
            elif e.response.status_code == 404:
                print(f"\n✗ ERROR: HTTP 404 - Repository or endpoint not found")
                print(f"  URL: {url}")
                print(f"  Repository: {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
                print(f"  Response: {e.response.text}")
                print("\n  Possible causes:")
                print("  1. Repository does not exist")
                print("  2. Issues are disabled for this repository")
                print("  3. GitHub token does not have access to this repository")
                print(f"  4. Repository URL is incorrect (check: https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME})")
                raise
            else:
                print(f"\nERROR: HTTP {e.response.status_code}")
                print(f"Response: {e.response.text}")
                raise
        except Exception as e:
            last_error = e
            continue
    
    if last_error:
        raise last_error
    raise Exception("Failed to create issue")

def get_project_node_id() -> Optional[str]:
    """Get the GraphQL node ID for the project."""
    if not PROJECT_OWNER or not PROJECT_NUMBER:
        return None
    
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
    except Exception:
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
    except Exception:
        pass
    
    return None

def get_issue_node_id(issue_number: int) -> str:
    """Get the GraphQL node ID for an issue."""
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          id
        }
      }
    }
    """
    
    variables = {
        "owner": GITHUB_REPO_OWNER,
        "repo": GITHUB_REPO_NAME,
        "number": issue_number
    }
    
    response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
    response.raise_for_status()
    
    result = response.json()
    if "errors" in result:
        raise Exception(f"GraphQL errors: {result['errors']}")
    
    return result["data"]["repository"]["issue"]["id"]

def add_issue_to_project(issue_number: int) -> Optional[str]:
    """Add an issue to the project and return the project item ID."""
    project_node_id = get_project_node_id()
    if not project_node_id:
        return None
    
    issue_node_id = get_issue_node_id(issue_number)
    
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    query = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item {
          id
        }
      }
    }
    """
    
    variables = {
        "projectId": project_node_id,
        "contentId": issue_node_id
    }
    
    try:
        response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        if "errors" in result:
            return None
        
        return result["data"]["addProjectV2ItemById"]["item"]["id"]
    except Exception:
        return None

def get_project_item_id_for_issue(issue_number: int) -> Optional[str]:
    """Get the project item ID for an existing issue."""
    project_node_id = get_project_node_id()
    if not project_node_id:
        return None
    
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
    except Exception:
        return False

# ============================================================================
# Processing Functions
# ============================================================================

def format_issue_body(centroid_error: str, failing_runs: List[str], timestamps: Dict[str, str], run_metadata: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Format the issue body with centroid error and all URLs.
    
    Args:
        centroid_error: The centroid error message
        failing_runs: List of failing run URLs
        timestamps: Dictionary mapping URLs to timestamps
        run_metadata: Optional dictionary mapping URLs to dicts with 'job_name', 'workflow_name', 'is_nd', 'commit_hash', and 'error_message'
    """
    body_parts = []
    
    count = len(failing_runs)
    
    # Add number of occurrences at the top
    body_parts.append(f"**Number of Occurrences:** {count}")
    body_parts.append("")
    
    # Add centroid error as the main description (no link, just error message)
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
    centroid_url = failing_runs[0] if failing_runs else None
    
    for url in failing_runs:
        timestamp = timestamps.get(url, "")
        
        # Check if this is the centroid
        is_centroid = (url == centroid_url)
        
        # Build label - add [CENTROID] prefix if this is the centroid
        label = timestamp if timestamp else "Link"
        if is_centroid:
            label = f"[CENTROID] {label}"
        
        # Get job/workflow info, ND flag, commit hash, and error message if available
        job_workflow_suffix = ""
        commit_hash_suffix = ""
        error_message = ""
        is_nd = False
        if run_metadata and url in run_metadata:
            meta = run_metadata[url]
            job_name = meta.get("job_name", "")
            workflow_name = meta.get("workflow_name", "")
            is_nd = meta.get("is_nd", False)
            commit_hash = meta.get("commit_hash", "")
            error_message = meta.get("error_message", "")
            
            if job_name or workflow_name:
                parts = []
                if workflow_name:
                    parts.append(workflow_name)
                if job_name:
                    parts.append(job_name)
                job_workflow_suffix = f" - {' / '.join(parts)}"
            
            # Add commit hash if available
            if commit_hash:
                commit_hash_suffix = f" (commit: {commit_hash})"
        
        # Add ND marker if applicable
        if is_nd:
            label += " (marked as ND)"
        
        # Parse timestamp for proper chronological sorting
        dt = parse_timestamp(timestamp) if timestamp else None
        # Use datetime for sorting (None for items without timestamps, which go to end)
        url_list.append((dt, label, url, job_workflow_suffix, commit_hash_suffix, error_message))
    
    # Sort chronologically (newest first), items without timestamps go to the end
    url_list.sort(key=lambda x: (x[0] is not None, x[0] if x[0] is not None else datetime.max), reverse=True)
    
    # Format the list
    for idx, (dt, label, url, job_workflow_suffix, commit_hash_suffix, error_message) in enumerate(url_list, 1):
        body_parts.append(f"{idx}. [{label}]({url}){job_workflow_suffix}{commit_hash_suffix}")
        # Add error message as sub-bullet in a code block if available
        if error_message:
            # Use 4 spaces for proper markdown list continuation
            body_parts.append("")  # Empty line before code block
            body_parts.append("    ```")
            # Indent each line of the error message to stay within the list context
            for line in error_message.split("\n"):
                body_parts.append(f"    {line}")
            body_parts.append("    ```")
    
    return "\n".join(body_parts)

def create_title_from_count(count: int, error_message: str = "", group_num: Optional[int] = None) -> str:
    """Create a title with occurrence count prefix and truncated error message.
    
    Format: [00045] Group X: Error message... (if group_num provided) or [00045] Error message...
    Truncates error message to fit within GitHub's 256 character limit.
    """
    count_str = f"{count:05d}"
    
    # Calculate available space for error message
    prefix_len = len(f"[{count_str}] ")
    if group_num is not None:
        prefix_len += len(f"Group {group_num}: ")
    max_error_len = 256 - prefix_len - 3  # 3 for "..."
    
    # Truncate error message if needed
    if error_message and len(error_message) > max_error_len:
        # Try to truncate at a word boundary
        truncated = error_message[:max_error_len].rsplit(' ', 1)[0]
        if len(truncated) < max_error_len - 20:  # If truncation removed too much, just cut at max
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
    """Extract group number from an existing issue title.
    
    Examples:
        "[00045] Group 1" -> 1
        "[00045] Error" -> None
        "[00045] Group 42" -> 42
    """
    pattern = r"\[.*?\]\s*Group\s+(\d+)"
    match = re.search(pattern, title)
    if match:
        return int(match.group(1))
    return None

def get_issue_title(issue_number: int) -> Optional[str]:
    """Get the title of an existing issue."""
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

def get_issue_state(issue_number: int) -> Optional[str]:
    """Get the state (open/closed) of an issue by its number."""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues/{issue_number}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        issue = response.json()
        return issue.get("state")  # Returns "open" or "closed"
    except Exception as e:
        print(f"  ⚠ Warning: Could not fetch issue #{issue_number} state: {e}")
        return None

def parse_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse timestamp string to datetime object.
    
    Handles formats like "January 9th, 8:59am, 58.95 seconds"
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
            
            # Parse date and time
            try:
                dt = datetime.strptime(f"{date_part_clean}, {time_part}", "%B %d, %I:%M%p")
                # Determine the correct year
                current_year = datetime.now().year
                dt = dt.replace(year=current_year)
                now = datetime.now()
                
                # If the date is more than 6 months in the future, assume it's from last year
                if dt > now + timedelta(days=180):
                    dt = dt.replace(year=current_year - 1)
                elif dt < now - timedelta(days=180):
                    # Only adjust if we're in January and the date is December (likely from previous year)
                    if now.month == 1 and dt.month == 12:
                        dt = dt.replace(year=current_year - 1)
                
                return dt
            except ValueError:
                pass
    except Exception:
        pass
    
    return None

def get_issue_number_for_centroid(centroid: str, centroid_to_issue: Dict[str, int]) -> Optional[int]:
    """Get issue number for a centroid using normalized comparison."""
    # Try exact match first
    if centroid in centroid_to_issue:
        return centroid_to_issue[centroid]
    
    # Try normalized match
    normalized = normalize_centroid(centroid)
    for key, issue_num in centroid_to_issue.items():
        if normalize_centroid(key) == normalized:
            return issue_num
    
    return None

def process_new_error(error_entry: List, issue_dump: List[Dict[str, Any]], centroid_to_issue: Dict[str, int], all_timestamps: Dict[str, str]) -> Tuple[bool, List[Dict[str, Any]], bool]:
    """
    Process a new error entry and either add it to an existing issue or create a new one.
    
    Args:
        error_entry: [error_message, url, timestamp, job_name, workflow_name, is_nd] from all_errors.json
        issue_dump: Current issue_dump.json data
        centroid_to_issue: Mapping of centroid_error to issue_number
        all_timestamps: Dictionary mapping URLs to timestamps
    
    Returns:
        Tuple of (updated, new_issue_dump, is_new_issue)
        updated: True if issue_dump was modified
        new_issue_dump: Updated issue_dump data
        is_new_issue: True if a new issue was created, False if existing issue was updated
    """
    error_message = error_entry[0]
    url = error_entry[1]
    timestamp = error_entry[2] if len(error_entry) > 2 else ""
    job_name = error_entry[3] if len(error_entry) > 3 and error_entry[3] else ""
    workflow_name = error_entry[4] if len(error_entry) > 4 and error_entry[4] else ""
    is_nd = error_entry[5] if len(error_entry) > 5 and error_entry[5] is not None else False
    
    # Find best matching centroid
    centroids = [entry["centroid_error"] for entry in issue_dump]
    best_idx, best_scores = find_best_matching_centroid(
        error_message, 
        centroids, 
        rapidfuzz_threshold=RAPIDFUZZ_THRESHOLD,
        semantic_threshold=SEMANTIC_THRESHOLD
    )
    
    if best_idx is not None:
        # Check if this centroid belongs to a closed issue
        entry = issue_dump[best_idx]
        old_centroid = entry["centroid_error"]
        issue_number = get_issue_number_for_centroid(old_centroid, centroid_to_issue)
        
        # If no issue number in mapping, or issue is closed, treat as no match
        if issue_number:
            issue_state = get_issue_state(issue_number)
            if issue_state == "closed":
                print(f"\n  Matched centroid belongs to closed issue #{issue_number}")
                print(f"  Closed issues are ignored - treating as no match, will create new issue")
                # Fall through to create new issue logic
                best_idx = None
            else:
                # Issue is open - proceed with update
                print(f"\n  Matching existing OPEN issue #{issue_number} (RapidFuzz: {best_scores['rapidfuzz']:.1f}, Semantic: {best_scores['semantic']:.1f})")
        else:
            # No issue number found - this centroid isn't mapped to an open issue
            print(f"\n  Matched centroid but no open issue found - will create new issue")
            best_idx = None
    
    if best_idx is not None:
        # Add to existing OPEN issue
        entry = issue_dump[best_idx]
        failing_runs = entry.get("failing_runs", [])
        run_metadata = entry.get("run_metadata", {})
        
        # Debug: log current state
        print(f"  [DEBUG] Entry has {len(failing_runs)} existing runs, {len(run_metadata)} metadata entries")
        
        # Check if URL already exists in this issue
        if url in failing_runs:
            print(f"  ⚠ URL already exists in this issue, skipping duplicate")
            return False, issue_dump, False
        
        # Add new URL
        failing_runs.append(url)
        if timestamp:
            all_timestamps[url] = timestamp
        
        # Fetch commit hash from GitHub API
        commit_hash = None
        github_token = load_secrets().get("GITHUB_TOKEN", "")
        if github_token:
            commit_hash = get_commit_hash_from_github(url, github_token)
        
        # Store job/workflow metadata, ND flag, commit hash, and error message
        run_metadata[url] = {
            "job_name": job_name,
            "workflow_name": workflow_name,
            "is_nd": is_nd,
            "commit_hash": commit_hash if commit_hash else "",
            "error_message": error_message
        }
        
        # Debug: verify error message was stored
        print(f"  [DEBUG] Stored error_message for {url}: {len(error_message)} chars")
        print(f"  [DEBUG] run_metadata now has {len(run_metadata)} entries")
        
        # Keep centroid unchanged - centroids are fixed once set
        centroid_error = entry["centroid_error"]
        
        # Update entry (centroid stays the same)
        entry["failing_runs"] = sorted(list(set(failing_runs)))  # Remove duplicates and sort
        entry["run_metadata"] = run_metadata
        
        # Get issue number using the unchanged centroid (with normalized lookup)
        issue_number = get_issue_number_for_centroid(centroid_error, centroid_to_issue)
        
        if issue_number:
            try:
                count = len(entry["failing_runs"])
                # Fetch existing title to preserve group number
                existing_title = get_issue_title(issue_number)
                group_num = None
                if existing_title:
                    group_num = extract_group_num_from_title(existing_title)
                title = create_title_from_count(count, entry["centroid_error"], group_num)
                run_metadata = entry.get("run_metadata", {})
                
                # Debug: verify all URLs have metadata with error_message
                print(f"  [DEBUG] Final state before update:")
                print(f"    failing_runs: {len(entry['failing_runs'])} URLs")
                print(f"    run_metadata: {len(run_metadata)} entries")
                urls_with_error = sum(1 for m in run_metadata.values() if m.get("error_message"))
                print(f"    URLs with error_message: {urls_with_error}")
                
                # Check which URLs are missing metadata
                missing_metadata = [u for u in entry["failing_runs"] if u not in run_metadata]
                if missing_metadata:
                    print(f"    ⚠ Missing metadata for {len(missing_metadata)} URL(s):")
                    for u in missing_metadata[:3]:  # Show first 3
                        print(f"      - {u}")
                
                body = format_issue_body(entry["centroid_error"], entry["failing_runs"], all_timestamps, run_metadata)
                
                print(f"  Updating issue #{issue_number}...")
                updated_issue = update_issue(issue_number, title, body)
                print(f"  ✓ Updated issue: {updated_issue['html_url']}")
                
                # Update project field if configured
                if PROJECT_FIELD_ID:
                    project_item_id = get_project_item_id_for_issue(issue_number)
                    if not project_item_id:
                        project_item_id = add_issue_to_project(issue_number)
                    if project_item_id:
                        update_project_field(project_item_id, count)
                
                time.sleep(0.5)  # Rate limiting
            except Exception as e:
                print(f"  ✗ Error updating issue: {e}")
                return False, issue_dump, False
            
            return True, issue_dump, False  # Updated existing issue
    
    # If we get here, either no match was found OR the matched issue was closed
    # In either case, create a new issue
    print(f"\n  No match found (or matched issue was closed) - creating new issue")
    
    # Create new entry
    new_entry = {
        "centroid_error": error_message,
        "failing_runs": [url],
        "run_metadata": {}
    }
    
    issue_dump.append(new_entry)
    if timestamp:
        all_timestamps[url] = timestamp
    
    # Fetch commit hash from GitHub API
    commit_hash = None
    github_token = load_secrets().get("GITHUB_TOKEN", "")
    if github_token:
        commit_hash = get_commit_hash_from_github(url, github_token)
    
    # Store job/workflow metadata, ND flag, commit hash, and error message
    run_metadata = {}
    run_metadata[url] = {
        "job_name": job_name,
        "workflow_name": workflow_name,
        "is_nd": is_nd,
        "commit_hash": commit_hash if commit_hash else "",
        "error_message": error_message
    }
    new_entry["run_metadata"] = run_metadata
    
    # Create GitHub issue
    try:
        count = 1
        title = create_title_from_count(count, error_message)
        body = format_issue_body(error_message, [url], all_timestamps, run_metadata)
        
        print(f"  Creating new issue...")
        issue = create_issue(title, body)
        issue_number = issue["number"]
        print(f"  ✓ Created issue #{issue_number}: {issue['html_url']}")
        
        # Add to project if configured
        if PROJECT_OWNER and PROJECT_NUMBER:
            project_item_id = add_issue_to_project(issue_number)
            if project_item_id and PROJECT_FIELD_ID:
                update_project_field(project_item_id, count)
        
        # Update mapping for future use
        centroid_to_issue[error_message] = issue_number
        
        time.sleep(0.5)  # Rate limiting
    except Exception as e:
        print(f"  ✗ Error creating issue: {e}")
        # Remove the entry we just added
        issue_dump.pop()
        return False, issue_dump, False
    
    return True, issue_dump, True  # Created new issue

# ============================================================================
# Main Function
# ============================================================================

def main():
    """Main function."""
    print("="*80)
    print("Syncing new errors to GitHub issues")
    print("="*80)
    
    # Check GitHub API rate limit at start
    secrets = load_secrets()
    github_token = secrets.get("GITHUB_TOKEN", "")
    if github_token:
        log_rate_limit_status(github_token, "start")
    
    # Refresh issue dump from GitHub project to ensure it's up to date
    print(f"\n{'='*80}")
    print("Refreshing issue dump from GitHub project...")
    print(f"{'='*80}")
    try:
        # Import and run download_issue_dump to refresh issue_dump.json
        import download_issue_dump
        download_issue_dump.main()
        # Check if download_issue_dump actually ran (it returns early if project not configured)
        # The function will print a warning if project fields are missing
        print(f"\n✓ Issue dump refresh completed (may have been skipped if project not configured)")
    except SystemExit as e:
        # download_issue_dump may exit if critical errors occur
        print(f"\n⚠ Warning: Issue dump refresh exited: {e}")
        print("  Continuing with existing issue_dump.json if it exists...")
    except Exception as e:
        print(f"\n⚠ Warning: Failed to refresh issue dump: {e}")
        print("  Continuing with existing issue_dump.json if it exists...")
        import traceback
        traceback.print_exc()
    
    # Load all errors
    print(f"\nLoading errors from {ALL_ERRORS_FILE}...")
    try:
        with open(ALL_ERRORS_FILE, 'r', encoding='utf-8') as f:
            all_errors = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File not found: {ALL_ERRORS_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {ALL_ERRORS_FILE}: {e}")
        sys.exit(1)
    
    print(f"Found {len(all_errors)} error(s)")
    
    # Load issue dump (now refreshed)
    print(f"\nLoading issue dump from {ISSUE_DUMP_FILE}...")
    try:
        with open(ISSUE_DUMP_FILE, 'r', encoding='utf-8') as f:
            issue_dump = json.load(f)
    except FileNotFoundError:
        print(f"WARNING: File not found: {ISSUE_DUMP_FILE}")
        print("Creating new issue dump...")
        issue_dump = []
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {ISSUE_DUMP_FILE}: {e}")
        sys.exit(1)
    
    print(f"Found {len(issue_dump)} existing issue(s)")
    
    # Build URL to error_message mapping from all_errors for backfilling (used later)
    url_to_error_message = {}
    for error_entry in all_errors:
        if len(error_entry) >= 2 and error_entry[1]:
            url_to_error_message[error_entry[1]] = error_entry[0]  # url -> error_message
    
    # Verify repository access before proceeding
    print(f"\nVerifying repository access...")
    if not verify_repository_access():
        print("\n✗ Cannot proceed without repository access. Exiting.")
        sys.exit(1)
    print(f"✓ Repository {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME} is accessible")
    
    # Get all GitHub issues and map to centroids (only open issues are mapped)
    print(f"\nFetching GitHub issues to map centroids...")
    issues = get_all_issues(open_only=False)  # Fetch all to filter out closed ones
    print(f"Found {len(issues)} issue(s) in repository")
    
    centroid_to_issue = map_issues_to_centroids(issues, issue_dump)
    print(f"Mapped {len(centroid_to_issue)} centroid(s) to OPEN issue numbers (closed issues ignored)")
    
    # Filter issue_dump to only include entries from open issues
    # Closed issues are completely ignored - remove their centroids from issue_dump
    print(f"\nFiltering issue_dump to exclude closed issues...")
    original_count = len(issue_dump)
    original_url_count = sum(len(entry.get("failing_runs", [])) for entry in issue_dump)
    
    # Build normalized lookup for centroid_to_issue keys
    normalized_centroid_to_issue = {normalize_centroid(k): k for k in centroid_to_issue.keys()}
    
    def is_centroid_in_open_issues(centroid: str) -> bool:
        """Check if centroid (with normalized comparison) is in an open issue."""
        normalized = normalize_centroid(centroid)
        return normalized in normalized_centroid_to_issue
    
    issue_dump = [
        entry for entry in issue_dump
        if is_centroid_in_open_issues(entry.get("centroid_error", ""))
    ]
    filtered_count = original_count - len(issue_dump)
    filtered_url_count = original_url_count - sum(len(entry.get("failing_runs", [])) for entry in issue_dump)
    if filtered_count > 0:
        print(f"  Removed {filtered_count} entry/entries from closed issues ({filtered_url_count} URLs)")
    print(f"  {len(issue_dump)} open issue(s) remaining in issue_dump")
    
    # Backfill missing error_messages in issue_dump from all_errors
    # This must happen AFTER map_issues_to_centroids adds entries from repo issues
    # This fixes issues where run_metadata exists but has no error_message
    backfilled_count = 0
    for entry in issue_dump:
        run_metadata = entry.get("run_metadata", {})
        for url, meta in run_metadata.items():
            if not meta.get("error_message") and url in url_to_error_message:
                meta["error_message"] = url_to_error_message[url]
                backfilled_count += 1
    
    if backfilled_count > 0:
        print(f"  ✓ Backfilled {backfilled_count} missing error_message(s) from all_errors.json")
    
    # Build timestamp map from all_errors
    all_timestamps = {}
    for error_entry in all_errors:
        if len(error_entry) > 1 and error_entry[1]:
            url = error_entry[1]
            if len(error_entry) > 2:
                all_timestamps[url] = error_entry[2]
    
    # Build set of existing URLs from issues in the PROJECT BOARD only
    # This uses issue_dump (from project) not all repo issues
    # Issues in the repo but NOT in the project board are ignored
    print(f"\nBuilding set of existing URLs from PROJECT BOARD issues...")
    existing_urls = set()
    total_urls_in_project = 0
    
    for entry in issue_dump:
        failing_runs = entry.get("failing_runs", [])
        for url in failing_runs:
            if url:
                existing_urls.add(url)
                total_urls_in_project += 1
    
    print(f"  Issues in project board: {len(issue_dump)}")
    print(f"  Total URLs in project board issues: {total_urls_in_project}")
    print(f"  Unique URLs: {len(existing_urls)}")
    
    # Also report orphan issues (in repo but not in project)
    open_repo_issues = sum(1 for issue in issues if issue.get("state") != "closed")
    orphan_count = open_repo_issues - len(centroid_to_issue)
    if orphan_count > 0:
        print(f"  ⚠ Warning: {orphan_count} open issue(s) in repo are NOT in the project board")
        print(f"    Consider closing or adding them to the project")
    
    # Parse date range filters
    date_range_start = parse_date_to_datetime(DATE_RANGE_START)
    date_range_end = parse_date_to_datetime(DATE_RANGE_END)
    
    if date_range_start:
        print(f"\nDate range start: {DATE_RANGE_START}")
    if date_range_end:
        print(f"Date range end: {DATE_RANGE_END}")
    
    # Filter all_errors to only include entries with URLs not already processed
    print(f"\nFiltering errors to find new ones...")
    new_errors = []
    skipped_no_url = 0
    skipped_existing = 0
    skipped_date_range = 0
    
    for error_entry in all_errors:
        url = error_entry[1] if len(error_entry) > 1 else None
        timestamp_str = error_entry[2] if len(error_entry) > 2 else ""
        
        if not url:
            skipped_no_url += 1
            continue
        
        if url in existing_urls:
            skipped_existing += 1
            continue
        
        # Check date range filter using parsed timestamp
        if timestamp_str and (date_range_start or date_range_end):
            parsed_dt = parse_timestamp(timestamp_str)
            if parsed_dt:
                if date_range_start and parsed_dt < date_range_start:
                    skipped_date_range += 1
                    continue
                if date_range_end and parsed_dt > date_range_end:
                    skipped_date_range += 1
                    continue
        
        new_errors.append(error_entry)
    
    print(f"  Total errors in all_errors.json: {len(all_errors)}")
    print(f"  Skipped (no URL): {skipped_no_url}")
    print(f"  Skipped (URL already in open issues): {skipped_existing}")
    if skipped_date_range > 0:
        print(f"  Skipped (outside date range): {skipped_date_range}")
    print(f"  New errors to process: {len(new_errors)}")
    
    if skipped_existing == 0 and len(all_errors) > 0:
        print(f"  ⚠ Warning: No existing URLs found - issue_dump might be empty or out of sync")
    
    # Process new errors if any
    new_count = 0
    updated_count = 0
    
    if new_errors:
        # Process each new error
        print(f"\n{'='*80}")
        print("Processing new errors...")
        print(f"{'='*80}")
        
        for idx, error_entry in enumerate(new_errors, 1):
            error_message = error_entry[0]
            url = error_entry[1]
            
            print(f"\n[{idx}/{len(new_errors)}] Processing error...")
            print(f"  URL: {url}")
            
            # Process the error
            updated, issue_dump, is_new_issue = process_new_error(error_entry, issue_dump, centroid_to_issue, all_timestamps)
            
            if updated:
                if is_new_issue:
                    new_count += 1
                else:
                    updated_count += 1
                
                # Update existing_urls set to avoid reprocessing in same run
                existing_urls.add(url)
    else:
        print(f"\nNo new errors to process.")
    
    # Clean up old issues (newest run older than 3 months) - always run this automatically
    print(f"\n{'='*80}")
    print("Cleaning up old issues (newest run older than 3 months)...")
    print(f"{'='*80}")
    
    # Build all_metadata from issue_dump for cleanup function
    all_metadata = {}
    for entry in issue_dump:
        run_metadata = entry.get("run_metadata", {})
        for url, meta in run_metadata.items():
            all_metadata[url] = meta
    
    # Import and call cleanup function
    cleanup_updated = 0
    cleanup_closed = 0
    try:
        import maintain_issues
        issue_dump, cleanup_updated, cleanup_closed = maintain_issues.cleanup_old_runs(
            issue_dump, all_timestamps, centroid_to_issue, all_metadata
        )
        print(f"\n✓ Cleanup completed: {cleanup_closed} issue(s) closed (newest run older than 3 months)")
    except Exception as e:
        print(f"\n⚠ Warning: Failed to cleanup old runs: {e}")
        import traceback
        traceback.print_exc()
    
    # Save updated issue dump
    if new_count > 0 or updated_count > 0 or cleanup_closed > 0:
        print(f"\n{'='*80}")
        print("Saving updated issue dump...")
        print(f"{'='*80}")
        
        with open(ISSUE_DUMP_FILE, 'w', encoding='utf-8') as f:
            json.dump(issue_dump, f, indent=2, ensure_ascii=False)
        
        print(f"✓ Saved {len(issue_dump)} issue(s) to {ISSUE_DUMP_FILE}")
    
    # Summary
    print(f"\n{'='*80}")
    print("Summary:")
    print(f"  New issues created: {new_count}")
    print(f"  Existing issues updated: {updated_count}")
    print(f"  Old issues closed: {cleanup_closed} issue(s) closed (newest run older than 3 months)")
    print(f"  Errors skipped (no URL): {skipped_no_url}")
    print(f"  Errors skipped (already exists): {skipped_existing}")
    print(f"  Total issues in dump: {len(issue_dump)}")
    print(f"{'='*80}")

if __name__ == "__main__":
    main()
