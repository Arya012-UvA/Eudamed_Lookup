"""Resolve the numeric *_ID columns on /udi rows via the /reference operation.

/udi filters and (presumably) returns RISK_CLASS_ID, APPLICABLE_LEGISLATION_ID,
PLACED_ON_THE_MARKET_ID and SPECIAL_DEVICE_TYPE_ID as numbers. /reference maps
ID -> CODE, so one fetch can turn those into human-readable labels.

Important caveat, straight from the spec: /reference exposes only ID, CODE and
LANGUAGE. There is no column identifying which code *table* a row belongs to.
So if risk-class id 1 and legislation id 1 are different things - which is
likely, since they are separate enumerations - a flat ID -> CODE map is
ambiguous and would silently mislabel fields.

This resolver therefore refuses to guess: an ID that maps to more than one
distinct CODE is treated as ambiguous and left unresolved, and the ambiguity is
reported. A confidently wrong risk class is worse than a visible numeric id.
A failed lookup is likewise never fatal.
"""

from .client import ApiError
from .fields import index_row, pick


class Reference:
    def __init__(self, client=None, language="en", verbose=False):
        self.client = client
        self.language = language
        self.verbose = verbose
        self._by_id = {}
        self.ambiguous = {}
        self.loaded = False
        self.error = ""

    def load(self):
        if self.loaded or self.client is None:
            return self
        try:
            rows, _ = self.client.reference(LANGUAGE=self.language)
        except (ApiError, ValueError) as exc:
            self.error = str(exc)
            self.loaded = True
            if self.verbose:
                print(f"  reference lookup unavailable: {exc}", flush=True)
            return self
        collected = {}
        for row in rows:
            i = index_row(row)
            rid = pick(i, "ID", "id", default=None)
            codev = pick(i, "CODE", "code")
            if rid is None or not codev:
                continue
            try:
                key = int(rid)
            except (TypeError, ValueError):
                key = str(rid)
            collected.setdefault(key, set()).add(str(codev))

        for key, codes in collected.items():
            if len(codes) == 1:
                self._by_id[key] = next(iter(codes))
            else:
                self.ambiguous[key] = sorted(codes)

        self.loaded = True
        if self.verbose:
            print(f"  reference: {len(self._by_id)} unambiguous code(s)", flush=True)
        if self.ambiguous:
            example = next(iter(self.ambiguous.items()))
            print(f"  warning: {len(self.ambiguous)} reference id(s) map to several codes "
                  f"and are left unresolved (e.g. {example}). /reference has no column "
                  f"identifying which code table an id belongs to.", flush=True)
        return self

    def label(self, value):
        """Human code for a numeric id, or the id itself when unresolvable.

        Ambiguous ids (several codes share the id) resolve to the raw id, never
        to an arbitrary pick among the candidates.
        """
        if value is None or value == "":
            return ""
        try:
            hit = self._by_id.get(int(value))
        except (TypeError, ValueError):
            hit = self._by_id.get(str(value))
        return hit or str(value)

    def enrich(self, device):
        """Fill blank label fields from their numeric ids, in place."""
        pairs = (("risk_class", "risk_class_id"), ("legislation", "legislation_id"),
                 ("market_status", "market_status_id"), ("special_type", "special_type_id"))
        for label_attr, id_attr in pairs:
            if not getattr(device, label_attr):
                setattr(device, label_attr, self.label(getattr(device, id_attr)))
        return device
