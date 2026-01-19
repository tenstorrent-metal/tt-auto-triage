#!/usr/bin/env python3
"""
Group similar errors using semantic similarity and RapidFuzz.
Reads errors from errors.json and outputs grouped errors.
"""

import json
import os
from typing import Dict, List, Any, Optional, Tuple

import numpy as np
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# Configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ERRORS_FILE = os.path.join(SCRIPT_DIR, "all_errors.json")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "grouped_errors.json")
# High thresholds to prevent matching different errors that share boilerplate
# e.g., "TT_THROW @ path1: Device init failed" vs "TT_THROW @ path2: Device timeout"
SEMANTIC_THRESHOLD = 85
RAPIDFUZZ_THRESHOLD = 70


def parse_error_item(item: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Parse an error item into message and metadata.
    
    Args:
        item: Can be a string, or a list of various lengths containing error data
        
    Returns:
        Tuple of (error_message, metadata_dict) or None if invalid
    """
    if isinstance(item, list) and len(item) >= 6:
        # New format: [message, url, timestamp, job_name, workflow_name, is_nd]
        return item[0], {
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": item[2] if len(item) > 2 and item[2] else "",
            "job_name": item[3] if len(item) > 3 and item[3] is not None else "",
            "workflow_name": item[4] if len(item) > 4 and item[4] is not None else "",
            "is_nd": item[5] if len(item) > 5 and item[5] is not None else False
        }
    elif isinstance(item, list) and len(item) >= 5:
        # Format: [message, url, timestamp, job_name, workflow_name] - no is_nd (backward compatibility)
        return item[0], {
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": item[2] if len(item) > 2 and item[2] else "",
            "job_name": item[3] if len(item) > 3 and item[3] is not None else "",
            "workflow_name": item[4] if len(item) > 4 and item[4] is not None else "",
            "is_nd": False
        }
    elif isinstance(item, list) and len(item) >= 3:
        # Format: [message, url, timestamp] - no job/workflow or is_nd
        return item[0], {
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": item[2] if item[2] else "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        }
    elif isinstance(item, list) and len(item) == 2:
        # Format: [message, url] - no timestamp, job/workflow, or is_nd
        return item[0], {
            "error": item[0], 
            "url": item[1] if item[1] else "", 
            "timestamp": "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        }
    elif isinstance(item, list) and len(item) == 1:
        # Format: [message] - no URL, timestamp, job/workflow, or is_nd
        return item[0], {
            "error": item[0], 
            "url": "", 
            "timestamp": "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        }
    elif isinstance(item, str):
        # Old format: just string
        return item, {
            "error": item, 
            "url": "", 
            "timestamp": "",
            "job_name": "",
            "workflow_name": "",
            "is_nd": False
        }
    else:
        # Invalid entry
        return None


def load_errors(errors_file: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Load and parse errors from file.
    
    Returns:
        Tuple of (error_messages, errors_with_metadata)
    """
    print(f"Loading errors from {errors_file}...")
    with open(errors_file, 'r') as f:
        data = json.load(f)
        # Handle both ["error1", "error2"] and {"errors": ["error1", "error2"]}
        raw_errors = data if isinstance(data, list) else data.get('errors', data.get('error', []))
    
    errors_with_metadata = []
    error_messages = []
    
    for item in raw_errors:
        result = parse_error_item(item)
        if result is not None:
            message, metadata = result
            error_messages.append(message)
            errors_with_metadata.append(metadata)
    
    print(f"Found {len(error_messages)} errors")
    return error_messages, errors_with_metadata


def cluster_errors(error_messages: List[str], semantic_threshold: float, rapidfuzz_threshold: float) -> Tuple[List[List[int]], np.ndarray]:
    """Cluster errors using semantic similarity and RapidFuzz.
    
    Returns:
        Tuple of (groups, embeddings) where groups is a list of lists of indices
    """
    # Encode all error messages once
    print("Encoding errors...")
    model = SentenceTransformer('all-MiniLM-L6-v2')
    embeddings = model.encode(error_messages)
    
    # Compute semantic similarity matrix
    print("Computing semantic similarities...")
    semantic_matrix = cosine_similarity(embeddings) * 100
    
    # Use strict centroid-based clustering: all errors must be similar to the centroid
    # This prevents transitive chaining where A->B->C groups dissimilar A and C together
    print("Clustering errors using strict centroid-based approach...")
    visited = set()
    groups = []
    
    for i in range(len(error_messages)):
        if i in visited:
            continue
        
        # Start a new group with this error as the centroid
        centroid_idx = i
        current_group = [i]
        visited.add(i)
        
        # Find all unvisited errors that are similar to THIS centroid
        # Only add errors that are directly similar to the centroid, not transitively
        for j in range(len(error_messages)):
            if j in visited:
                continue
            
            # Check similarity to the centroid (not to other group members)
            semantic_score = semantic_matrix[i][j]  # Use precomputed matrix
            rapidfuzz_score = fuzz.token_set_ratio(error_messages[centroid_idx], error_messages[j])
            
            if semantic_score >= semantic_threshold and rapidfuzz_score >= rapidfuzz_threshold:
                current_group.append(j)
                visited.add(j)
        
        groups.append(current_group)
    
    # Sort groups by size (descending order)
    groups.sort(key=len, reverse=True)
    
    return groups, embeddings


def build_grouped_errors(groups: List[List[int]], errors_with_metadata: List[Dict[str, Any]], embeddings: np.ndarray) -> Dict[str, Any]:
    """Build the grouped errors dictionary with centroids.
    
    Returns:
        Dictionary mapping group names to group data
    """
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
    
    return grouped_errors


def main():
    """Main function to group similar errors."""
    # Load errors
    error_messages, errors_with_metadata = load_errors(ERRORS_FILE)
    
    if not error_messages:
        print("No errors found. Exiting.")
        return
    
    # Cluster errors
    groups, embeddings = cluster_errors(error_messages, SEMANTIC_THRESHOLD, RAPIDFUZZ_THRESHOLD)
    
    # Build grouped errors
    grouped_errors = build_grouped_errors(groups, errors_with_metadata, embeddings)
    
    # Output results
    print(f"\nFound {len(groups)} groups:")
    for group_idx, group in enumerate(groups, 1):
        print(f"  Group {group_idx}: {len(group)} error(s)")
    
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(grouped_errors, f, indent=2)
    
    print(f"\nResults saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
