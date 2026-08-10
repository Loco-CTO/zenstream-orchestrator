"""Shared normalization, trigram indexing, and relevance scoring helpers."""

from __future__ import annotations

import unicodedata


def normalize_search_text(value: object) -> str:
    """Normalize search text consistently across indexes and ranking."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


def trigram_set(value: object) -> set[str]:
    """Return unique boundary-padded trigrams for a normalized value."""
    normalized = normalize_search_text(value)
    if not normalized:
        return set()
    padded = f"  {normalized} "
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def search_grams(value: object) -> set[str]:
    """Return short-search grams plus padded trigrams for an indexed title."""
    normalized = normalize_search_text(value)
    if not normalized:
        return set()
    grams = {
        normalized[index : index + size]
        for size in (1, 2)
        for index in range(max(0, len(normalized) - size + 1))
    }
    grams.update(trigram_set(normalized))
    return grams


def _dice(left: str, right: str) -> float:
    left_grams = trigram_set(left)
    right_grams = trigram_set(right)
    if not left_grams or not right_grams:
        return 0.0
    return (2.0 * len(left_grams & right_grams)) / (len(left_grams) + len(right_grams))


def _candidate_windows(candidate: str, query_word_count: int):
    """Yield the complete title and short contiguous word windows."""
    yield candidate
    words = candidate.split()
    if not words:
        return
    maximum = min(len(words), max(1, query_word_count + 2))
    for width in range(1, maximum + 1):
        for start in range(len(words) - width + 1):
            yield " ".join(words[start : start + width])


def match_score(query: object, candidate: object) -> float:
    """Return a sortable score where exact/prefix/substring matches dominate."""
    wanted = normalize_search_text(query)
    value = normalize_search_text(candidate)
    if not wanted or not value:
        return 0.0
    if wanted == value:
        return 1.0
    if value.startswith(wanted):
        return 0.99
    if wanted in value:
        return 0.98

    query_word_count = len(wanted.split())
    fuzzy = max(
        (
            _dice(wanted, window)
            for window in _candidate_windows(value, query_word_count)
        ),
        default=0.0,
    )
    # Keep fuzzy matches below substring matches while preserving their relative order.
    return min(0.97, fuzzy * 0.97)


def register_sqlite_functions(dbapi_connection) -> None:
    """Register search functions on every SQLite connection."""
    dbapi_connection.create_function(
        "catalog_match_score", 2, match_score, deterministic=True
    )
