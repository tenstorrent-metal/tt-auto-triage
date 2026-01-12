#!/usr/bin/env python3
"""
Group similar errors using semantic similarity and RapidFuzz.
Reads errors from errors.json and outputs grouped errors.
"""

import json
import numpy as np
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from collections import defaultdict

# Configuration
import os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ERRORS_FILE = os.path.join(SCRIPT_DIR, "all_errors.json")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "grouped_errors.json")
SEMANTIC_THRESHOLD = 70
RAPIDFUZZ_THRESHOLD = 50

# Load errors
print(f"Loading errors from {ERRORS_FILE}...")
with open(ERRORS_FILE, 'r') as f:
    data = json.load(f)
    # Handle both ["error1", "error2"] and {"errors": ["error1", "error2"]}
    raw_errors = data if isinstance(data, list) else data.get('errors', data.get('error', []))

# Parse error format: can be string or [message, url, timestamp, job_name, workflow_name, is_nd] array
errors_with_metadata = []
error_messages = []
for item in raw_errors:
    if isinstance(item, list) and len(item) >= 6:
        # New format: [message, url, timestamp, job_name, workflow_name, is_nd]
        error_messages.append(item[0])
        errors_with_metadata.append({
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": item[2] if len(item) > 2 and item[2] else "",
            "job_name": item[3] if len(item) > 3 and item[3] is not None else "",
            "workflow_name": item[4] if len(item) > 4 and item[4] is not None else "",
            "is_nd": item[5] if len(item) > 5 and item[5] is not None else False
        })
    elif isinstance(item, list) and len(item) >= 5:
        # Format: [message, url, timestamp, job_name, workflow_name] - no is_nd (backward compatibility)
        error_messages.append(item[0])
        errors_with_metadata.append({
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": item[2] if len(item) > 2 and item[2] else "",
            "job_name": item[3] if len(item) > 3 and item[3] is not None else "",
            "workflow_name": item[4] if len(item) > 4 and item[4] is not None else "",
            "is_nd": False  # Default to False for old format
        })
    elif isinstance(item, list) and len(item) >= 3:
        # Format: [message, url, timestamp] - no job/workflow or is_nd
        error_messages.append(item[0])
        errors_with_metadata.append({
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": item[2] if item[2] else "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        })
    elif isinstance(item, list) and len(item) == 2:
        # Format: [message, url] - no timestamp, job/workflow, or is_nd
        error_messages.append(item[0])
        errors_with_metadata.append({
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        })
    elif isinstance(item, list) and len(item) == 1:
        # Format: [message] - no URL, timestamp, job/workflow, or is_nd
        error_messages.append(item[0])
        errors_with_metadata.append({
            "error": item[0], 
            "url": "", 
            "timestamp": "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        })
    elif isinstance(item, str):
        # Old format: just string
        error_messages.append(item)
        errors_with_metadata.append({
            "error": item, 
            "url": "", 
            "timestamp": "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        })
    else:
        # Skip invalid entries
        continue

print(f"Found {len(error_messages)} errors")

# Encode all error messages once
print("Encoding errors...")
model = SentenceTransformer('all-MiniLM-L6-v2')
embeddings = model.encode(error_messages)

# Compute semantic similarity matrix
print("Computing semantic similarities...")
semantic_matrix = cosine_similarity(embeddings) * 100

# Build graph: edge if both thresholds met
print("Building similarity graph...")
graph = defaultdict(list)
for i in range(len(error_messages)):
    for j in range(i + 1, len(error_messages)):
        semantic_score = semantic_matrix[i][j]
        rapidfuzz_score = fuzz.token_set_ratio(error_messages[i], error_messages[j])
        
        if semantic_score >= SEMANTIC_THRESHOLD and rapidfuzz_score >= RAPIDFUZZ_THRESHOLD:
            graph[i].append(j)
            graph[j].append(i)

# Find connected components (groups)
print("Finding groups...")
visited = set()
groups = []

def dfs(node, current_group):
    """Depth-first search to find connected component."""
    visited.add(node)
    current_group.append(node)
    for neighbor in graph[node]:
        if neighbor not in visited:
            dfs(neighbor, current_group)

for i in range(len(error_messages)):
    if i not in visited:
        group = []
        dfs(i, group)
        groups.append(group)

# Sort groups by size (descending order)
groups.sort(key=len, reverse=True)

# Convert groups to error lists (preserve error + URL + timestamp format)
# For each group, find the centroid and reorder so the closest error is first
print("Finding centroid errors for each group...")
grouped_errors = {}
for group_idx, group in enumerate(groups, 1):
    group_name = f"group_{group_idx}"
    
    if len(group) == 1:
        # Single error group - centroid is the same as the single error
        centroid_error = errors_with_metadata[group[0]]
        group_errors = [errors_with_metadata[group[0]]]
    else:
        # Calculate centroid of embeddings for this group
        group_embeddings = embeddings[group]
        centroid = np.mean(group_embeddings, axis=0)
        
        # Find the error closest to the centroid
        # Calculate cosine similarity between centroid and each error in the group
        centroid_similarities = cosine_similarity([centroid], group_embeddings)[0]
        closest_idx = np.argmax(centroid_similarities)
        
        # Store the centroid error
        centroid_error = errors_with_metadata[group[closest_idx]]
        
        # Reorder group so closest error is first
        reordered_group = [group[closest_idx]] + [group[i] for i in range(len(group)) if i != closest_idx]
        group_errors = [errors_with_metadata[i] for i in reordered_group]
    
    grouped_errors[group_name] = {
        "count": len(group_errors),
        "centroid": centroid_error,
        "errors": group_errors
    }

# Output results
print(f"\nFound {len(groups)} groups:")
for group_idx, group in enumerate(groups, 1):
    print(f"  Group {group_idx}: {len(group)} error(s)")

with open(OUTPUT_FILE, 'w') as f:
    json.dump(grouped_errors, f, indent=2)

print(f"\nResults saved to {OUTPUT_FILE}")
