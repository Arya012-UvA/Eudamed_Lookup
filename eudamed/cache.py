"""A local row cache, because the API cannot be searched approximately.

Established against the live API:

* ``/udi`` filters are **exact match, case-insensitive**. ``TRADE_NAME=Mind``
  returns the device literally called ``MIND``; it is not a prefix or substring
  match. No ``*`` or ``%`` wildcards work.
* There is **no pagination**. ``$top``, ``$skip`` and ``$count`` are all
  rejected with "Invalid Query Parameter", and a request returns at most 1000
  rows.
* ``$filter`` is recognised but unusable: the gateway appends ``,1 eq 1`` to the
  value, which is invalid OData, so every expression fails to parse.

The consequence is that a device cannot be found unless its exact registered
trade name is already known. Fuzzy matching only helps once rows are held
locally, so rows are fetched in partitions and cached as JSON Lines, and the
matching runs over the cache.
"""

import json
import os

from .fields import index_row
from .records import Device


def identity(row):
    i = index_row(row)
    for key in ("uuid", "primarydi", "basicudi", "id"):
        value = i.get(key)
        if value not in (None, "", []):
            return str(value)
    return json.dumps(row, sort_keys=True, default=str)[:200]


class RowCache:
    """De-duplicating store of raw /udi rows, persisted as JSON Lines."""

    def __init__(self):
        self.rows = {}
        self.partitions = []        # {"filter": ..., "rows": n, "truncated": bool}

    def add(self, rows, source=None, truncated=False):
        added = 0
        for row in rows:
            key = identity(row)
            if key not in self.rows:
                self.rows[key] = row
                added += 1
        self.partitions.append({
            "filter": source or {}, "rows": len(rows), "new": added,
            "truncated": truncated,
        })
        return added

    def __len__(self):
        return len(self.rows)

    @property
    def truncated_partitions(self):
        return [p for p in self.partitions if p["truncated"]]

    def devices(self, reference=None):
        for row in self.rows.values():
            device = Device(row)
            if reference is not None:
                reference.enrich(device)
            yield device

    # -- persistence ---------------------------------------------------
    def save(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for row in self.rows.values():
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        meta = os.path.splitext(path)[0] + ".meta.json"
        with open(meta, "w", encoding="utf-8") as handle:
            json.dump({"rows": len(self.rows), "partitions": self.partitions},
                      handle, indent=2, default=str)
        return path, meta

    @classmethod
    def load(cls, path):
        cache = cls()
        with open(path, encoding="utf-8") as handle:
            rows = []
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path} line {lineno} is not JSON: {exc}") from exc
        cache.add(rows, source={"loaded": path})
        meta = os.path.splitext(path)[0] + ".meta.json"
        if os.path.exists(meta):
            try:
                with open(meta, encoding="utf-8") as handle:
                    cache.partitions = json.load(handle).get("partitions", cache.partitions)
            except (OSError, json.JSONDecodeError):
                pass
        return cache
