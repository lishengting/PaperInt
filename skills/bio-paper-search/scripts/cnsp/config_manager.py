"""Load CNSP journal lists from config."""

from __future__ import annotations

import json
import os


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