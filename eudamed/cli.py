"""Command line interface for the EUDAMED Public API v1.0."""

import argparse
import json
import os
import sys
import threading

from . import config
from .cache import RowCache
from .client import ApiError, AuthError, Client
from .fields import describe_keys
from .records import Actor, Device
from .reference import Reference
from .report import write_all
from .search import Target, load_targets, run, run_cached
from .ui_backend import DEFAULT_UI_BASE, UiClient

EXIT_OK, EXIT_ERROR, EXIT_AUTH, EXIT_USAGE = 0, 1, 2, 3


def log(message, indent=False):
    print(("  " if indent else "") + message, file=sys.stderr, flush=True)


def add_common(parser):
    conn = parser.add_argument_group("connection")
    conn.add_argument("--backend", choices=("ui", "datalake"), default=None,
                      help="which EUDAMED API to use. 'ui' is the website's own backend: "
                           "undocumented, but it does SUBSTRING search. 'datalake' is the "
                           "documented public API, whose filters are EXACT match only")
    conn.add_argument("--base", default=None,
                      help=f"API base URL (default depends on --backend: "
                           f"{DEFAULT_UI_BASE} or {config.DEFAULT_BASE})")
    conn.add_argument("--key", default=None,
                      help=f"subscription key (default ${config.KEY_ENV})")
    conn.add_argument("--auth-mode", choices=("header", "query"), default="header",
                      help=f"send the key as the {config.KEY_HEADER} header (default) "
                           f"or the {config.KEY_QUERY} query parameter")
    conn.add_argument("--format", dest="fmt", choices=config.FORMATS, default="json",
                      help="response format requested from the API (required by the spec)")
    conn.add_argument("--api-version", default=config.API_VERSION,
                      help="value for the api-version query parameter ('' to omit)")
    conn.add_argument("--delay", type=float, default=0.2,
                      help="minimum seconds between requests (default 0.2)")
    conn.add_argument("--retries", type=int, default=4,
                      help="attempts per request, with exponential backoff (default 4)")
    conn.add_argument("--timeout", type=float, default=60)
    conn.add_argument("--require-key", dest="require_key", action="store_true",
                      help="refuse to run without a subscription key. Off by default: the live "
                           "API answers anonymous requests, despite the spec declaring a key")
    conn.add_argument("--no-key", dest="require_key", action="store_false",
                      help="deprecated, kept for compatibility - this is now the default")
    conn.add_argument("--dry-run", action="store_true",
                      help="print the URLs that would be requested, then exit")
    parser.add_argument("--verbose", "-v", action="store_true")


def make_client(args):
    """The client for the chosen backend.

    `ui` is the EUDAMED website's own backend: undocumented, but it does
    substring search, which the documented `datalake` API does not.
    """
    if getattr(args, "backend", "datalake") == "ui":
        return UiClient(base=args.base, delay=args.delay, retries=args.retries,
                        timeout=args.timeout, verbose=args.verbose,
                        page_size=getattr(args, "page_size", 100),
                        max_pages=getattr(args, "max_pages", 5),
                        language=getattr(args, "language", "en"))
    return Client(base=args.base, key=args.key, auth_mode=args.auth_mode, fmt=args.fmt,
                  delay=args.delay, retries=args.retries, timeout=args.timeout,
                  verbose=args.verbose, api_version=args.api_version)


def require_key(client, args):
    """Whether to proceed without a subscription key.

    The OpenAPI document declares a key as required, but that block is an Azure
    APIM portal artefact: the live gateway answers anonymous requests. Verified
    against the real API - GET /udi and GET /reference both return 200 with no
    credential, and /reference served 294 rows. So running without a key is the
    default, and --require-key opts back in to the strict check.
    """
    if getattr(args, "backend", "datalake") == "ui":
        return True          # the web-UI backend takes no credential
    if client.key or args.dry_run or not getattr(args, "require_key", False):
        return True
    log("No subscription key, and --require-key was given.")
    log(f"  Pass --key, or export {config.KEY_ENV}=..., or drop --require-key: "
        "the live API does answer anonymous requests.")
    return False


