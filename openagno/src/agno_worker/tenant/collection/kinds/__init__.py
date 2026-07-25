"""Collection protocol kinds.

Add a new kind by creating ``kinds/<name>/`` with at least ``kind.py`` that
registers an ``extract_config`` via ``registry.register_config_extractor``,
then re-export public APIs from that package as needed.
"""
