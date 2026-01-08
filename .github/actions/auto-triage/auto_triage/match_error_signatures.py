#!/usr/bin/env python3
"""
Minimal error matching using RapidFuzz.
Paste errors as strings for testing.
"""

from rapidfuzz import fuzz

# Paste your errors here
error1 = """
UMD mutex deadlock on 'NON_MMIO_3_PCIe' causing 10-minute timeout during device initialization. Lock held by thread TID:2397 was never released, blocking subsequent device access.
"""

error2 = """
there was a umd deadlock on NON_MMIO_3_PCIe.
"""

# Compare using RapidFuzz's token_set_ratio (best for error messages)
score = fuzz.token_set_ratio(error1, error2)

print(f"Similarity Score: {score}/100")
print(f"Match: {'YES' if score >= 70 else 'NO'} (threshold: 70)")

# Also show other RapidFuzz metrics for comparison
print(f"\nOther metrics:")
print(f"  token_sort_ratio: {fuzz.token_sort_ratio(error1, error2)}")
print(f"  partial_ratio: {fuzz.partial_ratio(error1, error2)}")
print(f"  ratio: {fuzz.ratio(error1, error2)}")
