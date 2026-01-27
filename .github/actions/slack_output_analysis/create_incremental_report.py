#!/usr/bin/env python3
"""
Create an incremental error report containing only NEW entries.

Compares the current error_report.json (JSON A) with the previous run's
error_report.json and outputs only the entries with github_job_id values
that weren't in the previous report.

This is used to feed the Pydantic model with only new data, avoiding
duplicate key violations in the database.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CURRENT_REPORT = os.path.join(SCRIPT_DIR, "error_report.json")
PREVIOUS_REPORT = os.environ.get("PREVIOUS_REPORT_PATH", "")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "incremental_error_report.json")


def main():
    print("=" * 80)
    print("Creating incremental error report (new entries only)")
    print("=" * 80)
    
    # Load current report
    print(f"\nLoading current report: {CURRENT_REPORT}")
    try:
        with open(CURRENT_REPORT, 'r', encoding='utf-8') as f:
            current_data = json.load(f)
        print(f"  Current report has {len(current_data)} entries")
    except FileNotFoundError:
        print(f"ERROR: Current report not found: {CURRENT_REPORT}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in current report: {e}")
        sys.exit(1)
    
    # Load previous report (if exists)
    previous_job_ids: set = set()
    if PREVIOUS_REPORT and os.path.exists(PREVIOUS_REPORT):
        print(f"\nLoading previous report: {PREVIOUS_REPORT}")
        try:
            with open(PREVIOUS_REPORT, 'r', encoding='utf-8') as f:
                previous_data = json.load(f)
            # Filter out None values to avoid incorrect matching
            previous_job_ids = {
                entry.get("github_job_id") 
                for entry in previous_data 
                if entry.get("github_job_id") is not None
            }
            print(f"  Previous report has {len(previous_data)} entries")
            print(f"  Found {len(previous_job_ids)} unique job IDs to exclude")
        except json.JSONDecodeError as e:
            print(f"WARNING: Invalid JSON in previous report, treating as empty: {e}")
        except Exception as e:
            print(f"WARNING: Could not load previous report, treating as empty: {e}")
    else:
        print(f"\nNo previous report found - all entries are new")
        if PREVIOUS_REPORT:
            print(f"  (Looked for: {PREVIOUS_REPORT})")
    
    # Filter to only new entries
    print(f"\nFiltering to new entries only...")
    new_entries = []
    skipped_count = 0
    skipped_no_id = 0
    
    for entry in current_data:
        job_id = entry.get("github_job_id")
        # Skip entries without github_job_id - can't reliably track for duplicates
        if job_id is None:
            skipped_no_id += 1
            continue
        if job_id in previous_job_ids:
            skipped_count += 1
        else:
            new_entries.append(entry)
    
    print(f"  Entries in current report: {len(current_data)}")
    print(f"  Entries already in previous report: {skipped_count}")
    print(f"  Entries without job ID (excluded): {skipped_no_id}")
    print(f"  New entries: {len(new_entries)}")
    
    # Save incremental report
    print(f"\nSaving incremental report: {OUTPUT_FILE}")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_entries, f, indent=2, ensure_ascii=False)
    
    print(f"  ✓ Saved {len(new_entries)} new entries to {OUTPUT_FILE}")
    
    # Summary
    print(f"\n{'=' * 80}")
    print("Summary:")
    print(f"  Full report (error_report.json): {len(current_data)} entries")
    print(f"  Incremental report (incremental_error_report.json): {len(new_entries)} entries")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
