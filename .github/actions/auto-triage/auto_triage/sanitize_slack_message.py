#!/usr/bin/env python3
"""
Ensure slack_message.json is valid JSON even if the LLM wrapped it in extra tags.

Usage:
    ./sanitize_slack_message.py ./output/slack_message.json
"""

import json
import re
import sys
from pathlib import Path


def normalize_candidates(raw: str):
    candidates = []
    candidates.append(raw)

    # Remove common XML-like wrapper tags the LLM sometimes emits
    stripped = re.sub(r"<\/?content[^>]*>", "", raw)
    stripped = re.sub(r"<parameter[^>]*>", "", stripped)
    candidates.append(stripped)

    # Extract the outermost JSON object if extra text surrounds it
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(raw[first_brace : last_brace + 1])

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item.strip())
    return unique


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: sanitize_slack_message.py <slack_message.json>", file=sys.stderr)
        return 2

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Error: {target} does not exist.", file=sys.stderr)
        return 1

    raw = target.read_text(encoding="utf-8")
    candidates = normalize_candidates(raw)

    last_error = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            target.write_text(json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"Sanitized Slack message written to {target}")
            return 0
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    print(f"Failed to parse {target}: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

