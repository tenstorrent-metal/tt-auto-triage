#!/usr/bin/env python3
"""
Generate error report JSON and markdown summary.
Creates a report with job URL, error message, ND flag, and centroid issue URL.
"""

import json
import os
import sys
from typing import Dict, List, Any, Optional

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

def get_github_issue_url(issue_number: int, repo_owner: str, repo_name: str) -> str:
    """Generate GitHub issue URL from issue number."""
    return f"https://github.com/{repo_owner}/{repo_name}/issues/{issue_number}"

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
    
    # Fetch GitHub issues
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    centroid_to_issue = {}
    page = 1
    per_page = 100
    
    print("Fetching GitHub issues to map centroids to issue URLs...")
    while True:
        try:
            params = {
                "state": "all",
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
                    # Try to match with centroids in issue_dump
                    for entry in issue_dump:
                        centroid_error = entry.get("centroid_error", "")
                        if not centroid_error:
                            continue
                        # Try exact match
                        if centroid_error.strip() == centroid_from_issue.strip():
                            centroid_to_issue[centroid_error] = issue["number"]
                            break
                        # Try case-insensitive match
                        if centroid_error.strip().lower() == centroid_from_issue.strip().lower():
                            centroid_to_issue[centroid_error] = issue["number"]
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
    # Load all errors
    try:
        with open(ALL_ERRORS_FILE, 'r', encoding='utf-8') as f:
            all_errors = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {ALL_ERRORS_FILE} not found")
        sys.exit(1)
    
    # Load issue dump
    try:
        with open(ISSUE_DUMP_FILE, 'r', encoding='utf-8') as f:
            issue_dump = json.load(f)
    except FileNotFoundError:
        print(f"⚠ Warning: {ISSUE_DUMP_FILE} not found, centroid URLs will be missing")
        issue_dump = []
    
    # Get centroid to issue mapping
    centroid_to_issue = get_centroid_to_issue_mapping(issue_dump)
    secrets = load_secrets()
    repo_owner = secrets["GITHUB_REPO_OWNER"]
    repo_name = secrets["GITHUB_REPO_NAME"]
    
    # Get centroids for matching
    centroids = [entry["centroid_error"] for entry in issue_dump]
    
    # Generate report entries
    report_entries = []
    matched_count = 0
    unmatched_count = 0
    
    for error_entry in all_errors:
        error_message = error_entry[0] if len(error_entry) > 0 else ""
        job_url = error_entry[1] if len(error_entry) > 1 else None
        is_nd = error_entry[5] if len(error_entry) > 5 else False
        
        if not error_message:
            continue
        
        # Find matching centroid
        centroid_issue_url = None
        if centroids:
            best_idx, best_scores = find_best_matching_centroid(
                error_message,
                centroids,
                rapidfuzz_threshold=RAPIDFUZZ_THRESHOLD,
                semantic_threshold=SEMANTIC_THRESHOLD
            )
            
            if best_idx is not None:
                matched_count += 1
                centroid_error = centroids[best_idx]
                issue_number = centroid_to_issue.get(centroid_error)
                if issue_number:
                    centroid_issue_url = get_github_issue_url(issue_number, repo_owner, repo_name)
            else:
                unmatched_count += 1
        else:
            unmatched_count += 1
        
        report_entry = {
            "job_url": job_url,
            "error_message": error_message,
            "is_nd": is_nd,
            "centroid_issue_url": centroid_issue_url
        }
        report_entries.append(report_entry)
    
    # Generate markdown summary
    total_errors = len(report_entries)
    nd_errors = sum(1 for entry in report_entries if entry["is_nd"])
    errors_with_centroid = sum(1 for entry in report_entries if entry["centroid_issue_url"])
    
    nd_percentage = (nd_errors/total_errors*100) if total_errors > 0 else 0
    centroid_percentage = (errors_with_centroid/total_errors*100) if total_errors > 0 else 0
    
    markdown = f"""# Error Report Summary

## Statistics

- **Total Errors**: {total_errors}
- **ND (Non-Deterministic) Errors**: {nd_errors} ({nd_percentage:.1f}% of total)
- **Errors with Centroid Issues**: {errors_with_centroid} ({centroid_percentage:.1f}% of total)
- **Matched to Centroids**: {matched_count}
- **Unmatched Errors**: {unmatched_count}

## Error Breakdown

### ND Errors by Status

- **ND Errors with Centroid Issues**: {sum(1 for e in report_entries if e['is_nd'] and e['centroid_issue_url'])}
- **ND Errors without Centroid Issues**: {sum(1 for e in report_entries if e['is_nd'] and not e['centroid_issue_url'])}

### Non-ND Errors by Status

- **Non-ND Errors with Centroid Issues**: {sum(1 for e in report_entries if not e['is_nd'] and e['centroid_issue_url'])}
- **Non-ND Errors without Centroid Issues**: {sum(1 for e in report_entries if not e['is_nd'] and not e['centroid_issue_url'])}

## Sample Errors

"""
    
    # Add sample entries (first 10)
    for i, entry in enumerate(report_entries[:10], 1):
        nd_badge = "🟡 ND" if entry["is_nd"] else "⚪"
        centroid_link = f"[View Issue]({entry['centroid_issue_url']})" if entry["centroid_issue_url"] else "*No centroid issue*"
        job_link = f"[Job URL]({entry['job_url']})" if entry["job_url"] else "*No job URL*"
        
        error_preview = entry["error_message"][:100].replace("\n", " ") + "..." if len(entry["error_message"]) > 100 else entry["error_message"]
        
        markdown += f"""### Error {i} {nd_badge}

- **Job**: {job_link}
- **Centroid Issue**: {centroid_link}
- **Error Message**: `{error_preview}`

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
