"""Shape-tolerant access to API response rows.

The OpenAPI document declares no response schemas: every 200 is
``{"description": ""}`` with no content. So the field names in a response are
unknown until a live call is made, and may differ between the JSON and CSV
representations of the same operation.

Rather than guess one spelling, every lookup here is done on a *squashed* key
(lowercased, non-alphanumerics stripped), so ``TRADE_NAME``, ``tradeName``,
``trade_name`` and ``Trade Name`` all resolve to the same field. Unknown keys
are preserved on the record so ``probe`` can report them.
"""

import re

_SQUASH = re.compile(r"[^a-z0-9]+")


def squash_key(key):
    return _SQUASH.sub("", str(key).lower())


def index_row(row):
    """Map a raw response row to {squashed_key: value}.

    Later duplicates do not clobber an earlier non-empty value, so a row
    carrying both ``TRADE_NAME`` and ``tradeName`` keeps whichever is populated.
    """
    out = {}
    for key, value in (row or {}).items():
        sq = squash_key(key)
        if sq not in out or _empty(out[sq]):
            out[sq] = value
    return out


def _empty(value):
    return value is None or value == "" or value == [] or value == {}


def pick(indexed, *names, default=""):
    """First populated value among candidate spellings of a field name."""
    for name in names:
        value = indexed.get(squash_key(name))
        if not _empty(value):
            return value
    return default


def unwrap_rows(payload):
    """Extract a list of row dicts from a response body of unknown shape.

    Handles a bare list, a list under a common wrapper key, and a single
    object. Returns [] for anything else rather than raising, so a surprising
    shape degrades to "no results" plus a warning instead of a crash.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("content", "items", "data", "results", "records", "rows",
                    "value", "elements", "udiDiData", "actors"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
        # A dict of scalars is most likely a single record.
        if any(not isinstance(v, (dict, list)) for v in payload.values()):
            return [payload]
    return []


def describe_keys(rows):
    """Sorted original key names seen across rows, for the probe command."""
    seen = {}
    for row in rows:
        for key in (row or {}):
            seen.setdefault(squash_key(key), key)
    return sorted(seen.values())
