#!/usr/bin/env python3
"""
Utility functions for GitHub API interactions, including rate limit checking.
"""

import re
from typing import Dict, Optional

import requests

# Module-level cache for commit hashes to avoid redundant API calls
# Maps URL -> commit_hash (or None if not found)
_commit_hash_cache: Dict[str, Optional[str]] = {}


def get_commit_hash_cache_stats() -> Dict[str, int]:
    """Get statistics about the commit hash cache."""
    return {
        "total_entries": len(_commit_hash_cache),
        "found": sum(1 for v in _commit_hash_cache.values() if v is not None),
        "not_found": sum(1 for v in _commit_hash_cache.values() if v is None)
    }


def clear_commit_hash_cache() -> None:
    """Clear the commit hash cache."""
    global _commit_hash_cache
    _commit_hash_cache = {}


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


def get_commit_hash_from_github(job_url: str, github_token: str, use_cache: bool = True) -> Optional[str]:
    """Fetch full commit hash from GitHub API using job URL.
    
    Uses a module-level cache to avoid redundant API calls for the same URL.
    
    Args:
        job_url: GitHub Actions job URL (e.g., https://github.com/owner/repo/actions/runs/RUN_ID/job/JOB_ID)
        github_token: GitHub token for API access
        use_cache: Whether to use the cache (default True)
    
    Returns:
        Full commit SHA (40 characters) or None if not found
    """
    global _commit_hash_cache
    
    if not job_url or not github_token:
        return None
    
    # Normalize URL for cache key (remove trailing slashes)
    cache_key = job_url.rstrip('/')
    
    # Check cache first
    if use_cache and cache_key in _commit_hash_cache:
        cached = _commit_hash_cache[cache_key]
        # Return cached value (could be None if previous fetch failed)
        return cached
    
    try:
        # Extract run ID from URL: https://github.com/owner/repo/actions/runs/RUN_ID/job/JOB_ID
        match = re.search(r'/actions/runs/(\d+)', job_url)
        if not match:
            _commit_hash_cache[cache_key] = None
            return None
        
        run_id = match.group(1)
        
        # Extract repo owner and name from URL
        repo_match = re.search(r'github\.com/([^/]+)/([^/]+)/actions', job_url)
        if not repo_match:
            _commit_hash_cache[cache_key] = None
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
            _commit_hash_cache[cache_key] = commit_sha
            return commit_sha
        
        _commit_hash_cache[cache_key] = None
        return None
        
    except Exception as e:
        print(f"  ⚠ Warning: Could not fetch commit hash for {job_url}: {e}")
        _commit_hash_cache[cache_key] = None
        return None
