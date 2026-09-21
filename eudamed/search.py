"""Orchestration: device list in, ranked candidates out."""

import csv

from . import devicetype
from .client import ApiError, AuthError
from .fields import describe_keys
from .matching import STATUS_ERROR, STATUS_NOT_FOUND, classify, norm, score_device, score_identifier
from .records import Actor, Device


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
                  fields="TRADE_NAME", keep_raw=False, widen_client=None,
                  software_only=False, typer=None):
    """Run every query term for one target and rank the union of the results.

    ``widen_client`` is an optional substring-capable client (see
    ``eudamed.ui_backend.UiClient``). The documented API matches names
    exactly, so a device whose trade name *and* device name both differ from
    every supplied term cannot be found at all. When the primary pass comes
    back empty, that client is asked the same question with substring
    matching, and anything it returns is tagged ``matched_via`` so a fallback
    hit is never mistaken for a confirmed exact match.

    ``software_only`` drops candidates whose own record does not say they are
    software (see ``eudamed.devicetype``). What was dropped, and why, is
    reported in ``dropped_kinds`` rather than simply vanishing. ``typer`` is
    an optional ``devicetype.Typer``, which can consult a device's detail
    record when its list row carries no device type - necessary on the web-UI
    backend, whose list rows have no nomenclature code.
    """
    seen, found_via, queries, errors, raw_keys = {}, {}, [], [], set()
    dropped = {devicetype.OTHER: 0, devicetype.UNKNOWN: 0}

    def admit(entry, device):
        """Whether a scored candidate survives the device-type filter."""
        if not software_only:
            return True
        if typer is not None:
            kind, reason = typer.classify(device)
            entry["device_kind"], entry["device_kind_reason"] = kind, reason
        kind = entry.get("device_kind")
        if kind == devicetype.SOFTWARE:
            return True
        dropped[kind] = dropped.get(kind, 0) + 1
        return False

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
        if not admit(entry, device):
            continue
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
                if not admit(entry, device):
                    continue
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
        "software_only": bool(software_only),
        # Split by kind so a sparsely populated device-type field shows up as
        # a large "unknown" count rather than as a quietly shorter result.
        "dropped_kinds": dict(dropped),
    }


def run(client, targets, reference=None, top=5, min_score=0.45,
        fields="TRADE_NAME", keep_raw=False, progress=None, widen_client=None,
        software_only=False, typer=None):
    results = []
    for target in targets:
        if progress:
            progress(target.name)
        result = search_target(client, target, reference=reference, top=top,
                              min_score=min_score, fields=fields, keep_raw=keep_raw,
                              widen_client=widen_client, software_only=software_only,
                              typer=typer)
        results.append(result)
        if progress:
            top_c = result["candidates"][0] if result["candidates"] else None
            detail = ""
            if result["status"] == STATUS_ERROR:
                detail = f" ({len(result['errors'])} request error(s) - nothing checked)"
            drops = sum(result.get("dropped_kinds", {}).values())
            if drops and not top_c:
                detail = f" ({drops} candidate(s) dropped as not software)"
            if top_c:
                via = f" via {top_c['matched_on']}"
                if top_c.get("matched_via"):
                    via += f", {top_c['matched_via']}"
                detail = (f" (best: {top_c['trade_name']!r} {top_c['score']}"
                          f"{via})")
            progress(f"  -> {result['status']}{detail}", indent=True)
    return results


# ------------------------------------------------------------ manufacturer
def _actor_srns(client, name, queries, errors, backend=""):
    """Look up actors by name and return (actors, srns) for that one client."""
    record = {"term": name, "param": "NAME", "rows": None, "error": None}
    if backend:
        record["backend"] = backend
    try:
        rows, _ = client.actors(NAME=name)
    except AuthError:
        raise
    except (ApiError, ValueError) as exc:
        record["error"] = str(exc)
        errors.append(f"NAME={name!r}: {exc}")
        queries.append(record)
        return [], []
    record["rows"] = len(rows)
    queries.append(record)
    actors = [Actor(row) for row in rows]
    return actors, [a.actor_id for a in actors if a.actor_id]