# ---------------------------------------------------------------- search
def cmd_search(args):
    if args.input:
        try:
            targets = load_targets(args.input)
        except (OSError, ValueError) as exc:
            log(f"cannot read --input: {exc}")
            return EXIT_USAGE
    elif args.trade_name:
        targets = [Target(name=args.trade_name, country=args.country or "",
                          keys=[args.trade_name])]
    else:
        log("give either --trade-name NAME or --input FILE.csv")
        return EXIT_USAGE

    # Validate arguments before credentials, so a typo reports the typo rather
    # than a missing key.
    given = [f.strip() for f in args.fields.split(",") if f.strip()]
    if not given:
        log("--fields must name at least one /udi parameter")
        return EXIT_USAGE
    params = []
    for raw in given:
        param = raw.upper()
        if param not in config.UDI_PARAMS:
            log(f"--fields: {raw!r} is not a documented /udi parameter. "
                f"Choose from: {', '.join(config.UDI_PARAMS)}")
            return EXIT_USAGE
        params.append(param)

    # Local mode: match against a cache instead of querying per name. This is
    # the only mode that can find an approximate name, because /udi filters are
    # exact-match.
    if args.cache:
        try:
            cache = RowCache.load(args.cache)
        except (OSError, ValueError) as exc:
            log(f"cannot read --cache: {exc}")
            return EXIT_USAGE
        log(f"matching against {len(cache)} cached row(s) from {args.cache}")
        if cache.truncated_partitions:
            log(f"  note: {len(cache.truncated_partitions)} partition(s) in this cache hit "
                "the 1000-row cap, so it is incomplete")
        reference = None
        if args.resolve_codes:
            client = make_client(args)
            log("loading reference codes")
            reference = Reference(client, language=args.language,
                                  verbose=args.verbose).load()
        results = run_cached(list(cache.devices(reference)), targets, top=args.top,
                             min_score=args.min_score, keep_raw=args.keep_raw,
                             progress=log)
        meta = {"base": f"local cache {args.cache}", "fields": "local fuzzy match",
                "format": "cache", "requests": 0,
                "cached_rows": len(cache),
                "cache_incomplete": bool(cache.truncated_partitions),
                "tool_version": __import__("eudamed").__version__}
        paths = write_all(results, args.out, meta)
        tally = {s: sum(r["status"] == s for r in results)
                 for s in ("found", "possible", "not found")}
        log("")
        log(f"{tally}  from {len(cache)} cached row(s)")
        for kind in ("json", "csv", "md", "html"):
            log(f"  {kind:4} {os.path.abspath(paths[kind])}")
        return EXIT_OK

    client = make_client(args)
    if not require_key(client, args):
        return EXIT_AUTH

    if args.dry_run:
        for target in targets:
            for term in target.keys + target.broad:
                for param in params:
                    print(client.build_url(client.DEVICE_PATH, {param: term}))
        return EXIT_OK

    reference = None
    if args.resolve_codes:
        log("loading reference codes")
        reference = Reference(client, language=args.language, verbose=args.verbose).load()

    try:
        results = run(client, targets, reference=reference, top=args.top,
                      min_score=args.min_score, fields=args.fields,
                      keep_raw=args.keep_raw, progress=log)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH

    meta = {"base": client.base, "backend": args.backend,
            "fields": args.fields, "format": args.fmt,
            "requests": client.request_count, "api_version": args.api_version,
            "reference_loaded": bool(reference and reference.tables),
            "tool_version": __import__("eudamed").__version__}
    paths = write_all(results, args.out, meta)

    tally = {s: sum(r["status"] == s for r in results)
             for s in ("found", "possible", "not found")}
    log("")
    log(f"{tally}  in {client.request_count} request(s)")
    for kind in ("json", "csv", "md", "html"):
        log(f"  {kind:4} {os.path.abspath(paths[kind])}")

    mfr = [r["name"] for r in results
           if any(c["matched_on"] == "manufacturer" for c in r["candidates"])]
    if mfr:
        log("")
        log(f"manufacturer-only leads (not counted as matches): {', '.join(mfr)}")
    unresolved = [r["name"] for r in results if r["errors"]]
    if unresolved:
        log(f"devices with request errors: {', '.join(unresolved)}")
    return EXIT_OK


# ---------------------------------------------------------------- actors
def cmd_actors(args):
    client = make_client(args)
    query = {k: v for k, v in (("NAME", args.name), ("ACTOR_ID", args.actor_id),
                               ("ACTOR_TYPE", args.actor_type),
                               ("ACT_COUNTRY_ISO2_CODE", args.country)) if v}
    if not query:
        log("give at least one of --name, --actor-id, --actor-type, --country")
        return EXIT_USAGE
    if args.dry_run:
        print(client.build_url("/actors", query))
        return EXIT_OK
    if not require_key(client, args):
        return EXIT_AUTH
    try:
        rows, _ = client.actors(**query)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH
    except (ApiError, ValueError) as exc:
        log(str(exc))
        return EXIT_ERROR
    actors = [Actor(r).to_dict() for r in rows]
    print(json.dumps(actors, indent=2, ensure_ascii=False))
    log(f"{len(actors)} actor(s)")
    return EXIT_OK


