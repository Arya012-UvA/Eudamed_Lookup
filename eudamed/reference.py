"""Resolve the numeric *_ID columns on /udi rows via the /reference operation.

Verified response shape (live probe, 294 rows, LANGUAGE=en):

    {"ID": -101.0, "CODE": "PLACED_ON_THE_MARKET_ID", "LANGUAGE": "en",
     "VALUE": "Israel"}

So the table is keyed by ``(CODE, ID)`` and ``VALUE`` holds the label. ``CODE``
names the code table, and the names line up with the numeric /udi query
parameters (RISK_CLASS_ID, APPLICABLE_LEGISLATION_ID, PLACED_ON_THE_MARKET_ID,
SPECIAL_DEVICE_TYPE_ID), which is what makes a device row resolvable.

IDs arrive as JSON numbers and may be negative or non-integral, so they are
normalised before use.

A failed or unrecognised lookup is never fatal: the numeric id is reported
instead of a label.
"""

from .client import ApiError
from .fields import index_row, pick

# Device attribute -> the /reference CODE table that explains it.
CODE_TABLES = {
    "risk_class_id": "RISK_CLASS_ID",
    "legislation_id": "APPLICABLE_LEGISLATION_ID",
    "market_status_id": "PLACED_ON_THE_MARKET_ID",
    "special_type_id": "SPECIAL_DEVICE_TYPE_ID",
}

# Device attribute holding the label -> attribute holding the numeric id.
LABEL_FOR = {
    "risk_class": "risk_class_id",
    "legislation": "legislation_id",
    "market_status": "market_status_id",
    "special_type": "special_type_id",
}


def normalise_id(value):
    """JSON numbers come through as floats; -101.0 and -101 must agree."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return int(number) if number.is_integer() else number


class Reference:
    def __init__(self, client=None, language="en", verbose=False):
        self.client = client
        self.language = language
        self.verbose = verbose
        self._values = {}        # (CODE, normalised ID) -> VALUE
        self.tables = {}         # CODE -> number of entries
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

        for row in rows:
            i = index_row(row)
            table = str(pick(i, "CODE", "code")).strip()
            rid = normalise_id(pick(i, "ID", "id", default=None))
            label = pick(i, "VALUE", "value")
            if not table or rid is None or not label:
                continue
            self._values[(table, rid)] = str(label)
            self.tables[table] = self.tables.get(table, 0) + 1

        self.loaded = True
        if self.verbose:
            print(f"  reference: {len(self._values)} value(s) across "
                  f"{len(self.tables)} table(s)", flush=True)
        return self

    def label(self, table, value_id):
        """Human label for an id within a code table.

        Falls back to the id itself, so an unknown table or id shows the raw
        number rather than a wrong label or a blank.
        """
        rid = normalise_id(value_id)
        if rid is None:
            return ""
        hit = self._values.get((table, rid))
        return hit if hit else str(rid)

    def enrich(self, device):
        """Fill blank label fields from their numeric ids, in place."""
        for label_attr, id_attr in LABEL_FOR.items():
            if getattr(device, label_attr, ""):
                continue
            table = CODE_TABLES[id_attr]
            setattr(device, label_attr, self.label(table, getattr(device, id_attr, None)))
        return device
