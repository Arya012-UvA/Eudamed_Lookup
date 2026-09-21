"""Orchestration: device list in, ranked candidates out."""

import csv

from .client import ApiError, AuthError
from .fields import describe_keys
from .matching import STATUS_ERROR, STATUS_NOT_FOUND, classify, norm, score_device, score_identifier
from .records import Device


class Target:
    """One device you are looking for."""

    def __init__(self, name, description="", ca="", country="", keys=None, broad=None):
        self.name = name
        self.description = description
        self.ca = ca
        self.country = (country or "").upper()
        self.keys = list(keys) if keys else [name]
        self.broad = list(broad) if broad else []

    def to_dict(self):
        return {"name": self.name, "description": self.description, "ca": self.ca,
                "country": self.country, "keys": self.keys, "broad": self.broad}


def load_targets(path):
    """Read targets from CSV. Only `name` is required."""
    targets = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} is empty")
        cols = {(c or "").strip().lower() for c in reader.fieldnames}
        if "name" not in cols:
            raise ValueError(
                f"{path} has no 'name' column (found: {', '.join(reader.fieldnames)})")
        for row in reader:
            clean = {(k or "").strip().lower(): (v or "").strip()
                     for k, v in row.items() if k}
            name = clean.get("name", "")
            if not name:
                continue
            targets.append(Target(
                name=name,
                description=clean.get("description", ""),
                ca=clean.get("ca", ""),
                country=clean.get("country", ""),
                keys=_split(clean.get("keys")) or [name],
                broad=_split(clean.get("broad")),
            ))
    if not targets:
        raise ValueError(f"{path} contained no usable rows")
    return targets


