"""Caller-declared dataset epoch for the process-local repeat-result cache.

The cache has no server-side invalidation protocol.  It is correct only when
the caller guarantees that the database content relevant to the cached SQL is
unchanged for the whole lifetime of the epoch.  This module contains only the
shared validation/normalization rule; it never contacts IoTDB.
"""

from __future__ import annotations

MAX_CACHE_EPOCH_LENGTH = 200


def normalize_cache_epoch(value: str | None) -> str | None:
    """Return a normalized epoch label, or ``None`` when caching is disabled.

    The value is a caller-owned identity, not a timestamp or a server version.
    Leading/trailing whitespace is ignored so the same epoch cannot silently
    create two cache namespaces; embedded whitespace is allowed.  Control
    characters are rejected because they would make logs and persisted audit
    records ambiguous.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("cache epoch must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("cache epoch must not be blank")
    if len(cleaned) > MAX_CACHE_EPOCH_LENGTH:
        raise ValueError(
            f"cache epoch must be at most {MAX_CACHE_EPOCH_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("cache epoch must not contain control characters")
    return cleaned


__all__ = ["MAX_CACHE_EPOCH_LENGTH", "normalize_cache_epoch"]
