"""Orchestration: device list in, ranked candidates out."""

import csv

from .client import ApiError, AuthError
from .fields import describe_keys
from .matching import classify, score_device
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
                  fields="TRADE_NAME", keep_raw=False):
    """Run every query term for one target and rank the union of the results."""
    seen, queries, errors, raw_keys = {}, [], [], set()
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

    candidates = []
    for device in seen.values():
        value, matched_on = score_device(target.keys, target.country, device)
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
    status = classify(candidates[0]["score"]) if candidates else "not found"

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
        fields="TRADE_NAME", keep_raw=False, progress=None):
    results = []
    for target in targets:
        if progress:
            progress(target.name)
        result = search_target(client, target, reference=reference, top=top,
                              min_score=min_score, fields=fields, keep_raw=keep_raw)
        results.append(result)
        if progress:
            top_c = result["candidates"][0] if result["candidates"] else None
            detail = ""
            if top_c:
                detail = (f" (best: {top_c['trade_name']!r} {top_c['score']} "
                          f"via {top_c['matched_on']})")
            progress(f"  -> {result['status']}{detail}", indent=True)
    return results
