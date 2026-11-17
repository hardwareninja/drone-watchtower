"""Resolve a newly seen serial once against the local aircraft list.

config.json ``aircraft`` is a display name for a serial the operator
already knows. Looking that name up is local: there is no network query.
The same serial is resolved once per contents of the list and then served
from memory, including a negative result so an unknown id is not scanned
again on every beacon.
"""
from __future__ import annotations

import threading

import textsan


class LocalRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[tuple, str | None]] = {}
        self._lookups = 0
        self._lock = threading.Lock()

    @property
    def lookups(self) -> int:
        return self._lookups

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()
            self._lookups = 0

    def resolve(self, ident, table: dict | None) -> str | None:
        """Return the display name for *ident*, or None if the list has none.

        *table* is the local aircraft map. A repeat call with the same
        ident and the same map does not scan the map again.
        """
        key = textsan.safe_label(ident)
        if not key:
            return None
        token = self._token(table)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == token:
                return hit[1]
            self._lookups += 1
            model = self._lookup(table, key)
            self._cache[key] = (token, model)
            return model

    @staticmethod
    def _token(table) -> tuple:
        if not isinstance(table, dict):
            return ()
        return tuple(sorted((str(k), str(v)) for k, v in table.items()))

    @staticmethod
    def _lookup(table, ident: str) -> str | None:
        if not isinstance(table, dict):
            return None
        direct = table.get(ident)
        if direct:
            text = textsan.safe_label(direct)
            return text or None
        want = ident.upper()
        for key, value in table.items():
            if textsan.safe_label(key).upper() == want:
                text = textsan.safe_label(value)
                return text or None
        return None


registry = LocalRegistry()
