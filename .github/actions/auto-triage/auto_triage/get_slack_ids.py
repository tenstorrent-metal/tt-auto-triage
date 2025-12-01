#!/usr/bin/env python3
"""
Lookup Slack member IDs for one or more developers by name.

Requirements:
  * A Slack bot token with the `users:read` scope.
  * The token must be provided via the SLACK_BOT_TOKEN environment variable.

Usage:
  ./auto_triage/get_slack_ids.py "Alice Smith" "bob.jones"

Optional flags:
  --limit N        Return up to N matches per query (default: 1).
  --include-bots   Include bot/service accounts in results.
  --json           Emit machine-readable JSON instead of a table.
"""

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Tuple

import requests


SLACK_API_BASE = "https://slack.com/api"


def normalize(value: str) -> str:
    """Normalize a string for fuzzy comparisons."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve Slack user IDs by name.")
    parser.add_argument(
        "query",
        nargs="+",
        help="Developer names, GitHub handles, or email prefixes to search for.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Maximum matches to return per query (default: 1).",
    )
    parser.add_argument(
        "--include-bots",
        action="store_true",
        help="Include bot/service accounts in search results.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output (one array entry per query).",
    )
    return parser


def fetch_all_users(token: str) -> List[Dict]:
    """Fetch all Slack users (auto-pagination)."""
    users = []
    cursor = None
    params = {"limit": 200}

    while True:
        if cursor:
            params["cursor"] = cursor
        response = requests.get(
            f"{SLACK_API_BASE}/users.list",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(
                f"Slack API error: {data.get('error', 'unknown_error')}"
            )
        users.extend(data.get("members", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return users


def score_user(query_norm: str, user: Dict) -> Tuple[int, str]:
    """Return a (score, reason) tuple for how well a user matches the query."""
    profile = user.get("profile", {})
    candidates = [
        ("display name", profile.get("display_name")),
        ("real name", user.get("real_name")),
        ("username", user.get("name")),
        ("email", profile.get("email")),
    ]

    best_score = 0
    best_reason = ""

    for label, value in candidates:
        if not value:
            continue
        norm_value = normalize(value)
        if not norm_value:
            continue
        if norm_value == query_norm:
            score = 100
            reason = f"exact match on {label}"
        elif query_norm in norm_value:
            score = 70
            reason = f"substring match on {label}"
        else:
            continue

        if score > best_score:
            best_score = score
            best_reason = reason

    return best_score, best_reason


def search_users(
    query: str, users: List[Dict], include_bots: bool, limit: int
) -> List[Dict]:
    """Return the top matches for query."""
    query_norm = normalize(query)
    matches: List[Tuple[int, Dict, str]] = []

    for user in users:
        if user.get("deleted"):
            continue
        if (user.get("is_bot") or user.get("id") == "USLACKBOT") and not include_bots:
            continue
        score, reason = score_user(query_norm, user)
        if score > 0:
            matches.append((score, user, reason))

    matches.sort(key=lambda item: item[0], reverse=True)
    results = []
    for score, user, reason in matches[:limit]:
        profile = user.get("profile", {})
        results.append(
            {
                "query": query,
                "id": user.get("id"),
                "display_name": profile.get("display_name"),
                "real_name": user.get("real_name"),
                "email": profile.get("email"),
                "reason": reason,
                "score": score,
            }
        )

    return results


def emit_table(rows: List[List[Dict]]):
    for group in rows:
        if not group:
            print("No matches found.")
            print("-" * 60)
            continue
        query = group[0]["query"]
        print(f"Query: {query}")
        print("-" * 60)
        for match in group:
            print(
                f"{match['id']:<12}  {match.get('real_name') or match.get('display_name') or '(unknown)'}"
            )
            if match.get("display_name"):
                print(f"  Display: {match['display_name']}")
            if match.get("email"):
                print(f"  Email:   {match['email']}")
            print(f"  Reason:  {match['reason']} (score {match['score']})")
            print("")
        print("-" * 60)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        print("Error: SLACK_BOT_TOKEN is not set.", file=sys.stderr)
        return 1

    try:
        users = fetch_all_users(token)
    except Exception as exc:  # pragma: no cover - CLI utility
        print(f"Failed to fetch users: {exc}", file=sys.stderr)
        return 2

    all_results = []
    for query in args.query:
        matches = search_users(query, users, args.include_bots, args.limit)
        all_results.append(matches)

    if args.json:
        print(json.dumps(all_results, indent=2))
    else:
        emit_table(all_results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

