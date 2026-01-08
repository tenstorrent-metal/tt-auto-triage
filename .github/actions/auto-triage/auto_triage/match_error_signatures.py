#!/usr/bin/env python3
"""
Error signature matching for non-deterministic error tracking.

Uses RapidFuzz for fuzzy string matching - a well-established library that
combines multiple matching strategies optimized for error message comparison.

RapidFuzz provides:
- Token-based matching (handles word order differences)
- Partial matching (finds substrings)
- Multiple scoring algorithms (ratio, partial_ratio, token_sort_ratio, token_set_ratio)
- Fast C++ implementation
"""

import re
import json
from typing import List, Dict, Tuple, Optional

try:
    from rapidfuzz import fuzz, process
except ImportError:
    print("Error: rapidfuzz not installed. Install with: pip install rapidfuzz")
    exit(1)


def normalize_error_message(text: str) -> str:
    """Normalize error message for better matching.
    
    - Lowercase everything
    - Normalize whitespace
    - Remove timestamps and common prefixes
    """
    # Remove common prefixes
    text = re.sub(r'^RuntimeError:\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^AssertionError:\s*', '', text, flags=re.IGNORECASE)
    
    # Remove timestamps (e.g., [2025-11-29T02:11:44Z])
    text = re.sub(r'\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?\]', '', text)
    
    # Normalize whitespace
    text = ' '.join(text.split())
    
    return text.lower()


def extract_key_signatures(text: str) -> Dict[str, str]:
    """Extract key signatures that should match exactly.
    
    Returns a dict with normalized signatures for:
    - file_paths: Base file paths (without line numbers)
    - technical_ids: Technical identifiers like NON_MMIO_3_PCIe
    """
    signatures = {
        'file_paths': set(),
        'technical_ids': set(),
    }
    
    # Extract file paths (normalize to base paths)
    file_patterns = [
        r'[@/](?:[\w\-_./]+/)*[\w\-_]+\.(?:cpp|hpp|py|cc|c|h)(?::\d+)?',
        r'\(([\w\-_./]+/)*[\w\-_]+\.(?:cpp|hpp|py|cc|c|h)(?::\d+)?\)',
    ]
    for pattern in file_patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0] if match[0] else ''
            if match:
                normalized = match.lstrip('@').strip('()')
                # Extract base path (without line number)
                if ':' in normalized:
                    base_path = normalized.rsplit(':', 1)[0]
                    signatures['file_paths'].add(base_path.lower())
                else:
                    signatures['file_paths'].add(normalized.lower())
    
    # Extract technical identifiers (uppercase constants, quoted strings)
    # Match patterns like 'NON_MMIO_3_PCIe', UMD, etc.
    quoted = re.findall(r"['\"]([A-Z0-9_]+)['\"]", text)
    signatures['technical_ids'].update([q.lower() for q in quoted])
    
    # Match uppercase identifiers
    uppercase = re.findall(r'\b[A-Z][A-Z0-9_]+[A-Z0-9a-z]\b', text)
    # Filter out common words
    common_words = {'THE', 'AND', 'FOR', 'WAS', 'NOT', 'ARE', 'ALL', 'BUT', 'CAN', 'HAS', 'ITS'}
    signatures['technical_ids'].update([
        u.lower() for u in uppercase if u not in common_words
    ])
    
    return signatures


def match_error_signatures(
    error1: str,
    error2: str,
    require_file_match: bool = True
) -> Tuple[float, Dict[str, float]]:
    """
    Compare two error messages using RapidFuzz.
    
    Uses multiple RapidFuzz strategies:
    1. token_set_ratio - Best for error messages (handles word order, duplicates)
    2. partial_ratio - Finds substring matches
    3. token_sort_ratio - Handles reordered words
    
    Args:
        error1: First error message
        error2: Second error message
        require_file_match: If True, require file paths to match for high confidence
    
    Returns:
        Tuple of (overall_score, component_scores_dict)
        Score is 0.0 to 100.0 (RapidFuzz uses 0-100 scale)
    """
    # Normalize both messages
    norm1 = normalize_error_message(error1)
    norm2 = normalize_error_message(error2)
    
    # Extract key signatures
    sig1 = extract_key_signatures(error1)
    sig2 = extract_key_signatures(error2)
    
    component_scores = {}
    
    # 1. Token set ratio (best for error messages - handles word order and duplicates)
    token_set_score = fuzz.token_set_ratio(norm1, norm2)
    component_scores['token_set_ratio'] = token_set_score
    
    # 2. Partial ratio (finds substring matches - good for when one error is more detailed)
    partial_score = fuzz.partial_ratio(norm1, norm2)
    component_scores['partial_ratio'] = partial_score
    
    # 3. Token sort ratio (handles reordered words)
    token_sort_score = fuzz.token_sort_ratio(norm1, norm2)
    component_scores['token_sort_ratio'] = token_sort_score
    
    # 4. Check file path matches (high confidence signal)
    file_paths_match = len(sig1['file_paths'] & sig2['file_paths']) > 0
    component_scores['file_paths_match'] = 100.0 if file_paths_match else 0.0
    
    # 5. Check technical identifier matches
    tech_ids_match = len(sig1['technical_ids'] & sig2['technical_ids']) > 0
    component_scores['technical_ids_match'] = 100.0 if tech_ids_match else 0.0
    
    # Calculate overall score
    # Use token_set_ratio as primary (best for error messages)
    # Boost if file paths or technical IDs match
    overall_score = token_set_score
    
    # Boost score if key signatures match
    if file_paths_match:
        # File path match is a strong signal - boost by up to 10 points
        overall_score = min(100.0, overall_score + 10.0)
    
    if tech_ids_match and not file_paths_match:
        # Technical ID match is also good, but less than file paths
        overall_score = min(100.0, overall_score + 5.0)
    
    # If require_file_match and no file match, cap the score lower
    if require_file_match and not file_paths_match:
        # Still allow matches, but cap at 85 to require other strong signals
        overall_score = min(85.0, overall_score)
    
    return overall_score, component_scores


