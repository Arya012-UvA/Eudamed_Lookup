"""Deciding whether a registry row describes software.

Why this exists
---------------
A name search is a blunt instrument. "coach", "mind" and "sleep" occur in the
trade names of mattresses, braces and monitors as readily as in those of
therapy apps, so a substring sweep for psychological software returns mostly
hardware. Filtering on what the *record* says the device is removes that noise
regardless of the search term.

Two fields carry that statement, and both are already fetched on every row:

``SPECIAL_DEVICE_TYPE``
    An explicit flag. When it names software, the question is settled. It is
    usually **not** set for software, though: the DiGA-style rows seen so far
    carry ``None`` here, because in EUDAMED this field marks a handful of
    special cases (system, procedure pack, and so on) rather than classifying
    every device. So it confirms software and never rules it out.

``NOMENCLATURE_CODE`` (EMDN)
    EMDN category **Z12** is medical device software, and its subcategories
    (``Z1201…``, ``Z1203…``) are where therapy apps sit. This is the signal
    that actually fires in practice.

Three-way, not two
------------------
An empty field is not evidence. A row with no EMDN code and no special type
says nothing about what it is, and calling that "not software" would drop
real devices; calling it software would keep the noise the filter exists to
remove. So classification is three-valued - ``software`` / ``other`` /
``unknown`` - and a caller filtering on it is told how many rows fell into
each bucket, so a sparsely populated field shows up as a large ``unknown``
count instead of silently shrinking the result.
"""

SOFTWARE = "software"
OTHER = "other"
UNKNOWN = "unknown"

#: EMDN prefixes that denote software. Z12 is the software category; matching
#: on the prefix covers every subcategory beneath it.
SOFTWARE_EMDN_PREFIXES = ("Z12",)

#: Substrings that, in a resolved SPECIAL_DEVICE_TYPE label, mean software.
SOFTWARE_TYPE_WORDS = ("software", "mdsw")

#: Special device types that are positive evidence of something physical.
#: "None"/"Not applicable" is deliberately absent: it is the normal value for
#: software and must not be read as a denial.
PHYSICAL_TYPE_WORDS = ("system", "procedure pack", "kit", "part", "component",
                       "accessory", "implant")


def _emdn(code):
    """Normalise an EMDN code for prefix comparison.

    Real codes arrive with spaces and inconsistent case ("Z12 01 02",
    "z1201"), so strip both before testing the prefix.
    """
    return "".join(str(code or "").split()).upper()


def classify(device):
    """Return ``(kind, reason)`` from a Device's own row.

    ``reason`` names the field and value the decision rests on, so a filtered
    report can show why a row was kept or dropped rather than asserting it.
    """
    return classify_values(getattr(device, "special_type", ""),
                           getattr(device, "nomenclature_code", ""))


def classify_values(special_type, nomenclature_code):
    """The decision itself, on two field values."""
    special = str(special_type or "").strip()
    low = special.lower()
    if any(word in low for word in SOFTWARE_TYPE_WORDS):
        return SOFTWARE, f"special device type is {special!r}"

    code = _emdn(nomenclature_code)
    if code:
        for prefix in SOFTWARE_EMDN_PREFIXES:
            if code.startswith(prefix):
                return SOFTWARE, f"EMDN {code} is in the software category {prefix}"
        return OTHER, f"EMDN {code} is outside the software category"

    if any(word in low for word in PHYSICAL_TYPE_WORDS):
        return OTHER, f"special device type is {special!r}"

    return UNKNOWN, "no EMDN code and no special device type on the record"


def tally(entries):
    """Count device kinds across a list of result entries."""
    counts = {SOFTWARE: 0, OTHER: 0, UNKNOWN: 0}
    for entry in entries:
        kind = entry.get("device_kind") if isinstance(entry, dict) else None
        if kind in counts:
            counts[kind] += 1
    return counts


# The web-UI backend's list rows carry no nomenclature code at all - it lives
# on the per-device detail record, under the CND nomenclature the EMDN derives
# from. So "keep only software" needs one detail request per undetermined
# device, which is what Typer does.
DETAIL_CODE_KEYS = ("cndNomenclatures", "nomenclatures", "emdnCodes")


def detail_values(detail):
    """(special_type, nomenclature_code) from a UI detail record.

    Returns empty strings for anything it cannot find, so an unexpected shape
    leaves the device undetermined instead of misclassifying it.
    """
    if not isinstance(detail, dict):
        return "", ""
    from .fields import index_row, pick
    from .records import coded

    i = index_row(detail)
    special = coded(pick(i, "SPECIAL_DEVICE_TYPE", "specialDeviceType"))
    code = str(pick(i, "NOMENCLATURE_CODE", "nomenclatureCode", "emdnCode") or "")
    if code:
        return special, code

    for key in DETAIL_CODE_KEYS:
        entries = pick(i, key, default=None)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                found = entry.get("code") or entry.get("CODE") or ""
            else:
                found = entry
            if found:
                return special, str(found)
    return special, ""


class Typer:
    """Classifies devices, consulting the detail record when the row is silent.

    ``detail_client`` is any client exposing ``device_detail(uuid)`` - in
    practice ``UiClient``. Without one this is exactly ``classify``. Detail
    responses are cached per uuid so the same device is never fetched twice in
    a run, and a failed fetch leaves the device ``unknown`` rather than
    aborting the search.
    """

    def __init__(self, detail_client=None, verbose=False):
        self.detail_client = detail_client
        self.verbose = verbose
        self._cache = {}
        self.detail_requests = 0
        self.detail_errors = 0

    def classify(self, device):
        kind, reason = classify(device)
        if kind != UNKNOWN:
            return kind, reason
        uuid = getattr(device, "uuid", "")
        if not uuid or not hasattr(self.detail_client, "device_detail"):
            return kind, reason

        if uuid not in self._cache:
            from .client import ApiError
            self.detail_requests += 1
            try:
                self._cache[uuid] = self.detail_client.device_detail(uuid)
            except (ApiError, ValueError) as exc:
                self.detail_errors += 1
                self._cache[uuid] = None
                if self.verbose:
                    print(f"  detail lookup failed for {uuid}: {exc}", flush=True)
        detail = self._cache[uuid]
        if not detail:
            return UNKNOWN, "the record is silent and its detail record could not be read"

        special, code = detail_values(detail)
        if not (special or code):
            return UNKNOWN, "neither the row nor the detail record names a device type"
        kind, reason = classify_values(special, code)
        return kind, f"{reason} (from the detail record)"
