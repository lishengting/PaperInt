"""Load CNSP journal lists from config."""

from __future__ import annotations

import json
import os

# Journals that always get searched regardless of keywords (broad-scope)
_GENERAL_JOURNALS: set[str] = {
    'nature', 'science', 'cell', 'nature communications',
    'science advances', 'cell reports', 'iscience', 'plos one',
}


def _keyword_stems(keywords: list[str]) -> set[str]:
    """Generate word stems from keywords for journal name matching."""
    stems = set()
    for kw in keywords:
        kw = kw.lower().strip()
        stems.add(kw)
        # Strip common suffixes to get root stems
        for suffix in ('ics', 'ic', 'ology', 'omics', 'esis', 'sis', 'ism',
                         'ome', 'ome', 'gen', 's', 'a', 'es', 'ia', 'us'):
            if kw.endswith(suffix) and len(kw) - len(suffix) >= 3:
                stems.add(kw[:-len(suffix)])
    return stems


def _word_match(journal_word: str, stem: str) -> bool:
    """Check if a journal word matches a keyword stem."""
    if len(journal_word) < 3 or len(stem) < 3:
        return False
    if journal_word.startswith(stem) or stem.startswith(journal_word):
        return True
    # Check shared prefix (e.g. "microbiology" vs "microbiome" → common "microbi")
    min_len = min(len(journal_word), len(stem))
    for i in range(min_len, 3, -1):
        if journal_word[:i] == stem[:i]:
            return True
    return False


def filter_journals_by_keywords(journals: list[dict],
                                 keywords: list[str]) -> list[dict]:
    """Filter journals to those semantically relevant to keywords.

    Always includes broad-scope journals (Nature, Science, Cell, etc.).
    For others, matches keyword stems against journal name words.
    """
    if not keywords:
        return journals

    stems = _keyword_stems(keywords)
    filtered = []
    for j in journals:
        name = j.get('name', '').lower()
        # Always include general broad-scope journals
        if name in _GENERAL_JOURNALS:
            filtered.append(j)
            continue
        # Split journal name into words
        name_words = set(name.replace('-', ' ').replace('&', ' ').split())
        # Check if any journal word starts with or is contained in any keyword stem
        match = False
        for word in name_words:
            for stem in stems:
                if _word_match(word, stem):
                    match = True
                    break
            if match:
                break
        if match:
            filtered.append(j)
    return filtered


def load_journals(config: dict, journal_type: str) -> list[dict]:
    """Load journal list from JSON file specified in config.yaml cnsp section.

    Returns list of {name, link} dicts.
    """
    cnsp_cfg = config.get('cnsp', {})
    key_map = {
        'nature': 'nature_journals',
        'science': 'science_journals',
        'cell': 'cell_journals',
        'plos': 'plos_journals',
    }
    config_key = key_map.get(journal_type)
    if not config_key:
        return []

    json_path = cnsp_cfg.get(config_key, '')
    if not json_path:
        return []

    # Resolve relative to project root (where config.yaml lives)
    if not os.path.isabs(json_path):
        json_path = os.path.join(os.getcwd(), json_path)

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            journals = json.load(f)
        if isinstance(journals, list):
            return journals
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return []


def get_enabled_journals(config: dict, journal_type: str,
                         cnsp_journals_filter: list[str] | None = None) -> list[dict]:
    """Return journal list filtered by user request and config.

    If cnsp_journals_filter is provided, only return journals whose name
    appears in the filter (case-insensitive match).
    """
    cnsp_cfg = config.get('cnsp', {})
    per_parser = cnsp_cfg.get(journal_type, {})
    if not per_parser.get('enabled', True):
        return []

    journals = load_journals(config, journal_type)

    # Apply config-level include filter
    include = cnsp_cfg.get('include_journals', [])
    if include:
        include_lower = {j.lower() for j in include}
        journals = [j for j in journals if j.get('name', '').lower() in include_lower]

    # Filter out disabled journals (enabled: false in JSON)
    journals = [j for j in journals if j.get('enabled', True)]

    # Apply CLI-level filter
    if cnsp_journals_filter:
        filter_lower = {j.lower() for j in cnsp_journals_filter}
        journals = [j for j in journals if j.get('name', '').lower() in filter_lower]

    return journals


def get_request_delay(config: dict, journal_type: str) -> float:
    """Get configured request delay for a journal type."""
    cnsp_cfg = config.get('cnsp', {})
    per_parser = cnsp_cfg.get(journal_type, {})
    return float(per_parser.get('request_delay_seconds', 2))