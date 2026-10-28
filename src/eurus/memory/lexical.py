"""Fuzzy lexical matching over hashed n-gram feature vectors.

This module implements deterministic feature hashing of word unigrams,
bigrams, and character 3/4-grams into a fixed-width vector, plus cosine
similarity. It is fuzzy lexical matching, not a learned semantic embedding:
semantically unrelated texts that share words or character runs score high,
and there is no embedding model, training data, or notion of meaning behind
the vectors. Use it for cheap recall-side fuzzy matching only.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

LEXICAL_VECTOR_DIMENSIONS = 512
_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


def _stable_hash(token: str) -> tuple[int, int]:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % LEXICAL_VECTOR_DIMENSIONS
    sign = 1 if digest[4] & 1 else -1
    return bucket, sign


def tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def lexical_vector(text: str) -> tuple[float, ...]:
    tokens = tokenize(text)
    vector = [0.0] * LEXICAL_VECTOR_DIMENSIONS
    features: list[str] = list(tokens)
    features.extend(f"{left}_{right}" for left, right in zip(tokens, tokens[1:], strict=False))
    for token in tokens:
        if len(token) <= 3:
            continue
        padded = f"^{token}$"
        features.extend(f"#{padded[index : index + 3]}" for index in range(len(padded) - 2))
        features.extend(f"#{padded[index : index + 4]}" for index in range(len(padded) - 3))
    if not features:
        return tuple(vector)
    for feature in features:
        bucket, sign = _stable_hash(feature)
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must share dimensions")
    total = sum(a * b for a, b in zip(left, right, strict=True))
    return max(-1.0, min(1.0, total))


def lexical_similarity(left: str, right: str) -> float:
    return cosine_similarity(lexical_vector(left), lexical_vector(right))
