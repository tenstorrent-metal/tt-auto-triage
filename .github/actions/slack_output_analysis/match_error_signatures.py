#!/usr/bin/env python3
"""
Minimal error matching using RapidFuzz and Semantic Similarity.
Paste errors as strings for testing.
"""

from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# Paste your errors here
error1 = """
The issue 'self-hosted runner lost communication with the server' is known to be non-deterministic and affects many jobs
"""

error2 = """
The self-hosted runner lost communication with the server before any P150 BH tests could execute
"""

# RapidFuzz score
rapidfuzz_score = fuzz.token_set_ratio(error1, error2)

# Semantic similarity using sentence transformers (entailment-like)
# Using a model trained for semantic similarity/entailment
model = SentenceTransformer('all-MiniLM-L6-v2')  # Fast, good for similarity
embeddings = model.encode([error1, error2])
semantic_sim = cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]
semantic_score = semantic_sim * 100

print("=" * 60)
print("RAPIDFUZZ:")
print("=" * 60)
print(f"  token_set_ratio: {rapidfuzz_score}/100")

print("\n" + "=" * 60)
print("SEMANTIC SIMILARITY (Sentence Transformers):")
print("=" * 60)
print(f"  semantic_similarity: {semantic_score:.1f}/100")

print("\n" + "=" * 60)
print("SUMMARY:")
print("=" * 60)
print(f"  RapidFuzz: {rapidfuzz_score}/100")
print(f"  Semantic (Embeddings): {semantic_score:.1f}/100")
print(f"\n  Match (threshold 70): {'YES' if semantic_score >= 60 and rapidfuzz_score >= 50 else 'NO'}")
