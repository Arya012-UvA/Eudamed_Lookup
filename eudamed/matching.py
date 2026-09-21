"""Scoring a candidate device against the name you were looking for.

Design note - why evidence is typed
-----------------------------------
The predecessor script collapsed all evidence into one number, and pooled
trade-name tokens with manufacturer-name tokens when computing overlap. That
made a device score as a confident match purely because its *manufacturer* was
named after the product - e.g. searching "MindDoc" matched an unrelated device
called "Moodpath" at 0.90 ("found") because the manufacturer is "MindDoc Health
GmbH". The tool then reported a device that is not on the register as found.

Here every score carries the rule that produced it (``matched_on``), and
manufacturer-only evidence is hard-capped below the "possible" threshold. It
still surfaces as a candidate - it is a genuine lead - but it can never be
reported as a match on its own.
"""

import re
import unicodedata
from difflib import SequenceMatcher

FOUND = 0.85
POSSIBLE = 0.60

# Statuses. ERROR is distinct from NOT_FOUND on purpose: "the register does not
# list this device" and "the register could not be reached" are different
# answers, and conflating them reports a device as unregistered when in fact
# nothing was checked.
STATUS_FOUND = "found"
STATUS_POSSIBLE = "possible"
STATUS_NOT_FOUND = "not found"
STATUS_ERROR = "error"
ALL_STATUSES = (STATUS_FOUND, STATUS_POSSIBLE, STATUS_NOT_FOUND, STATUS_ERROR)

# Manufacturer-name evidence alone never reaches POSSIBLE, whatever the
# country bonus. This invariant is covered by a test.
MANUFACTURER_CAP = 0.55
FUZZY_CAP = 0.84       # a fuzzy trade-name match alone never reaches FOUND

COUNTRY_MATCH_BONUS = 0.05
COUNTRY_CONFLICT_PENALTY = 0.10

# /udi parameters that identify a device rather than name it, mapped to the
# Device attribute they filter on. A hit found through one of these is a match
# by construction - the API already matched the identifier - so it must not be
# re-scored for name similarity, which would score near zero and discard it.
IDENTIFIER_PARAMS = {
    "PRIMARY_DI": "primary_di",
    "BASIC_UDI": "basic_udi",
    "MF_SRN": "mf_srn",
    "REFERENCE": "reference",
    "NOMENCLATURE_CODE": "nomenclature_code",
}


def norm(value):
    """Lowercase, strip accents, collapse punctuation to single spaces."""
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def squash(value):
    return norm(value).replace(" ", "")


def _name_score(key, name):
    """Trade/device-name comparison. Returns (score, rule) or None."""
    nk, sk = norm(key), squash(key)
    nn, sn = norm(name), squash(name)
    if not sk or not sn:
        return None
    if sn == sk:
        return 1.0, "exact"
    if sk in sn:
        return 0.92, "contains"
    if len(sn) >= 4 and sn in sk:
        return 0.80, "contained_by"
    ratio = SequenceMatcher(None, sk, sn).ratio()
    tokens_key, tokens_name = set(nk.split()), set(nn.split())
    overlap = len(tokens_key & tokens_name) / len(tokens_key) if tokens_key else 0.0
    return min(FUZZY_CAP, 0.85 * max(ratio, overlap)), "fuzzy"


def score_device(keys, expected_country, device):
    """Score a Device against search keys.

    Returns (score, matched_on). ``matched_on`` is one of:
    ``trade_name:<rule>``, ``device_name:<rule>``, ``manufacturer``, ``none``.
    """
    best, matched_on = 0.0, "none"

    for key in keys or ():
        if not squash(key):
            continue

        trade = _name_score(key, device.trade_name)
        if trade and trade[0] > best:
            best, matched_on = trade[0], f"trade_name:{trade[1]}"

        # A device name match is real evidence but weaker than the trade name.
        dev = _name_score(key, device.device_name)
        if dev:
            scaled = dev[0] * 0.9
            if scaled > best:
                best, matched_on = scaled, f"device_name:{dev[1]}"

        # Manufacturer evidence is tracked separately and capped.
        mfr = _name_score(key, device.manufacturer_name)
        if mfr and mfr[0] >= 0.80:
            capped = min(MANUFACTURER_CAP, mfr[0])
            if capped > best:
                best, matched_on = capped, "manufacturer"

    if best <= 0.0:
        return 0.0, "none"

    country = (expected_country or "").upper()
    actual = device.country
    if country and actual:
        best += COUNTRY_MATCH_BONUS if actual == country else -COUNTRY_CONFLICT_PENALTY

    ceiling = MANUFACTURER_CAP if matched_on == "manufacturer" else 1.0
    return round(max(0.0, min(ceiling, best)), 3), matched_on


def score_identifier(param, term, device):
    """Score a hit that was found by querying an identifier field.

    Returns (score, matched_on) or None when the identifier does not actually
    appear in the row - which would mean the API matched on something else.
    """
    attr = IDENTIFIER_PARAMS.get(param)
    if not attr:
        return None
    actual, wanted = squash(getattr(device, attr, "")), squash(term)
    if not actual or not wanted:
        return None
    if actual == wanted:
        return 1.0, f"identifier:{param}"
    if wanted in actual or actual in wanted:
        return 0.95, f"identifier:{param}"
    return None


def classify(score):
    if score >= FOUND:
        return STATUS_FOUND
    if score >= POSSIBLE:
        return STATUS_POSSIBLE
    return STATUS_NOT_FOUND