# ------------------------------------------------------------- reference
def cmd_reference(args):
    client = make_client(args)
    query = {k: v for k, v in (("ID", args.id), ("CODE", args.code),
                               ("LANGUAGE", args.language)) if v}
    if args.dry_run:
        print(client.build_url("/reference", query))
        return EXIT_OK
    if not require_key(client, args):
        return EXIT_AUTH
    try:
        rows, body = client.reference(**query)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH
    except (ApiError, ValueError) as exc:
        log(str(exc))
        return EXIT_ERROR
    if args.tables:
        # /reference is one flat table keyed by (CODE, ID); CODE names the code
        # table and VALUE holds the label.
        from .fields import index_row, pick
        tables = {}
        for row in rows:
            i = index_row(row)
            tables.setdefault(str(pick(i, "CODE")), []).append(
                (pick(i, "ID", default=None), pick(i, "VALUE")))
        for code in sorted(tables):
            entries = tables[code]
            print(f"{code}  ({len(entries)} value(s))")
            for rid, label in entries[:8]:
                print(f"    {rid} = {label}")
            if len(entries) > 8:
                print(f"    ... {len(entries) - 8} more")
        log(f"{len(tables)} code table(s), {len(rows)} row(s)")
        return EXIT_OK

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body)
        log(f"{len(rows)} row(s) -> {os.path.abspath(args.out)}")
    else:
        print(json.dumps(rows, indent=2, ensure_ascii=False)[:20000])
        log(f"{len(rows)} row(s)")
    return EXIT_OK


# ----------------------------------------------------------------- probe
def cmd_probe(args):
    """One call per operation, reporting the real response shape.

    The OpenAPI document declares no response schemas, so the field names this
    prints are the ground truth the record mapping should be aligned to.
    """
    client = make_client(args)
    if args.dry_run:
        print(client.build_url("/udi", {"TRADE_NAME": args.trade_name}))
        return EXIT_OK
    if not require_key(client, args):
        return EXIT_AUTH

    ops = [("/udi", {"TRADE_NAME": args.trade_name}),
           ("/actors", {"NAME": args.actor_name} if args.actor_name else None),
           ("/reference", {"LANGUAGE": args.language})]
    report, failures = {}, 0
    for path, query in ops:
        if query is None:
            continue
        log(f"probing {path} {query}")
        try:
            rows, body = client.request(path, query)
        except AuthError as exc:
            log(str(exc))
            return EXIT_AUTH
        except (ApiError, ValueError) as exc:
            log(f"  failed: {exc}")
            report[path] = {"error": str(exc)}
            failures += 1
            continue
        keys = describe_keys(rows)
        report[path] = {"rows": len(rows), "bytes": len(body), "fields": keys,
                        "sample": rows[0] if rows else None}
        log(f"  {len(rows)} row(s), {len(keys)} field(s)")

        # A 200 with no rows is the confusing case: it could be an empty result,
        # an unrecognised envelope, or an error delivered with a 200. Show the
        # body, since at this size it is the whole answer.
        if not rows:
            snippet = body.strip()
            log(f"  body ({len(body)} bytes): {snippet[:400]!r}")
            report[path]["body"] = snippet[:1000]
        if args.raw_dir:
            os.makedirs(args.raw_dir, exist_ok=True)
            name = path.strip("/").replace("/", "_") + (".json" if args.fmt == "json" else ".csv")
            with open(os.path.join(args.raw_dir, name), "w", encoding="utf-8") as handle:
                handle.write(body)
            log(f"  raw -> {os.path.join(args.raw_dir, name)}")

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    # If the filtered /udi call found nothing, try it unfiltered. That
    # separates "this endpoint returns nothing at all" from "the filter matched
    # nothing", which need completely different fixes.
    udi = report.get("/udi", {})
    if udi.get("rows") == 0 and not args.dry_run:
        log("")
        log("/udi returned no rows - retrying with no filter to tell apart "
            "an empty endpoint from an unmatched filter")
        try:
            rows, body = client.request("/udi", {})
            report["/udi (no filter)"] = {
                "rows": len(rows), "bytes": len(body),
                "fields": describe_keys(rows),
                "sample": rows[0] if rows else None,
                "body": None if rows else body.strip()[:1000],
            }
            log(f"  unfiltered: {len(rows)} row(s), {len(body)} bytes")
            if rows:
                log("  -> the endpoint has data, so TRADE_NAME matched nothing. Either the "
                    "device is not registered under that name, or the filter needs a "
                    "different form (try --fields DEVICE_NAME, or a shorter term).")
                log(f"  -> fields: {', '.join(describe_keys(rows))}")
            else:
                log(f"  -> the endpoint returns nothing even unfiltered. Body: "
                    f"{body.strip()[:200]!r}")
                log("     This looks like the dataset is not exposed here, not a search problem.")
        except AuthError as exc:
            log(str(exc))
        except (ApiError, ValueError) as exc:
            log(f"  unfiltered probe failed: {exc}")

    if udi.get("fields"):
        log("")
        log("/udi response fields:")
        for key in udi["fields"]:
            log(f"  {key}", indent=True)
        mapped = Device(udi.get("sample") or {})
        missing = [name for name in ("trade_name", "manufacturer_name", "mf_srn", "primary_di")
                   if not getattr(mapped, name)]
        if missing:
            log("")
            log(f"fields the record mapping could NOT resolve: {', '.join(missing)}")
            log("Add the real spellings to eudamed/records.py (Device.__init__).")
        else:
            log("")
            log("record mapping resolved trade name, manufacturer, SRN and UDI-DI.")
    return EXIT_ERROR if failures else EXIT_OK


