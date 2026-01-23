#!/usr/bin/env python3
"""
Download all issues from a GitHub project.
Extracts centroid error and all failing run URLs for each issue.
Project name is specified in secrets.json.
"""

import json
import os
import re
import sys
import time
from typing import Dict, List, Any

import requests

# File paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, "secrets.json")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "issue_dump.json")

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
            "PROJECT_NAME": secrets.get("project_name", "")
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

# ============================================================================
# GitHub API Functions
# ============================================================================

def find_project_by_name(project_name: str) -> Dict[str, Any]:
    """Find a GitHub project by name and return its number and node ID."""
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Try organization first
    query_org = """
    query($owner: String!) {
      organization(login: $owner) {
        projectsV2(first: 100) {
          nodes {
            id
            number
            title
          }
        }
      }
    }
    """
    
    variables_org = {
        "owner": PROJECT_OWNER
    }
    
    org_projects = []
    try:
        response = requests.post(url, json={"query": query_org, "variables": variables_org}, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        if "errors" in result:
            # If organization query fails, try user query
            error_msg = result["errors"][0].get("message", "Unknown error")
            if "Could not resolve to an Organization" in error_msg:
                print(f"  '{PROJECT_OWNER}' is not an organization, trying as user account...")
            else:
                print(f"  Organization query error: {error_msg}")
        else:
            org_projects = result["data"]["organization"]["projectsV2"]["nodes"]
            print(f"  Found {len(org_projects)} project(s) in organization")
            for project in org_projects:
                if project["title"].lower() == project_name.lower():
                    print(f"Found project: {project['title']} (number: {project['number']})")
                    return {
                        "id": project["id"],
                        "number": project["number"],
                        "title": project["title"]
                    }
    except Exception as e:
        print(f"  Organization query exception: {e}")
    
    # Try user account
    query_user = """
    query($owner: String!) {
      user(login: $owner) {
        projectsV2(first: 100) {
          nodes {
            id
            number
            title
          }
        }
      }
    }
    """
    
    variables_user = {
        "owner": PROJECT_OWNER
    }
    
    user_projects = []
    try:
        response = requests.post(url, json={"query": query_user, "variables": variables_user}, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        if "errors" in result:
            error_msg = result["errors"][0].get("message", "Unknown error")
            print(f"  User query error: {error_msg}")
        elif result["data"]["user"]:
            user_projects = result["data"]["user"]["projectsV2"]["nodes"]
            print(f"  Found {len(user_projects)} project(s) in user account")
            for project in user_projects:
                if project["title"].lower() == project_name.lower():
                    print(f"Found project: {project['title']} (number: {project['number']})")
                    return {
                        "id": project["id"],
                        "number": project["number"],
                        "title": project["title"]
                    }
    except Exception as e:
        print(f"  User query exception: {e}")
    
    # Try repository-level projects if repo info is available
    repo_projects = []
    if GITHUB_REPO_OWNER and GITHUB_REPO_NAME:
        query_repo = """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            projectsV2(first: 100) {
              nodes {
                id
                number
                title
              }
            }
          }
        }
        """
        
        variables_repo = {
            "owner": GITHUB_REPO_OWNER,
            "repo": GITHUB_REPO_NAME
        }
        
        try:
            response = requests.post(url, json={"query": query_repo, "variables": variables_repo}, headers=headers)
            response.raise_for_status()
            result = response.json()
            
            if "errors" not in result and result["data"]["repository"]:
                repo_projects = result["data"]["repository"]["projectsV2"]["nodes"]
                print(f"  Found {len(repo_projects)} project(s) in repository {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
                for project in repo_projects:
                    if project["title"].lower() == project_name.lower():
                        print(f"Found project: {project['title']} (number: {project['number']})")
                        return {
                            "id": project["id"],
                            "number": project["number"],
                            "title": project["title"]
                        }
        except Exception as e:
            print(f"  Repository query exception: {e}")
    
    # If we get here, project wasn't found - show available projects
    all_projects = org_projects + user_projects + repo_projects
    if all_projects:
        print(f"\nAvailable projects:")
        for project in all_projects:
            print(f"  - {project['title']} (number: {project['number']})")
        print(f"\nNote: Looking for project with name '{project_name}' (case-insensitive)")
    else:
        print(f"\nNo projects found")
        print("Searched in:")
        print(f"  - Organization/user: {PROJECT_OWNER}")
        if GITHUB_REPO_OWNER and GITHUB_REPO_NAME:
            print(f"  - Repository: {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
        print("\nPossible issues:")
        print("  - Project name might be different")
        print("  - Project might be in a different location")
        print("  - Token might not have access to view projects")
        print("  - Try setting 'project_name' in secrets.json to match exactly")
    
    raise Exception(f"Project '{project_name}' not found")

def get_project_items(project_id: str) -> List[Dict[str, Any]]:
    """Get all items (issues) from a GitHub project."""
    url = "https://api.github.com/graphql"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    
    all_items = []
    cursor = None
    page_num = 1
    
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
                      id
                      number
                      title
                      body
                      url
                      state
                      __typename
                    }
                    ... on DraftIssue {
                      id
                      title
                      body
                      __typename
                    }
                  }
                }
              }
            }
          }
        }
        """
        
        variables = {
            "projectId": project_id,
            "cursor": cursor
        }
        
        try:
            response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
            response.raise_for_status()
            result = response.json()
            
            if "errors" in result:
                print(f"GraphQL errors: {result['errors']}")
                raise Exception(f"GraphQL errors: {result['errors']}")
            
            items_data = result["data"]["node"]["items"]
            items = items_data["nodes"]
            
            print(f"  Page {page_num}: Found {len(items)} item(s)")
            
            if len(items) == 0:
                print(f"    No items returned - this might indicate a permissions issue")
                break
            
            # Debug: show content types and structure
            content_types = {}
            null_content_count = 0
            for item in items:
                if item.get("content") is None:
                    null_content_count += 1
                elif item.get("content"):
                    typename = item["content"].get("__typename", "Unknown")
                    content_types[typename] = content_types.get(typename, 0) + 1
            
            if null_content_count > 0:
                print(f"    Items with null content: {null_content_count}")
            if content_types:
                print(f"    Content types: {content_types}")
            
            # Filter for issues only (exclude DraftIssue)
            issues = []
            for item in items:
                content = item.get("content")
                if content:
                    typename = content.get("__typename")
                    if typename == "Issue":
                        issues.append(content)
                    elif typename:
                        # Debug: show what other types we're skipping
                        if page_num == 1:  # Only show on first page to avoid spam
                            print(f"    Skipping content type: {typename}")
            
            print(f"    Issues found: {len(issues)}")
            all_items.extend(issues)
            
            # If we got items but no issues, show a sample item structure for debugging
            if len(items) > 0 and len(issues) == 0 and page_num == 1:
                print(f"    DEBUG: Sample item structure:")
                sample = items[0]
                print(f"      Keys: {list(sample.keys())}")
                if sample.get("content"):
                    print(f"      Content keys: {list(sample['content'].keys())}")
                else:
                    print(f"      Content is null or missing")
            
            page_info = items_data["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            
            cursor = page_info["endCursor"]
            page_num += 1
            time.sleep(0.5)  # Rate limiting
            
        except Exception as e:
            print(f"ERROR: Failed to get project items: {e}")
            raise
    
    return all_items

def extract_centroid_error(issue_body: str) -> str:
    """Extract the centroid error from the issue body."""
    # Look for the Error Message section with code block
    pattern = r"## Error Message\s*```\s*(.+?)\s*```"
    match = re.search(pattern, issue_body, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    
    # Fallback: try without the ## header
    pattern = r"```\s*(.+?)\s*```"
    match = re.search(pattern, issue_body, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    return ""

def extract_centroid_metadata(issue_body: str) -> Dict[str, Optional[str]]:
    """Extract centroid URL, commit hash, and timestamp from the issue body.
    
    Parses format from chronological list: "1. [[CENTROID] timestamp](url) - workflow / job (commit: abc1234...)"
    The centroid is marked with [CENTROID] prefix in the label.
    
    Returns:
        Dictionary with 'url', 'commit_hash', and 'timestamp' keys
    """
    # Pattern to match numbered list entry with [CENTROID] flag
    # Format: "1. [[CENTROID] timestamp](url) - workflow / job (commit: abc1234...)"
    line_pattern = r"^\d+\.\s+\[\[CENTROID\]\s+([^\]]+)\]\(([^)]+)\)(?:\s*-\s*[^(]+)?(?:\s*\(commit:\s*([a-fA-F0-9]+)\))?"
    
    # Split body into lines for processing
    lines = issue_body.split('\n')
    
    for line in lines:
        match = re.match(line_pattern, line)
        if match:
            timestamp, url, commit_hash = match.groups()
            return {
                "url": url.strip() if url else None,
                "commit_hash": commit_hash.strip() if commit_hash else None,
                "timestamp": timestamp.strip() if timestamp else None
            }
    
    return {
        "url": None,
        "commit_hash": None,
        "timestamp": None
    }

def extract_failing_runs(issue_body: str) -> List[str]:
    """Extract all failing run URLs from the issue body."""
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
    
    return sorted(list(urls))

def extract_run_metadata(issue_body: str) -> Dict[str, Dict[str, Any]]:
    """Extract run metadata (job_name, workflow_name, is_nd, commit_hash, error_message) from issue body.
    
    Parses the format: [label](url) - workflow / job (commit: abc1234...)
    or: [label (marked as ND)](url) - workflow / job (commit: abc1234...)
    
    Also extracts error_message from code blocks that follow each URL line:
        1. [label](url) - workflow / job
           ```
           Error message here
           ```
    
    Returns:
        Dictionary mapping URL to dict with 'job_name', 'workflow_name', 'is_nd', 'commit_hash', and 'error_message'
    """
    run_metadata = {}
    
    # Pattern to match lines like: "1. [label](url) - workflow / job (commit: abc1234...)"
    # Also handles [CENTROID] prefix: "1. [[CENTROID] label](url) - workflow / job (commit: abc1234...)"
    line_pattern = r"^\d+\.\s+\[([^\]]+)\]\(([^)]+)\)(?:\s*-\s*([^(]+))?(?:\s*\(commit:\s*([a-fA-F0-9]+)\))?"
    
    # Split body into lines for processing
    lines = issue_body.split('\n')
    
    current_url = None
    current_metadata = None
    in_code_block = False
    code_block_lines = []
    
    for i, line in enumerate(lines):
        # Check if this line matches a numbered URL entry
        match = re.match(line_pattern, line)
        if match:
            # Save any pending code block to previous URL
            if current_url and code_block_lines:
                current_metadata["error_message"] = '\n'.join(code_block_lines).strip()
                code_block_lines = []
            
            label, url, suffix, commit_hash = match.groups()
            
            # Only process GitHub Actions run URLs
            if not ("github.com" in url and ("actions/runs" in url or "/job/" in url)):
                current_url = None
                current_metadata = None
                continue
            
            # Check if marked as ND
            is_nd = "(marked as ND)" in label
            
            # Parse workflow/job from suffix
            workflow_name = ""
            job_name = ""
            if suffix:
                suffix = suffix.strip()
                if " / " in suffix:
                    parts = suffix.split(" / ", 1)
                    workflow_name = parts[0].strip() if parts[0].strip() else ""
                    job_name = parts[1].strip() if parts[1].strip() else ""
                else:
                    workflow_name = suffix.strip()
            
            commit_hash_str = commit_hash.strip() if commit_hash else ""
            
            current_url = url
            current_metadata = {
                "job_name": job_name,
                "workflow_name": workflow_name,
                "is_nd": is_nd,
                "commit_hash": commit_hash_str,
                "error_message": ""
            }
            run_metadata[url] = current_metadata
            in_code_block = False
            code_block_lines = []
            
        elif current_url:
            # Check for code block markers (indented with spaces)
            stripped = line.strip()
            if stripped == '```' or line.lstrip().startswith('```'):
                if in_code_block:
                    # End of code block
                    in_code_block = False
                    if code_block_lines:
                        current_metadata["error_message"] = '\n'.join(code_block_lines).strip()
                        code_block_lines = []
                else:
                    # Start of code block
                    in_code_block = True
                    code_block_lines = []
            elif in_code_block:
                # Inside code block - collect lines (remove leading indentation)
                # Lines are typically indented with 3 spaces
                code_block_lines.append(line.lstrip())
    
    # Handle any remaining code block
    if current_url and code_block_lines:
        current_metadata["error_message"] = '\n'.join(code_block_lines).strip()
    
    return run_metadata

def parse_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Parse an issue and extract centroid error, failing runs, and run metadata."""
    issue_body = issue.get("body", "")
    issue_number = issue.get("number", 0)
    issue_title = issue.get("title", "")
    issue_url = issue.get("url", "")
    issue_state = issue.get("state", "")
    
    centroid_error = extract_centroid_error(issue_body)
    failing_runs = extract_failing_runs(issue_body)
    run_metadata = extract_run_metadata(issue_body)
    centroid_metadata = extract_centroid_metadata(issue_body)
    
    # If centroid metadata was found, ensure it's stored in run_metadata for the centroid URL
    centroid_url = centroid_metadata.get("url")
    if centroid_url and centroid_url not in run_metadata:
        run_metadata[centroid_url] = {}
    
    if centroid_url and centroid_url in run_metadata:
        # Update run_metadata with centroid commit hash and timestamp if found
        if centroid_metadata.get("commit_hash"):
            run_metadata[centroid_url]["commit_hash"] = centroid_metadata["commit_hash"]
        if centroid_metadata.get("timestamp"):
            # Store timestamp in a way that can be used later
            # Note: We don't have a "timestamp" field in run_metadata, but we can store it
            # For now, we'll rely on the timestamp being in the centroid line format
    
    return {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_url": issue_url,
        "issue_state": issue_state,
        "centroid_error": centroid_error,
        "failing_runs": failing_runs,
        "run_metadata": run_metadata,
        "centroid_metadata": centroid_metadata,
        "num_occurrences": len(failing_runs)
    }

