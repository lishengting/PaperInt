"""Title normalization and lightweight matching helpers."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


STOPWORDS = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"}
PUBLICATION_PREFIXES = (
    "author correction",
    "correction",
    "erratum",
    "corrigendum",
    "retraction",
)


def normalize_title(title: str | None) -> str:
    """Normalize title text for candidate comparison."""

    if not title:
        return ""
    text = title.lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [word for word in text.split() if word not in STOPWORDS]
    return " ".join(words)


def title_similarity(query: str, candidate: str | None) -> float:
    """Return a conservative similarity score for two titles."""

    left = normalize_title(query)
    right = normalize_title(candidate)
    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    left_words = set(left.split())
    right_words = set(right.split())
    shared_words = left_words & right_words
    jaccard = len(shared_words) / max(len(left_words | right_words), 1)
    return max(ratio, jaccard)


def titles_match(query: str, candidate: str | None, threshold: float = 0.82) -> bool:
    """Return whether a title result is close enough to trust."""

    if _has_unrequested_publication_prefix(query, candidate):
        return False
    if _candidate_title_in_citation_query(query, candidate):
        return True
    return title_similarity(query, candidate) >= threshold


def _candidate_title_in_citation_query(query: str, candidate: str | None) -> bool:
    left = normalize_title(query)
    right = normalize_title(candidate)
    if not left or not right:
        return False
    left_words = set(left.split())
    right_words = set(right.split())
    if not right_words <= left_words:
        return False
    if len(left_words - right_words) < 3:
        return False
    return bool(re.search(r"\b(?:19|20)\d{2}\b", left))


def _has_unrequested_publication_prefix(query: str, candidate: str | None) -> bool:
    if not candidate:
        return False
    query_text = (query or "").strip().lower()
    candidate_text = candidate.strip().lower()
    for prefix in PUBLICATION_PREFIXES:
        marker = f"{prefix}:"
        if candidate_text.startswith(marker) and not query_text.startswith(marker):
            return True
    return False
