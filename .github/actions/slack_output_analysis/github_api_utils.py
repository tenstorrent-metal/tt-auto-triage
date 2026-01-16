#!/usr/bin/env python3
"""
Utility functions for GitHub API interactions, including rate limit checking.
"""

import requests
from typing import Dict, Optional, Tuple


def check_github_rate_limit(github_token: str) -> Optional[Dict[str, int]]:
    """Check GitHub API rate limit status.
    
    Args:
        github_token: GitHub token for API access
    
    Returns:
        Dictionary with 'remaining', 'limit', 'reset' keys, or None if error
    """
    if not github_token:
        return None
    
    try:
        url = "https://api.github.com/rate_limit"
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        core = data.get("resources", {}).get("core", {})
        return {
            "remaining": core.get("remaining", 0),
            "limit": core.get("limit", 5000),
            "reset": core.get("reset", 0)
        }
    except Exception as e:
        print(f"⚠ Warning: Could not check GitHub API rate limit: {e}")
        return None


def log_rate_limit_status(github_token: str, stage: str = "") -> None:
    """Log GitHub API rate limit status.
    
    Args:
        github_token: GitHub token for API access
        stage: Optional stage label (e.g., "start", "end")
    """
    rate_limit = check_github_rate_limit(github_token)
    if rate_limit:
        remaining = rate_limit["remaining"]
        limit = rate_limit["limit"]
        reset = rate_limit["reset"]
        
        # Calculate reset time
        from datetime import datetime
        reset_time = datetime.fromtimestamp(reset) if reset else None
        reset_str = reset_time.strftime("%Y-%m-%d %H:%M:%S") if reset_time else "unknown"
        
        stage_label = f" [{stage}]" if stage else ""
        print(f"\n{'='*60}")
        print(f"GitHub API Rate Limit{stage_label}:")
        print(f"  Remaining: {remaining:,} / {limit:,} ({remaining/limit*100:.1f}%)")
        print(f"  Resets at: {reset_str}")
        print(f"{'='*60}\n")
    else:
        print(f"\n⚠ Warning: Could not check GitHub API rate limit{stage}\n")


def get_commit_hash_from_github(job_url: str, github_token: str) -> Optional[str]:
    """Fetch full commit hash from GitHub API using job URL.
    
    Args:
        job_url: GitHub Actions job URL (e.g., https://github.com/owner/repo/actions/runs/RUN_ID/job/JOB_ID)
        github_token: GitHub token for API access
    
    Returns:
        Full commit SHA (40 characters) or None if not found
    """
    if not job_url or not github_token:
        return None
    
    try:
        import re
        # Extract run ID from URL: https://github.com/owner/repo/actions/runs/RUN_ID/job/JOB_ID
        match = re.search(r'/actions/runs/(\d+)', job_url)
        if not match:
            return None
        
        run_id = match.group(1)
        
        # Extract repo owner and name from URL
        repo_match = re.search(r'github\.com/([^/]+)/([^/]+)/actions', job_url)
        if not repo_match:
            return None
        
        repo_owner = repo_match.group(1)
        repo_name = repo_match.group(2)
        
        # Fetch workflow run details
        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/actions/runs/{run_id}"
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        run_data = response.json()
        
        # Get the commit SHA (head_sha is the full commit hash)
        commit_sha = run_data.get("head_sha")
        if commit_sha and len(commit_sha) == 40:
            return commit_sha
        
    except Exception as e:
        print(f"  ⚠ Warning: Could not fetch commit hash for {job_url}: {e}")
    
    return None
