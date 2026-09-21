"""Command line interface for the EUDAMED Public API v1.0."""

import argparse
import json
import os
import sys
import threading

from . import config
from .client import ApiError, AuthError, Client
from .fields import describe_keys
from .records import Actor, Device
from .reference import Reference
from .report import write_all
from .search import Target, load_targets, run

EXIT_OK, EXIT_ERROR, EXIT_AUTH, EXIT_USAGE = 0, 1, 2, 3


def log(message, indent=False):
    print(("  " if indent else "") + message, file=sys.stderr, flush=True)


def add_common(parser):
    conn = parser.add_argument_group("connection")
    conn.add_argument("--base", default=None,
                      help=f"API base URL (default ${config.BASE_ENV} or {config.DEFAULT_BASE})")
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
    conn.add_argument("--dry-run", action="store_true",
                      help="print the URLs that would be requested, then exit")
    parser.add_argument("--verbose", "-v", action="store_true")


def make_client(args):
    return Client(base=args.base, key=args.key, auth_mode=args.auth_mode, fmt=args.fmt,
                  delay=args.delay, retries=args.retries, timeout=args.timeout,
                  verbose=args.verbose, api_version=args.api_version)


def require_key(client, args):
    """Fail early and clearly rather than after a 401 per device."""
    if client.key or args.dry_run:
        return True
    log("No subscription key. The EUDAMED Public API requires one "
        f"(--key, or export {config.KEY_ENV}=...).")
    log("Get one from https://developer.datalake.sante.service.ec.europa.eu, or test "
        "locally against the bundled stand-in:")
    log("  python3 -m eudamed.fakeserver")
    log("  python3 -m eudamed search --base http://127.0.0.1:8099/eudamed --key dummy "
        "--trade-name MindDoc")
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
                    print(client.build_url("/udi", {param: term}))
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

    meta = {"base": client.base, "fields": args.fields, "format": args.fmt,
            "requests": client.request_count, "api_version": args.api_version,
            "reference_loaded": bool(reference and reference._by_id),
            "tool_version": __import__("eudamed").__version__}
    paths = write_all(results, args.out, meta)

    tally = {s: sum(r["status"] == s for r in results)
             for s in ("found", "possible", "not found")}
    log("")
    log(f"{tally}  in {client.request_count} request(s)")
    for kind in ("json", "csv", "html"):
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
        if args.raw_dir:
            os.makedirs(args.raw_dir, exist_ok=True)
            name = path.strip("/").replace("/", "_") + (".json" if args.fmt == "json" else ".csv")
            with open(os.path.join(args.raw_dir, name), "w", encoding="utf-8") as handle:
                handle.write(body)
            log(f"  raw -> {os.path.join(args.raw_dir, name)}")

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    udi = report.get("/udi", {})
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

    server = make_server(client, port=args.port, host=args.host, verbose=args.verbose)
    url = f"http://{args.host}:{args.port}"
    log(f"EUDAMED search UI on {url}")
    log(f"  querying {client.base}")
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
    ui.add_argument("--no-open", dest="open_browser", action="store_false",
                    help="do not open a browser automatically")
    add_common(ui)
    ui.set_defaults(func=cmd_serve)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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
