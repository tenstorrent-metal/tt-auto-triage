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


def require_type(value, expected_type, path):
    if not isinstance(value, expected_type):
        raise ValueError(f"{path} must be of type {expected_type.__name__}")


def require_string(value, path, allow_empty=False):
    require_type(value, str, path)
    if not allow_empty and not value.strip():
        raise ValueError(f"{path} must be a non-empty string")


def validate_person(person, path):
    require_type(person, dict, path)
    if "name" not in person:
        raise ValueError(f"{path}.name is required")
    require_string(person["name"], f"{path}.name")
    if "slack_id" in person and person["slack_id"] is not None:
        require_string(person["slack_id"], f"{path}.slack_id", allow_empty=True)


def validate_person_list(entries, path):
    require_type(entries, list, path)
    for idx, entry in enumerate(entries):
        validate_person(entry, f"{path}[{idx}]")


def validate_commit(commit, index):
    path = f"commits[{index}]"
    require_type(commit, dict, path)
    for key in ("hash",):
        if key not in commit:
            raise ValueError(f"{path}.{key} is required")
        require_string(commit[key], f"{path}.{key}")
    if "url" in commit and commit["url"] is not None:
        require_string(commit["url"], f"{path}.url")
    if "author" not in commit:
        raise ValueError(f"{path}.author is required")
    validate_person(commit["author"], f"{path}.author")
    if "approvers" in commit:
        validate_person_list(commit["approvers"], f"{path}.approvers")
    if "relevant_developers" in commit:
        validate_person_list(commit["relevant_developers"], f"{path}.relevant_developers")
    if "relevant_files" in commit:
        require_type(commit["relevant_files"], list, f"{path}.relevant_files")
        for file_idx, file_entry in enumerate(commit["relevant_files"]):
            require_string(file_entry, f"{path}.relevant_files[{file_idx}]")


def validate_payload(payload):
    require_type(payload, dict, "root")
    for key in ("case", "scenario", "failure_message", "slack_message"):
        if key not in payload:
            raise ValueError(f"Field '{key}' is required at the top level")
        require_string(payload[key], key)
    for key in ("failing_run_url", "failing_run_label"):
        if key in payload:
            require_string(payload[key], key)
    if "commits" not in payload:
        raise ValueError("Field 'commits' is required at the top level (can be an empty array)")
    require_type(payload["commits"], list, "commits")
    for idx, commit in enumerate(payload["commits"]):
        validate_commit(commit, idx)
    if "relevant_developers" in payload:
        validate_person_list(payload["relevant_developers"], "relevant_developers")
    if "relevant_files" in payload:
        require_type(payload["relevant_files"], list, "relevant_files")
        for idx, entry in enumerate(payload["relevant_files"]):
            require_string(entry, f"relevant_files[{idx}]")
    if "notes" in payload and payload["notes"] is not None:
        require_string(payload["notes"], "notes", allow_empty=True)


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
            validate_payload(parsed)
            target.write_text(json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"Sanitized Slack message written to {target}")
            return 0
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        except ValueError as exc:
            last_error = exc
            continue

    print(f"Failed to sanitize {target}: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