def search_manufacturer(client, name, reference=None, actor_client=None,
                        srns=None, top=200, software_only=False, keep_raw=False,
                        typer=None):
    """Every device registered by a manufacturer, found from its name.

    Two steps, because /udi has no manufacturer-*name* filter at all - only
    MF_SRN, the actor's registration number:

    1. ``/actors?NAME=`` resolves the name to one or more SRNs.
    2. ``/udi?MF_SRN=`` lists that actor's devices.

    On the documented API step 1 is an exact whole-string match, so
    "HelloBetter" will not find "GET.ON Institut fuer Online Gesundheitstrai-
    ning GmbH". ``actor_client`` is an optional substring-capable client (the
    web-UI backend) used to widen step 1 when the exact lookup finds nothing -
    the same fallback shape the name search uses.

    Pass ``srns`` to skip step 1 entirely when the SRN is already known.
    """
    queries, errors, actors = [], [], []
    wanted = list(srns or [])

    if not wanted:
        actors, wanted = _actor_srns(client, name, queries, errors,
                                     backend="primary" if actor_client else "")
        if not wanted and actor_client is not None:
            actors, wanted = _actor_srns(actor_client, name, queries, errors,
                                         backend="ui")
            for actor in actors:
                actor.matched_via = "ui-substring"

    # De-duplicate while keeping the lookup order, so the report reads in the
    # order the register returned.
    seen_srn, srn_list = set(), []
    for srn in wanted:
        if srn not in seen_srn:
            seen_srn.add(srn)
            srn_list.append(srn)

    devices, seen_dev = [], set()
    dropped = {devicetype.OTHER: 0, devicetype.UNKNOWN: 0}
    for srn in srn_list:
        record = {"term": srn, "param": "MF_SRN", "rows": None, "error": None}
        try:
            rows, _ = client.udi(MF_SRN=srn)
        except AuthError:
            raise
        except (ApiError, ValueError) as exc:
            record["error"] = str(exc)
            errors.append(f"MF_SRN={srn!r}: {exc}")
            queries.append(record)
            continue
        record["rows"] = len(rows)
        queries.append(record)
        for row in rows:
            device = Device(row)
            if reference is not None:
                reference.enrich(device)
            key = device.identity()
            if key in seen_dev:
                continue
            seen_dev.add(key)
            entry = device.to_dict()
            # An MF_SRN hit is a match by construction: the API matched the
            # manufacturer's registration number, so there is nothing to score.
            entry["score"] = 1.0
            entry["matched_on"] = f"identifier:MF_SRN:{srn}"
            if keep_raw:
                entry["raw"] = device.raw
            if software_only:
                if typer is not None:
                    kind, reason = typer.classify(device)
                    entry["device_kind"], entry["device_kind_reason"] = kind, reason
                kind = entry.get("device_kind")
                if kind != devicetype.SOFTWARE:
                    dropped[kind] = dropped.get(kind, 0) + 1
                    continue
            devices.append(entry)

    devices.sort(key=lambda d: (d["trade_name"].lower(), d["primary_di"]))
    return {
        "query": name,
        "actors": [a.to_dict() for a in actors],
        "srns": srn_list,
        "devices": devices[:top],
        "device_count": len(devices),
        "queries": queries,
        "errors": errors,
        "software_only": bool(software_only),
        "dropped_kinds": dict(dropped),
    }


def manufacturer_results(found):
    """Re-shape a manufacturer search as a list of standard result dicts.

    Lets the existing report writers produce the same document for a
    manufacturer search as for a name search, instead of a second format.
    """
    results = []
    for entry in found["devices"]:
        results.append({
            "name": entry["trade_name"] or entry["device_name"] or entry["primary_di"]
                    or "(unnamed)",
            "description": entry.get("medical_purpose", ""),
            "ca": "", "country": entry.get("manufacturer_country", ""),
            "keys": [found["query"]], "broad": [],
            "status": "found", "candidates": [entry],
            "queries": found["queries"], "errors": [],
            "response_fields": [], "total_matches": found["device_count"],
        })
    return results