def _split(value):
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def search_target(client, target, reference=None, top=5, min_score=0.45,
                  fields="TRADE_NAME", keep_raw=False, widen_client=None):
    """Run every query term for one target and rank the union of the results.

    ``widen_client`` is an optional substring-capable client (see
    ``eudamed.ui_backend.UiClient``). The documented API matches names
    exactly, so a device whose trade name *and* device name both differ from
    every supplied term cannot be found at all. When the primary pass comes
    back empty, that client is asked the same question with substring
    matching, and anything it returns is tagged ``matched_via`` so a fallback
    hit is never mistaken for a confirmed exact match.
    """
    seen, found_via, queries, errors, raw_keys = {}, {}, [], [], set()
    param_names = [f.strip().upper() for f in fields.split(",") if f.strip()]

    for term in target.keys + target.broad:
        for param in param_names:
            record = {"term": term, "param": param, "rows": None, "error": None}
            try:
                rows, _ = client.udi(**{param: term})
            except AuthError:
                raise
            except (ApiError, ValueError) as exc:
                record["error"] = str(exc)
                errors.append(f"{param}={term!r}: {exc}")
                queries.append(record)
                continue
            record["rows"] = len(rows)
            raw_keys.update(describe_keys(rows))
            queries.append(record)
            for row in rows:
                device = Device(row)
                if reference is not None:
                    reference.enrich(device)
                key = device.identity()
                if key not in seen:
                    seen[key] = device
                # Remember how each device was reached; an identifier hit is a
                # match by construction and must not be name-scored away.
                found_via.setdefault(key, []).append((param, term))

    key_terms = set(target.keys)
    candidates = []
    for key, device in seen.items():
        value, matched_on = score_device(target.keys, target.country, device)
        # Only terms from `keys` may claim an identifier match. A `broad` term
        # is a recall helper, so an SRN listed there must not turn every device
        # from that manufacturer into a full-confidence hit.
        for param, term in found_via.get(key, ()):
            if term not in key_terms:
                continue
            hit = score_identifier(param, term, device)
            if hit and hit[0] > value:
                value, matched_on = hit
        if value < min_score:
            continue
        entry = device.to_dict()
        entry["score"] = value
        entry["matched_on"] = matched_on
        if keep_raw:
            entry["raw"] = device.raw
        candidates.append(entry)

    candidates.sort(key=lambda c: (-c["score"], c["matched_on"] == "manufacturer",
                                   c["latest_version"] is False, c["trade_name"]))
    candidates = candidates[:top]

    def settle(cands):
        """Status for the current candidate list and query log."""
        if cands:
            return classify(cands[0]["score"])
        if queries and all(q["error"] for q in queries):
            # No query succeeded, so the register was never actually
            # consulted. "Not found" would assert something never checked.
            return STATUS_ERROR
        return STATUS_NOT_FOUND

    status = settle(candidates)

    # Exact filters cannot find a near-miss, so fall back to substring search
    # for devices the primary pass missed entirely.
    if widen_client is not None and status in (STATUS_NOT_FOUND, STATUS_ERROR):
        # Substring matching still needs the term to *be* a substring, so a
        # punctuation difference defeats it: "PINK! Coach" is not contained in
        # "PINK Coach - Breast Cancer Companion". Probe the normalised form as
        # well, which matters most in the browser, where a typed name has no
        # `broad` terms to fall back on.
        widen_terms = []
        for raw in target.keys + target.broad:
            for candidate in (raw, norm(raw)):
                if candidate and candidate not in widen_terms:
                    widen_terms.append(candidate)

        for term in widen_terms:
            record = {"term": term, "param": "TRADE_NAME", "rows": None,
                      "error": None, "backend": "ui"}
            try:
                rows, _ = widen_client.udi(TRADE_NAME=term)
            except AuthError:
                raise
            except (ApiError, ValueError) as exc:
                record["error"] = str(exc)
                errors.append(f"widen TRADE_NAME={term!r}: {exc}")
                queries.append(record)
                continue
            record["rows"] = len(rows)
            queries.append(record)
            raw_keys.update(describe_keys(rows))
            for row in rows:
                device = Device(row)
                if reference is not None:
                    reference.enrich(device)
                key = device.identity()
                if key in seen:
                    continue
                seen[key] = device
                value, matched_on = score_device(target.keys, target.country, device)
                if value < min_score:
                    continue
                entry = device.to_dict()
                entry["score"] = value
                entry["matched_on"] = matched_on
                # Provenance: this came from the undocumented substring
                # backend, not from an exact match on the documented API.
                entry["matched_via"] = "ui-substring"
                if keep_raw:
                    entry["raw"] = device.raw
                candidates.append(entry)

        candidates.sort(key=lambda c: (-c["score"], c["matched_on"] == "manufacturer",
                                       c["latest_version"] is False, c["trade_name"]))
        candidates = candidates[:top]
        # Re-settle: a widen query that succeeded but found nothing means the
        # register *was* consulted, so this is "not found" rather than "error".
        status = settle(candidates)

    return {
        **target.to_dict(),
        "status": status,
        "candidates": candidates,
        "queries": queries,
        "errors": errors,
        "response_fields": sorted(raw_keys),
        "total_matches": len(seen),
    }


def run(client, targets, reference=None, top=5, min_score=0.45,
        fields="TRADE_NAME", keep_raw=False, progress=None, widen_client=None):
    results = []
    for target in targets:
        if progress:
            progress(target.name)
        result = search_target(client, target, reference=reference, top=top,
                              min_score=min_score, fields=fields, keep_raw=keep_raw,
                              widen_client=widen_client)
        results.append(result)
        if progress:
            top_c = result["candidates"][0] if result["candidates"] else None
            detail = ""
            if result["status"] == STATUS_ERROR:
                detail = f" ({len(result['errors'])} request error(s) - nothing checked)"
            if top_c:
                via = f" via {top_c['matched_on']}"
                if top_c.get("matched_via"):
                    via += f", {top_c['matched_via']}"
                detail = (f" (best: {top_c['trade_name']!r} {top_c['score']}"
                          f"{via})")
            progress(f"  -> {result['status']}{detail}", indent=True)
    return results