# ----------------------------------------------------------------- serve
def cmd_serve(args):
    """Run the local web UI."""
    from .webui import serve as make_server

    client = make_client(args)
    if not require_key(client, args):
        return EXIT_AUTH

    targets = []
    if args.input:
        try:
            targets = load_targets(args.input)
        except (OSError, ValueError) as exc:
            log(f"cannot read --input: {exc}")
            return EXIT_USAGE

    cache = None
    if args.cache:
        try:
            cache = RowCache.load(args.cache)
        except (OSError, ValueError) as exc:
            log(f"cannot read --cache: {exc}")
            return EXIT_USAGE

    server = make_server(client, port=args.port, host=args.host,
                         verbose=args.verbose, targets=targets, cache=cache)
    url = f"http://{args.host}:{args.port}"
    log(f"EUDAMED search UI on {url}")
    log(f"  querying {client.base}  (--backend {args.backend})")
    if targets:
        log(f"  {len(targets)} device(s) loaded from {args.input}")
    if cache is not None:
        log(f"  {len(cache)} cached row(s) from {args.cache}")
        if cache.truncated_partitions:
            log(f"  note: {len(cache.truncated_partitions)} partition(s) were truncated, "
                "so the cache is incomplete")
    log("  press Ctrl-C to stop")
    if args.open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        server.server_close()
    return EXIT_OK


# ------------------------------------------------------------------ scan
def cmd_scan(args):
    """Build a local cache of /udi rows.

    Necessary because /udi filters are exact-match and there is no pagination:
    an approximate name cannot be searched server-side, so rows must be held
    locally and matched here. A single request returns at most 1000 rows, so
    the dataset is fetched in partitions.
    """
    client = make_client(args)
    cache = RowCache()

    partitions = []
    for item in args.filter or []:
        if "=" not in item:
            log(f"--filter must be K=V, got {item!r}")
            return EXIT_USAGE
        key, value = item.split("=", 1)
        if key not in config.UDI_PARAMS:
            log(f"--filter: {key!r} is not a documented /udi parameter")
            return EXIT_USAGE
        partitions.append({key: value})

    if args.partition_by:
        field = args.partition_by.upper()
        if field not in config.UDI_PARAMS:
            log(f"--partition-by: {field!r} is not a documented /udi parameter")
            return EXIT_USAGE
        log(f"reading {field} values from /reference")
        reference = Reference(client, language=args.language, verbose=args.verbose).load()
        values = [rid for (table, rid) in reference._values if table == field]
        if not values:
            log(f"  /reference has no {field} table, so it cannot be partitioned on.")
            log("  Use --filter K=V with values you know instead.")
            return EXIT_USAGE
        log(f"  {len(values)} value(s): {sorted(values)}")
        base = list(partitions) or [{}]
        partitions = [{**p, field: v} for p in base for v in sorted(values)]

    if not partitions:
        partitions = [{}]
        log("no partitions given - fetching one unfiltered page (capped at 1000 rows)")

    for n, params in enumerate(partitions, start=1):
        label = ", ".join(f"{k}={v}" for k, v in params.items()) or "(unfiltered)"
        try:
            rows, body = client.request("/udi", params)
        except AuthError as exc:
            log(str(exc))
            return EXIT_AUTH
        except (ApiError, ValueError) as exc:
            log(f"  [{n}/{len(partitions)}] {label}: failed - {exc}")
            continue
        truncated = len(rows) == 1000
        added = cache.add(rows, source=params, truncated=truncated)
        flag = "  TRUNCATED at the 1000-row cap" if truncated else ""
        log(f"  [{n}/{len(partitions)}] {label}: {len(rows)} row(s), "
            f"+{added} new, {len(body)} bytes{flag}")

    path = args.out
    saved, meta = cache.save(path)
    log("")
    log(f"{len(cache)} unique row(s) -> {os.path.abspath(saved)}")
    log(f"  metadata -> {os.path.abspath(meta)}")
    if cache.truncated_partitions:
        log("")
        log(f"WARNING: {len(cache.truncated_partitions)} partition(s) hit the 1000-row cap,")
        log("         so the cache is INCOMPLETE. Partition more finely before concluding")
        log("         that a device is absent.")
    log("")
    log("Match a device list against it with:")
    log(f"  python3 -m eudamed search --input devices.csv --cache {saved} --out results")
    return EXIT_OK


