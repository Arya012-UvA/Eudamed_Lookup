"""Command line interface for the EUDAMED Public API v1.0."""

import argparse
import json
import os
import sys
import threading

from . import config, devicetype
from .client import ApiError, AuthError, Client
from .fields import describe_keys
from .records import Actor, Device
from .reference import Reference
from .report import write_all
from .search import Target, load_targets, manufacturer_results, run, search_manufacturer
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


def make_widen_client(args):
    """The substring-search fallback client, or None when it does not apply.

    Uses getattr throughout because not every subcommand defines every flag -
    `serve` has no --widen-page-size, for instance - mirroring how
    make_client already reads --page-size.
    """
    if getattr(args, "backend", "datalake") != "datalake":
        return None                      # --backend ui is already substring
    if not getattr(args, "widen", True) or getattr(args, "dry_run", False):
        return None
    return UiClient(base=getattr(args, "widen_base", None),
                    delay=args.delay, retries=args.retries, timeout=args.timeout,
                    verbose=args.verbose,
                    page_size=getattr(args, "widen_page_size", 100),
                    max_pages=getattr(args, "widen_max_pages", 2))


def make_typer(args, client, widen_client=None):
    """The device-type classifier, with a detail client when one helps.

    Only built when --software-only is in force, because resolving an
    undetermined device costs one extra request. The web-UI backend's list
    rows carry no nomenclature code, so on that backend the detail lookup is
    what makes the filter work at all rather than classing everything as
    undetermined.
    """
    if not getattr(args, "software_only", False):
        return None
    detail = None
    for candidate in (client, widen_client):
        if hasattr(candidate, "device_detail"):
            detail = candidate
            break
    return devicetype.Typer(detail_client=detail, verbose=args.verbose)


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
    if args.resolve_codes and not client.HAS_REFERENCE:
        # This backend nests its codes inside the rows instead, so there is
        # nothing to resolve and a request would only fail.
        log(f"skipping reference codes - the {args.backend} backend has no "
            "/reference operation; coded fields come from the rows themselves")
    elif args.resolve_codes:
        log("loading reference codes")
        reference = Reference(client, language=args.language, verbose=args.verbose).load()

    widen_client = make_widen_client(args)
    if widen_client is not None:
        log(f"  substring fallback ready ({widen_client.base}) for devices not found")

    typer = make_typer(args, client, widen_client)
    if typer is not None:
        log("  --software-only: keeping devices whose record says software "
            "(EMDN Z12, or a special device type naming software)")
        if typer.detail_client is None:
            log("    no detail endpoint on this backend, so a device with no EMDN code "
                "on its row stays undetermined and is dropped")

    try:
        results = run(client, targets, reference=reference, top=args.top,
                      min_score=args.min_score, fields=args.fields,
                      keep_raw=args.keep_raw, progress=log,
                      widen_client=widen_client,
                      software_only=args.software_only, typer=typer)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH

    meta = {"base": client.base, "backend": args.backend,
            "widen_requests": widen_client.request_count if widen_client else 0,
            "software_only": args.software_only,
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

    if args.software_only:
        other = sum(r.get("dropped_kinds", {}).get(devicetype.OTHER, 0) for r in results)
        unknown = sum(r.get("dropped_kinds", {}).get(devicetype.UNKNOWN, 0) for r in results)
        log("")
        log(f"--software-only dropped {other} candidate(s) whose record says they are "
            f"not software, and {unknown} whose record says nothing either way.")
        if typer is not None and typer.detail_requests:
            log(f"  {typer.detail_requests} detail request(s) resolved rows that carried "
                f"no device type ({typer.detail_errors} failed)")
        if unknown:
            log("  A large 'nothing either way' count means the EMDN code and special "
                "device type were both blank, not that those devices are hardware.")

    mfr = [r["name"] for r in results
           if any(c["matched_on"] == "manufacturer" for c in r["candidates"])]
    if mfr:
        log("")
        log(f"manufacturer-only leads (not counted as matches): {', '.join(mfr)}")
    unresolved = [r["name"] for r in results if r["errors"]]
    if unresolved:
        log(f"devices with request errors: {', '.join(unresolved)}")
    return EXIT_OK


# -------------------------------------------------------- manufacturer
def cmd_manufacturer(args):
    """Every device a named manufacturer has registered.

    /udi cannot filter on a manufacturer *name* - only on MF_SRN - so the name
    is resolved through /actors first. See search.search_manufacturer.
    """
    if not args.name and not args.srn:
        log("give --name 'Company GmbH' or --srn DE-MF-000012345")
        return EXIT_USAGE

    client = make_client(args)
    if not require_key(client, args):
        return EXIT_AUTH

    if args.dry_run:
        if args.srn:
            for srn in args.srn:
                print(client.build_url(client.DEVICE_PATH, {"MF_SRN": srn}))
        else:
            print(client.build_url(client.ACTOR_PATH, {"NAME": args.name}))
        return EXIT_OK

    reference = None
    if args.resolve_codes and client.HAS_REFERENCE:
        log("loading reference codes")
        reference = Reference(client, language=args.language, verbose=args.verbose).load()

    # The exact /actors filter rarely matches a company's full registered
    # name, so the substring backend is the useful path here - more so than
    # for a device name.
    actor_client = make_widen_client(args)
    if actor_client is not None:
        log(f"  substring fallback ready ({actor_client.base}) for the actor lookup")

    typer = make_typer(args, client, actor_client)
    try:
        found = search_manufacturer(
            client, args.name or "", reference=reference, actor_client=actor_client,
            srns=args.srn, top=args.top, software_only=args.software_only,
            keep_raw=args.keep_raw, typer=typer)
    except AuthError as exc:
        log(str(exc))
        return EXIT_AUTH

    if found["actors"]:
        log("")
        log(f"{len(found['actors'])} actor(s):")
        for actor in found["actors"]:
            via = " (via substring search)" if actor.get("matched_via") else ""
            log(f"  {actor['actor_id'] or '(no SRN)'}  {actor['name']}  "
                f"[{actor['actor_type'] or '?'} {actor['country']}]{via}", indent=True)
    elif not args.srn:
        log("")
        log(f"no actor matched {args.name!r}.")
        log("  /actors matches the name exactly on the documented API, so try the "
            "full registered company name, or --backend ui for substring search.")

    if not found["devices"]:
        log("")
        log("no devices found for that manufacturer.")
        for err in found["errors"]:
            log(f"  {err}", indent=True)
        if args.software_only and sum(found["dropped_kinds"].values()):
            log(f"  --software-only dropped {found['dropped_kinds']} - the manufacturer "
                "does have registered devices, but none the record calls software.")
        return EXIT_OK

    results = manufacturer_results(found)
    meta = {"base": client.base, "backend": args.backend,
            "fields": f"NAME={args.name!r} -> MF_SRN={found['srns']}",
            "software_only": args.software_only,
            "format": args.fmt, "requests": client.request_count,
            "api_version": args.api_version,
            "reference_loaded": bool(reference and reference.tables),
            "tool_version": __import__("eudamed").__version__}
    paths = write_all(results, args.out, meta)

    log("")
    log(f"{found['device_count']} device(s) for {len(found['srns'])} SRN(s) "
        f"in {client.request_count} request(s)")
    for entry in found["devices"]:
        log(f"  {entry['trade_name'] or entry['device_name']}  "
            f"[{entry['device_kind']}]  {entry['primary_di']}", indent=True)
    if args.software_only:
        log(f"  --software-only dropped {found['dropped_kinds'][devicetype.OTHER]} "
            f"non-software and {found['dropped_kinds'][devicetype.UNKNOWN]} "
            "undetermined device(s)")
    for kind in ("json", "csv", "md", "html"):
        log(f"  {kind:4} {os.path.abspath(paths[kind])}")
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
        print(client.build_url(client.DEVICE_PATH, {"TRADE_NAME": args.trade_name}))
        return EXIT_OK
    if not require_key(client, args):
        return EXIT_AUTH

    # Paths differ per backend, so ask the client rather than hardcoding the
    # documented API's spellings.
    ops = [(client.DEVICE_PATH, {"TRADE_NAME": args.trade_name}),
           (client.ACTOR_PATH, {"NAME": args.actor_name} if args.actor_name else None)]
    if client.HAS_REFERENCE:
        ops.append(("/reference", {"LANGUAGE": args.language}))
    else:
        log("skipping /reference - this backend has no such operation")
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
    udi = report.get(client.DEVICE_PATH, {})
    if udi.get("rows") == 0 and not args.dry_run:
        log("")
        log(f"{client.DEVICE_PATH} returned no rows - retrying with no filter to "
            "tell apart an empty endpoint from an unmatched filter")
        try:
            rows, body = client.request(client.DEVICE_PATH, {})
            report[f"{client.DEVICE_PATH} (no filter)"] = {
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

    widen_client = make_widen_client(args)
    server = make_server(client, port=args.port, host=args.host,
                         verbose=args.verbose, targets=targets,
                         widen_client=widen_client)
    url = f"http://{args.host}:{args.port}"
    log(f"EUDAMED search UI on {url}")
    log(f"  querying {client.base}  (--backend {args.backend})")
    if targets:
        log(f"  {len(targets)} device(s) loaded from {args.input}")
    if widen_client is not None:
        log(f"  substring fallback ready ({widen_client.base}) for devices not found")
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
        print(client.build_url(client.DEVICE_PATH, params))
        return EXIT_OK

    log(f"querying {client.DEVICE_PATH} with {params}")
    try:
        rows, body = client.request(client.DEVICE_PATH, params)
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

    typer = make_typer(args, client)

    # Optional local keyword narrowing, for concepts the API cannot filter on.
    terms = [t.strip().lower() for t in (args.keyword or "").split(",") if t.strip()]
    kept, devices = [], []
    kinds = {devicetype.OTHER: 0, devicetype.UNKNOWN: 0}
    for row in rows:
        device = Device(row)
        if reference is not None:
            reference.enrich(device)
        devices.append(device)
        if terms:
            haystack = " ".join(str(v) for v in device.to_dict().values()).lower()
            if not any(t in haystack for t in terms):
                continue
        if args.software_only:
            kind, _ = typer.classify(device) if typer else device.kind
            if kind != devicetype.SOFTWARE:
                kinds[kind] = kinds.get(kind, 0) + 1
                continue
        kept.append(device)
    if terms:
        log(f"  {len(kept)} of {len(devices)} row(s) mention {terms}")
    if args.software_only:
        log(f"  --software-only dropped {kinds[devicetype.OTHER]} non-software row(s) "
            f"and {kinds[devicetype.UNKNOWN]} row(s) with no device-type information")

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
    search.add_argument("--fields", default=config.DEFAULT_SEARCH_FIELDS,
                        help="comma-separated /udi parameters to search each term against. "
                             f"Default {config.DEFAULT_SEARCH_FIELDS}: filters are exact, so "
                             "querying several fields is what finds a device whose trade "
                             "name differs from its device name")
    search.add_argument("--top", type=int, default=5, help="candidates kept per device")
    search.add_argument("--min-score", type=float, default=0.0,
                        help="discard candidates below this score (default 0.0). The "
                             "filters are exact, so almost every returned row is a real "
                             "hit; a floor mostly discards good matches")
    search.add_argument("--no-resolve-codes", dest="resolve_codes", action="store_false",
                        help="skip the /reference call that turns numeric ids into codes")
    search.add_argument("--language", default="en", help="language for /reference labels")
    search.add_argument("--no-widen", dest="widen", action="store_false",
                        help="do not fall back to substring search when a device "
                             "is not found. The documented API matches names exactly, "
                             "so the fallback is what finds a device registered under "
                             "a longer name")
    search.add_argument("--widen-base", default=None,
                        help=f"base URL for the substring fallback "
                             f"(default {DEFAULT_UI_BASE})")
    search.add_argument("--widen-page-size", type=int, default=100,
                        help="rows per page for the substring fallback")
    search.add_argument("--widen-max-pages", type=int, default=2,
                        help="pages to walk per term in the substring fallback")
    search.add_argument("--page-size", type=int, default=100,
                        help="rows per page (ui backend only)")
    search.add_argument("--max-pages", type=int, default=5,
                        help="pages to walk per query (ui backend only)")
    search.add_argument("--software-only", action="store_true",
                        help="keep only candidates whose own record says they are software "
                             "(EMDN category Z12, or a special device type naming software). "
                             "Removes hardware noise regardless of the search term")
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
    ui.add_argument("--no-widen", dest="widen", action="store_false",
                    help="do not fall back to substring search when a device is "
                         "not found")
    ui.add_argument("--widen-base", default=None,
                    help=f"base URL for the substring fallback "
                         f"(default {DEFAULT_UI_BASE})")
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
    dis.add_argument("--software-only", action="store_true",
                     help="keep only rows whose record says they are software")
    add_common(dis)
    dis.set_defaults(func=cmd_discover)

    mfr = subs.add_parser(
        "manufacturer",
        help="every device registered by a manufacturer, found from its name")
    mfr.add_argument("--name", help="manufacturer name, e.g. 'GAIA AG'")
    mfr.add_argument("--srn", action="append", default=[], metavar="SRN",
                     help="skip the /actors lookup and use this SRN directly; repeatable")
    mfr.add_argument("--out", default="eudamed_manufacturer", help="output directory")
    mfr.add_argument("--top", type=int, default=200, help="maximum devices to report")
    mfr.add_argument("--software-only", action="store_true",
                     help="keep only devices whose record says they are software")
    mfr.add_argument("--no-widen", dest="widen", action="store_false",
                     help="do not fall back to substring search when the exact "
                          "/actors lookup finds no actor")
    mfr.add_argument("--widen-base", default=None,
                     help=f"base URL for the substring fallback (default {DEFAULT_UI_BASE})")
    mfr.add_argument("--no-resolve-codes", dest="resolve_codes", action="store_false")
    mfr.add_argument("--language", default="en")
    mfr.add_argument("--keep-raw", action="store_true")
    add_common(mfr)
    mfr.set_defaults(func=cmd_manufacturer)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "backend", None) is None:
        # The documented API is the default everywhere. Its filters are exact,
        # but searching several fields at once still finds most devices: a
        # device whose TRADE_NAME is "MindDoc: Your Companion" has DEVICE_NAME
        # "MindDoc", so DEVICE_NAME matches exactly and the local scorer then
        # recognises the trade name. --backend ui is available for genuine
        # substring search.
        args.backend = "datalake"
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