# ============================================================================
# Main Function
# ============================================================================

def get_repository_issues() -> List[Dict[str, Any]]:
    """Get all open issues from the repository."""
    if not GITHUB_REPO_OWNER or not GITHUB_REPO_NAME:
        print("ERROR: github_repo_owner or github_repo_name not found in secrets.json")
        return []
    
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    all_issues = []
    page = 1
    per_page = 100
    
    print(f"Fetching open issues from repository {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}...")
    
    while True:
        params = {
            "state": "open",  # Only fetch open issues
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
            
            print(f"  Page {page}: Found {len(actual_issues)} issue(s)")
            
            if len(issues) < per_page:
                break
            
            page += 1
            time.sleep(0.5)  # Rate limiting
            
        except Exception as e:
            print(f"ERROR: Failed to fetch repository issues: {e}")
            break
    
    return all_issues

def main():
    """Main function."""
    print("="*80)
    print("Downloading issues from GitHub repository")
    print("="*80)
    
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not found in secrets.json")
        sys.exit(1)
    
    if not GITHUB_REPO_OWNER or not GITHUB_REPO_NAME:
        print("ERROR: github_repo_owner or github_repo_name not found in secrets.json")
        sys.exit(1)
    
    # Get all open issues from the repository
    print(f"\nFetching all open issues from repository...")
    issues = get_repository_issues()
    print(f"Found {len(issues)} open issue(s)")
    
    if len(issues) == 0:
        print("No open issues found in repository.")
        # Still create empty file to avoid errors downstream
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=2, ensure_ascii=False)
        print(f"Created empty {OUTPUT_FILE}")
        return
    
    # Parse each issue
    print(f"\nParsing issues...")
    parsed_issues = []
    total_urls_extracted = 0
    for idx, issue in enumerate(issues, 1):
        issue_num = issue.get('number', 'unknown')
        issue_state = issue.get('state', 'unknown')
        print(f"  Processing issue {idx}/{len(issues)}: #{issue_num} ({issue_state})")
        parsed = parse_issue(issue)
        urls_count = len(parsed.get("failing_runs", []))
        total_urls_extracted += urls_count
        if urls_count > 0:
            print(f"    → Extracted {urls_count} URL(s)")
        parsed_issues.append(parsed)
    
    print(f"\n  Total URLs extracted from all issues: {total_urls_extracted}")
    
    # Create output structure - simplified format: list of entries
    # Each entry has centroid_error, failing_runs, and run_metadata
    # Only include OPEN issues - closed issues are not useful for the sync process
    output = []
    closed_count = 0
    for parsed in parsed_issues:
        if parsed.get("issue_state", "").upper() == "CLOSED":
            closed_count += 1
            continue  # Skip closed issues
        output.append({
            "centroid_error": parsed["centroid_error"],
            "failing_runs": parsed["failing_runs"],
            "run_metadata": parsed.get("run_metadata", {})
        })
    
    if closed_count > 0:
        print(f"  Skipped {closed_count} closed issue(s)")
    
    # Save to file
    print(f"\nSaving results to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"✓ Successfully saved {len(output)} issue(s) to {OUTPUT_FILE}")
    
    # Print summary
    print(f"\n{'='*80}")
    print("Summary:")
    print(f"  Total issues: {len(output)}")
    total_runs = sum(len(entry["failing_runs"]) for entry in output)
    print(f"  Total failing runs: {total_runs}")
    print(f"  Average runs per issue: {total_runs / len(output):.1f}" if output else "  Average runs per issue: 0")
    print(f"{'='*80}")

if __name__ == "__main__":
    main()