# ------------------------------------------------------------- discover
def cmd_discover(args):
    """Find devices by the documented /udi filters rather than by name.

    For questions like "all class I software for psychological conditions",
    where you do not have a list of trade names to start from.
    """
    client = make_client(args)

    reference = None
    risk_class_id = args.risk_class_id
    if args.risk_class and risk_class_id is None:
        # RISK_CLASS_ID is numeric, so a human class ("I", "IIa") has to be
        # resolved through /reference first.
        log("resolving the risk class via /reference")
        reference = Reference(client, language=args.language, verbose=args.verbose).load()
        wanted = args.risk_class.strip().lower().replace("class", "").strip()
        matches = [(rid, label) for (table, rid), label in reference._values.items()
                   if table == "RISK_CLASS_ID"
                   and label.strip().lower().replace("class", "").strip() == wanted]
        if not matches:
            available = sorted(
                label for (table, _), label in reference._values.items()
                if table == "RISK_CLASS_ID")
            log(f"no risk class matching {args.risk_class!r}.")
            log(f"  available: {', '.join(available) or '(reference lookup returned none)'}")
            return EXIT_USAGE
        risk_class_id, label = matches[0]
        log(f"  {args.risk_class!r} -> RISK_CLASS_ID={risk_class_id} ({label})")

    params = {}
    for name, value in (("RISK_CLASS_ID", risk_class_id),
                        ("NOMENCLATURE_CODE", args.nomenclature),
                        ("MEDICAL_PURPOSE", args.medical_purpose),
                        ("DEVICE_NAME", args.device_name),
                        ("TRADE_NAME", args.trade_name),
                        ("MF_SRN", args.mf_srn),
                        ("APPLICABLE_LEGISLATION_ID", args.legislation_id)):
        if value not in (None, ""):
            params[name] = value
    if not params:
        log("give at least one filter, e.g. --risk-class I --medical-purpose depression")
        return EXIT_USAGE

    if args.dry_run:
        print(client.build_url("/udi", params))
        return EXIT_OK

    log(f"querying /udi with {params}")
    try:
        rows, body = client.request("/udi", params)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH
    except (ApiError, ValueError) as exc:
        log(str(exc))
        return EXIT_ERROR

    log(f"  {len(rows)} row(s), {len(body)} bytes")
    if len(rows) == 1000:
        log("  WARNING: exactly 1000 rows - this is the server cap, so the result is")
        log("           TRUNCATED. Narrow the filters; do not treat this as complete.")

    if reference is None and args.resolve_codes:
        reference = Reference(client, language=args.language, verbose=args.verbose).load()

    # Optional local keyword narrowing, for concepts the API cannot filter on.
    terms = [t.strip().lower() for t in (args.keyword or "").split(",") if t.strip()]
    kept, devices = [], []
    for row in rows:
        device = Device(row)
        if reference is not None:
            reference.enrich(device)
        devices.append(device)
        if terms:
            haystack = " ".join(str(v) for v in device.to_dict().values()).lower()
            if not any(t in haystack for t in terms):
                continue
        kept.append(device)
    if terms:
        log(f"  {len(kept)} of {len(devices)} row(s) mention {terms}")

    # Present each hit as its own single-candidate result, so the existing
    # writers produce the same report shape as a name search.
    results = []
    for device in kept[:args.top]:
        entry = device.to_dict()
        entry["score"] = 1.0
        entry["matched_on"] = "filter:" + ",".join(sorted(params))
        if args.keep_raw:
            entry["raw"] = device.raw
        results.append({
            "name": device.trade_name or device.device_name or device.primary_di or "(unnamed)",
            "description": device.medical_purpose, "ca": "", "country": device.country,
            "status": "found", "candidates": [entry],
            "queries": [{"param": k, "term": v, "rows": len(rows), "error": None}
                        for k, v in params.items()],
            "errors": [], "response_fields": sorted(device.raw), "total_matches": len(rows),
        })

    meta = {"base": client.base, "fields": ", ".join(f"{k}={v}" for k, v in params.items()),
            "format": args.fmt, "requests": client.request_count,
            "api_version": args.api_version,
            "truncated": len(rows) == 1000,
            "tool_version": __import__("eudamed").__version__}
    paths = write_all(results, args.out, meta)
    log("")
    log(f"{len(results)} device(s) written")
    for kind in ("json", "csv", "md", "html"):
        log(f"  {kind:4} {os.path.abspath(paths[kind])}")
    return EXIT_OK