def find_matching_errors(
    current_error: str,
    known_errors: List[Dict[str, str]],
    threshold: float = 70.0,
    require_file_match: bool = False
) -> List[Tuple[Dict[str, str], float, Dict[str, float]]]:
    """
    Find known errors that match the current error using RapidFuzz.
    
    Args:
        current_error: The error message to match
        known_errors: List of dicts with 'error_message' key (and optionally 'job', 'test')
        threshold: Minimum similarity score to consider a match (0-100, default 70)
        require_file_match: If True, require file paths to match for high confidence
    
    Returns:
        List of tuples: (error_dict, overall_score, component_scores)
        Sorted by score (highest first)
    """
    matches = []
    
    for known_error in known_errors:
        known_msg = known_error.get('error_message', '')
        if not known_msg:
            continue
        
        score, components = match_error_signatures(
            current_error,
            known_msg,
            require_file_match=require_file_match
        )
        
        if score >= threshold:
            matches.append((known_error, score, components))
    
    # Sort by score (highest first)
    matches.sort(key=lambda x: x[1], reverse=True)
    
    return matches


def main():
    """CLI interface for testing."""
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: match_error_signatures.py <error1> <error2> [threshold]")
        print("\nOr: match_error_signatures.py --compare-file <known_errors.json> <current_error> [threshold]")
        print("\nThreshold is 0-100 (default: 70). RapidFuzz uses 0-100 scale.")
        sys.exit(1)
    
    if sys.argv[1] == '--compare-file':
        # Compare against a file of known errors
        if len(sys.argv) < 4:
            print("Usage: match_error_signatures.py --compare-file <known_errors.json> <current_error> [threshold]")
            sys.exit(1)
        
        known_file = sys.argv[2]
        current_error = sys.argv[3]
        threshold = float(sys.argv[4]) if len(sys.argv) > 4 else 70.0
        
        with open(known_file, 'r') as f:
            data = json.load(f)
            known_errors = data.get('examples', [])
        
        matches = find_matching_errors(current_error, known_errors, threshold=threshold)
        
        if matches:
            print(f"Found {len(matches)} matching error(s) (threshold: {threshold}):\n")
            for i, (error_dict, score, components) in enumerate(matches, 1):
                print(f"Match #{i} (score: {score:.1f}):")
                print(f"  Job: {error_dict.get('job', 'N/A')}")
                print(f"  Test: {error_dict.get('test', 'N/A')}")
                error_preview = error_dict.get('error_message', '')[:150]
                print(f"  Error: {error_preview}...")
                print(f"  Components:")
                for comp, comp_score in components.items():
                    print(f"    {comp}: {comp_score:.1f}")
                print()
        else:
            print(f"No matching errors found (threshold: {threshold})")
    else:
        # Compare two error messages directly
        error1 = sys.argv[1]
        error2 = sys.argv[2]
        threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 70.0
        
        score, components = match_error_signatures(error1, error2)
        
        print(f"Similarity Score: {score:.1f}/100")
        print(f"Threshold: {threshold}")
        print(f"Match: {'YES' if score >= threshold else 'NO'}")
        print("\nComponent Scores:")
        for component, comp_score in components.items():
            print(f"  {component}: {comp_score:.1f}")
        
        print("\nExtracted Signatures:")
        sig1 = extract_key_signatures(error1)
        sig2 = extract_key_signatures(error2)
        print(f"\nError 1:")
        for key, value in sig1.items():
            print(f"  {key}: {value}")
        print(f"\nError 2:")
        for key, value in sig2.items():
            print(f"  {key}: {value}")


if __name__ == '__main__':
    main()