# ------------------------------------------------------------------ raw
def cmd_raw(args):
    """GET any operation with arbitrary parameters, bypassing the allowlist."""
    client = make_client(args)
    params = {}
    for item in args.param:
        if "=" not in item:
            log(f"--param must be K=V, got {item!r}")
            return EXIT_USAGE
        key, value = item.split("=", 1)
        params[key] = value
    path = args.path if args.path.startswith("/") else "/" + args.path

    if args.dry_run:
        print(client.build_url(path, params, allow_undocumented=True))
        return EXIT_OK
    try:
        rows, body = client.request(path, params, allow_undocumented=True)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH
    except (ApiError, ValueError) as exc:
        log(str(exc))
        return EXIT_ERROR

    log(f"{len(rows)} row(s), {len(body)} bytes")
    if len(rows) == 1000:
        log("  note: exactly 1000 rows - likely a server-side cap, so this is truncated")
    if rows:
        log(f"fields: {', '.join(describe_keys(rows))}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body)
        log(f"body -> {os.path.abspath(args.out)}")
    else:
        print(json.dumps(rows[:args.rows] if rows else body[:2000], indent=2, default=str))
    return EXIT_OK


# ------------------------------------------------------------ filtertest
# Strategies tried against /udi for one term, in order. Each is (label, params).
def filter_strategies(term, param="TRADE_NAME"):
    return [
        ("exact as typed", {param: term}),
        ("lowercase", {param: term.lower()}),
        ("UPPERCASE", {param: term.upper()}),
        ("first word only", {param: term.split()[0] if term.split() else term}),
        ("first 4 characters", {param: term[:4]}),
        ("trailing * wildcard", {param: term + "*"}),
        ("surrounding * wildcards", {param: f"*{term}*"}),
        ("SQL-style % wildcards", {param: f"%{term}%"}),
        # The response envelope is {"value": [...]}, i.e. OData, so these are
        # worth trying even though the spec documents none of them.
        ("OData $filter contains()", {"$filter": f"contains({param},'{term}')"}),
        ("OData $filter eq", {"$filter": f"{param} eq '{term}'"}),
        ("OData $top=5", {"$top": "5"}),
        ("OData $count=true", {"$count": "true"}),
        ("OData $skip=1000 + $top=5", {"$skip": "1000", "$top": "5"}),
    ]


def cmd_filtertest(args):
    """Work out how /udi filtering actually behaves.

    The spec documents the filter parameters but not their semantics, and a
    plain TRADE_NAME query can return nothing while the endpoint holds data.
    This tries a battery of forms and, crucially, calibrates against a trade
    name taken from the API's own unfiltered response - so there is a control
    that must match.
    """
    client = make_client(args)
    if args.dry_run:
        for label, params in filter_strategies(args.term, args.field):
            print(f"{label}: "
                  f"{client.build_url(client.DEVICE_PATH, params, allow_undocumented=True)}")
        return EXIT_OK

    # Control: a real value from the dataset. If even this does not match, the
    # problem is the filter mechanism, not the term.
    control = None
    log("fetching an unfiltered sample to calibrate against")
    try:
        rows, body = client.request(client.DEVICE_PATH, {})
        log(f"  unfiltered: {len(rows)} row(s), {len(body)} bytes")
        if len(rows) == 1000:
            log("  note: exactly 1000 rows - almost certainly a server-side cap, "
                "so this sample is truncated")
        for row in rows:
            name = Device(row).trade_name
            if name:
                control = name
                break
        if control:
            log(f"  control trade name from the API: {control!r}")
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH
    except (ApiError, ValueError) as exc:
        log(f"  unfiltered fetch failed: {exc}")
        return EXIT_ERROR

    results = []
    trials = filter_strategies(args.term, args.field)
    if control:
        trials.insert(0, ("CONTROL: exact real trade name", {args.field: control}))

    for label, params in trials:
        try:
            rows, body = client.request(client.DEVICE_PATH, params,
                                        allow_undocumented=True)
            names = [Device(r).trade_name for r in rows[:3]]
            results.append({"strategy": label, "params": params, "rows": len(rows),
                            "bytes": len(body), "sample_names": names})
            log(f"  {label:32} -> {len(rows):5} row(s)  {names}")
        except (ApiError, ValueError) as exc:
            results.append({"strategy": label, "params": params, "error": str(exc)})
            log(f"  {label:32} -> error: {str(exc)[:110]}")

    print(json.dumps({"term": args.term, "field": args.field,
                      "control": control, "trials": results}, indent=2, default=str))

    # Read the outcome back to the user.
    by_label = {r["strategy"]: r for r in results}
    ctrl = by_label.get("CONTROL: exact real trade name")
    log("")
    if ctrl and ctrl.get("rows"):
        log("The filter works: an exact trade name from the dataset matched.")
        hits = [r["strategy"] for r in results
                if r.get("rows") and not r["strategy"].startswith("CONTROL")]
        if hits:
            log(f"Forms that also returned rows: {', '.join(hits)}")
        else:
            log(f"No form of {args.term!r} matched, so it is probably not registered "
                "under that trade name. Try --field DEVICE_NAME, or search the "
                "manufacturer with the actors command.")
    elif ctrl:
        log("Even an exact trade name taken from the API matched nothing, so the "
            "problem is the filter mechanism rather than your search term.")
    odata = [r["strategy"] for r in results
             if r["strategy"].startswith("OData") and r.get("rows")]
    if odata:
        log(f"OData options appear to work: {', '.join(odata)} - that gives "
            "pagination and substring search beyond what the spec documents.")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="eudamed",
        description="Query the official EUDAMED Public API v1.0 "
                    "(api.datalake.sante.service.ec.europa.eu).",
        epilog="A subscription key is required for the live API. "
               "Run 'python3 -m eudamed.fakeserver' to test without one.")
    parser.add_argument("--version", action="version",
                        version=f"eudamed {__import__('eudamed').__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    search = subs.add_parser("search", help="search devices by trade name (/udi)")
    search.add_argument("--trade-name", help="a single device name to look for")
    search.add_argument("--country", help="expected manufacturer country ISO2, e.g. DE")
    search.add_argument("--input", help="CSV of devices: name,description,ca,country,keys,broad")
    search.add_argument("--out", default="eudamed_results", help="output directory")
    search.add_argument("--fields", default="TRADE_NAME",
                        help="comma-separated /udi parameters to search each term against, "
                             "e.g. TRADE_NAME,DEVICE_NAME (default TRADE_NAME)")
    search.add_argument("--top", type=int, default=5, help="candidates kept per device")
    search.add_argument("--min-score", type=float, default=0.45,
                        help="discard candidates below this score (default 0.45)")
    search.add_argument("--no-resolve-codes", dest="resolve_codes", action="store_false",
                        help="skip the /reference call that turns numeric ids into codes")
    search.add_argument("--language", default="en", help="language for /reference labels")
    search.add_argument("--cache", help="match against a local cache built by `scan` "
                                        "instead of querying the API per name. Required "
                                        "for approximate names, since /udi filters are "
                                        "exact-match")
    search.add_argument("--page-size", type=int, default=100,
                        help="rows per page (ui backend only)")
    search.add_argument("--max-pages", type=int, default=5,
                        help="pages to walk per query (ui backend only)")
    search.add_argument("--keep-raw", action="store_true",
                        help="include each raw API row in results.json")
    add_common(search)
    search.set_defaults(func=cmd_search)

    actors = subs.add_parser("actors", help="look up actors (/actors)")
    actors.add_argument("--name")
    actors.add_argument("--actor-id")
    actors.add_argument("--actor-type")
    actors.add_argument("--country")
    add_common(actors)
    actors.set_defaults(func=cmd_actors)

    ref = subs.add_parser("reference", help="dump the reference code table (/reference)")
    ref.add_argument("--id")
    ref.add_argument("--code")
    ref.add_argument("--language", default="en")
    ref.add_argument("--out", help="write the raw response to this file")
    ref.add_argument("--tables", action="store_true",
                     help="summarise the code tables (CODE values) instead of the rows")
    add_common(ref)
    ref.set_defaults(func=cmd_reference)

    probe = subs.add_parser(
        "probe", help="discover the real response field names (run this first)")
    probe.add_argument("--trade-name", default="MindDoc")
    probe.add_argument("--actor-name", default="")
    probe.add_argument("--language", default="en")
    probe.add_argument("--raw-dir", help="save raw response bodies here")
    add_common(probe)
    probe.set_defaults(func=cmd_probe)

    ui = subs.add_parser(
        "serve", help="open a local web UI to search any name interactively")
    ui.add_argument("--port", type=int, default=8100)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--input", help="CSV of devices to offer in the UI as a "
                                    "clickable list and a 'Run all' batch")
    ui.add_argument("--cache", help="preload a cache built by `scan`, so the UI can match "
                                    "approximate names straight away")
    ui.add_argument("--no-open", dest="open_browser", action="store_false",
                    help="do not open a browser automatically")
    add_common(ui)
    ui.set_defaults(func=cmd_serve)

    ft = subs.add_parser(
        "filtertest", help="work out how /udi filtering behaves (exact? wildcards? OData?)")
    ft.add_argument("--term", default="MindDoc", help="the term to try")
    ft.add_argument("--field", default="TRADE_NAME",
                    help="the /udi parameter to filter on (default TRADE_NAME)")
    add_common(ft)
    ft.set_defaults(func=cmd_filtertest)

    raw = subs.add_parser(
        "raw", help="GET an operation with arbitrary query parameters")
    raw.add_argument("path", help="operation path, e.g. /udi")
    raw.add_argument("--param", action="append", default=[], metavar="K=V",
                     help="a query parameter; repeatable. Not restricted to the spec")
    raw.add_argument("--out", help="write the raw response body to this file")
    raw.add_argument("--rows", type=int, default=3, help="sample rows to print")
    add_common(raw)
    raw.set_defaults(func=cmd_raw)

    dis = subs.add_parser(
        "discover", help="find devices by filter (risk class, EMDN, purpose) not by name")
    dis.add_argument("--risk-class", help="e.g. I, IIa, IIb, III - resolved via /reference")
    dis.add_argument("--risk-class-id", type=int, help="numeric RISK_CLASS_ID directly")
    dis.add_argument("--nomenclature", help="EMDN / NOMENCLATURE_CODE, e.g. Z12")
    dis.add_argument("--medical-purpose", help="MEDICAL_PURPOSE filter")
    dis.add_argument("--device-name", help="DEVICE_NAME filter")
    dis.add_argument("--trade-name", help="TRADE_NAME filter")
    dis.add_argument("--mf-srn", help="MF_SRN filter")
    dis.add_argument("--legislation-id", type=int, help="APPLICABLE_LEGISLATION_ID")
    dis.add_argument("--keyword", help="comma-separated words to require in the returned "
                                       "rows, applied locally for concepts the API "
                                       "cannot filter on")
    dis.add_argument("--out", default="eudamed_discover", help="output directory")
    dis.add_argument("--top", type=int, default=200, help="maximum devices to report")
    dis.add_argument("--no-resolve-codes", dest="resolve_codes", action="store_false")
    dis.add_argument("--language", default="en")
    dis.add_argument("--keep-raw", action="store_true")
    add_common(dis)
    dis.set_defaults(func=cmd_discover)

    sc = subs.add_parser(
        "scan", help="cache /udi rows locally, so approximate names can be matched")
    sc.add_argument("--out", default="cache/udi.jsonl", help="cache file to write")
    sc.add_argument("--filter", action="append", default=[], metavar="K=V",
                    help="a documented /udi filter; repeatable, one partition each")
    sc.add_argument("--partition-by",
                    help="a coded /udi field to fan out over, e.g. RISK_CLASS_ID; "
                         "values come from /reference")
    sc.add_argument("--language", default="en")
    add_common(sc)
    sc.set_defaults(func=cmd_scan)

    return parser


NAME_DRIVEN = {"search", "serve"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "backend", None) is None:
        # A name search is useless against exact-match filters, so those
        # commands default to the UI backend; the rest use documented filters
        # and default to the documented API.
        args.backend = "ui" if args.command in NAME_DRIVEN else "datalake"
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_ERROR
    except (ApiError, ValueError, OSError) as exc:
        log(f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
