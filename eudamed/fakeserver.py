"""A local stand-in for the EUDAMED Public API, for testing without a key.

It mimics the parts of the contract the spec does pin down - required
``format``, the subscription key, SCREAMING_SNAKE query parameters, the three
operations - and serves a small fixture set. It deliberately does NOT invent a
response schema: it echoes the spec's own query-parameter names as response
field names, which is the most likely real shape and exactly what the
shape-tolerant field mapping is built to handle.

    python3 -m eudamed.fakeserver --port 8099

Then point the CLI at it:

    python3 -m eudamed search --base http://127.0.0.1:8099/eudamed \
        --key dummy --trade-name MindDoc
"""

import argparse
import csv
import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config

DEVICES = [
    {"PRIMARY_DI": "04260703120019", "BASIC_UDI": "426070312MINDDOC01",
     "TRADE_NAME": "MindDoc", "DEVICE_NAME": "MindDoc depression therapy software",
     "DEVICE_MODEL": "", "REFERENCE": "MD-1", "NOMENCLATURE_CODE": "Z12010203",
     "RISK_CLASS_ID": 2, "APPLICABLE_LEGISLATION_ID": 1, "PLACED_ON_THE_MARKET_ID": 1,
     "SPECIAL_DEVICE_TYPE_ID": 1, "MF_SRN": "DE-MF-000025123",
     "MF_NAME": "MindDoc Health GmbH", "MEDICAL_PURPOSE": "Treatment of depression",
     "UUID": "11111111-1111-1111-1111-111111111111", "LATEST_VERSION": True,
     "VERSION_NUMBER": 3},
    # Same manufacturer, unrelated trade name: the manufacturer-only lead that
    # the predecessor script reported as "found".
    {"PRIMARY_DI": "04260703120026", "BASIC_UDI": "426070312MOODPATH1",
     "TRADE_NAME": "Moodpath", "DEVICE_NAME": "Moodpath mood tracking",
     "DEVICE_MODEL": "", "REFERENCE": "MP-1", "NOMENCLATURE_CODE": "Z12010203",
     "RISK_CLASS_ID": 1, "APPLICABLE_LEGISLATION_ID": 1, "PLACED_ON_THE_MARKET_ID": 1,
     "SPECIAL_DEVICE_TYPE_ID": 1, "MF_SRN": "DE-MF-000025123",
     "MF_NAME": "MindDoc Health GmbH", "MEDICAL_PURPOSE": "Mood assessment",
     "UUID": "22222222-2222-2222-2222-222222222222", "LATEST_VERSION": True,
     "VERSION_NUMBER": 1},
    {"PRIMARY_DI": "04260703120033", "BASIC_UDI": "426070312KALMEDA01",
     "TRADE_NAME": "Kalmeda", "DEVICE_NAME": "Kalmeda tinnitus therapy app",
     "DEVICE_MODEL": "", "REFERENCE": "KA-1", "NOMENCLATURE_CODE": "Z12010299",
     "RISK_CLASS_ID": 1, "APPLICABLE_LEGISLATION_ID": 1, "PLACED_ON_THE_MARKET_ID": 1,
     "SPECIAL_DEVICE_TYPE_ID": 1, "MF_SRN": "DE-MF-000099001",
     "MF_NAME": "mynoise GmbH", "MEDICAL_PURPOSE": "Tinnitus therapy",
     "UUID": "33333333-3333-3333-3333-333333333333", "LATEST_VERSION": True,
     "VERSION_NUMBER": 2},
    {"PRIMARY_DI": "08594213450017", "BASIC_UDI": "859421345VITADIO1",
     "TRADE_NAME": "Vitadio", "DEVICE_NAME": "Vitadio diabetes therapy",
     "DEVICE_MODEL": "", "REFERENCE": "VI-1", "NOMENCLATURE_CODE": "Z12010204",
     "RISK_CLASS_ID": 2, "APPLICABLE_LEGISLATION_ID": 1, "PLACED_ON_THE_MARKET_ID": 1,
     "SPECIAL_DEVICE_TYPE_ID": 1, "MF_SRN": "CZ-MF-000077001",
     "MF_NAME": "Vitadio s.r.o.", "MEDICAL_PURPOSE": "Type 2 diabetes therapy",
     "UUID": "44444444-4444-4444-4444-444444444444", "LATEST_VERSION": True,
     "VERSION_NUMBER": 1},
]

ACTORS = [
    {"ACTOR_ID": "DE-MF-000025123", "NAME": "MindDoc Health GmbH",
     "ABBREVIATED_NAME": "MindDoc", "ACTOR_TYPE": "MANUFACTURER",
     "ACT_COUNTRY_ISO2_CODE": "DE", "CA_NAME": "BfArM", "CA_ACTOR_ID": "DE-CA-001"},
    {"ACTOR_ID": "DE-MF-000099001", "NAME": "mynoise GmbH", "ABBREVIATED_NAME": "mynoise",
     "ACTOR_TYPE": "MANUFACTURER", "ACT_COUNTRY_ISO2_CODE": "DE",
     "CA_NAME": "BfArM", "CA_ACTOR_ID": "DE-CA-001"},
]

REFERENCE = [
    # Real /reference shape, confirmed against the live API: the table is keyed
    # by (CODE, ID) and VALUE holds the label. CODE names the code table and
    # matches a numeric /udi query parameter. IDs arrive as JSON numbers and
    # may be negative.
    {"ID": 1.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class I"},
    {"ID": 2.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class IIa"},
    {"ID": 3.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class IIb"},
    {"ID": 4.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class III"},
    # The same ids mean different things in a different table - which is why a
    # lookup keyed on ID alone produces wrong labels.
    {"ID": 1.0, "CODE": "APPLICABLE_LEGISLATION_ID", "LANGUAGE": "en",
     "VALUE": "Regulation (EU) 2017/745"},
    {"ID": 2.0, "CODE": "APPLICABLE_LEGISLATION_ID", "LANGUAGE": "en",
     "VALUE": "Regulation (EU) 2017/746"},
    {"ID": 1.0, "CODE": "SPECIAL_DEVICE_TYPE_ID", "LANGUAGE": "en", "VALUE": "None"},
    {"ID": -101.0, "CODE": "PLACED_ON_THE_MARKET_ID", "LANGUAGE": "en", "VALUE": "Israel"},
    {"ID": 1.0, "CODE": "PLACED_ON_THE_MARKET_ID", "LANGUAGE": "en", "VALUE": "Germany"},
    {"ID": 2.0, "CODE": "PLACED_ON_THE_MARKET_ID", "LANGUAGE": "en", "VALUE": "Czechia"},
]

TABLES = {"/udi": (DEVICES, config.UDI_PARAMS),
          "/actors": (ACTORS, config.ACTOR_PARAMS),
          "/reference": (REFERENCE, config.REFERENCE_PARAMS)}


def filter_rows(rows, params, allowed):
    """Case-insensitive substring filtering, which is how the EUDAMED UI behaves."""
    out = rows
    for name, values in params.items():
        if name in ("format", "api-version", config.KEY_QUERY):
            continue
        if name not in allowed:
            return None, f"Unknown parameter {name}"
        needle = (values[0] or "").lower()
        out = [r for r in out if needle in str(r.get(name, "")).lower()]
    return out, None


class Handler(BaseHTTPRequestHandler):
    require_key = True
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _send(self, status, body, ctype="application/json"):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, message):
        self._send(status, json.dumps({"Error": message}))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = re.sub(r"^/eudamed", "", parsed.path) or "/"
        params = parse_qs(parsed.query, keep_blank_values=True)

        if self.require_key:
            key = (self.headers.get(config.KEY_HEADER)
                   or (params.get(config.KEY_QUERY) or [""])[0])
            if not key:
                self._error(401, "Access denied due to missing subscription key.")
                return

        if path not in TABLES:
            self._error(404, f"Resource not found: {path}")
            return

        fmt = (params.get("format") or [""])[0]
        if not fmt:
            self._error(400, "Required query parameter 'format' is missing.")
            return
        if fmt not in config.FORMATS:
            self._error(400, f"format must be one of {list(config.FORMATS)}.")
            return

        table, allowed = TABLES[path]
        rows, problem = filter_rows(table, params, allowed)
        if problem:
            self._error(400, problem)
            return

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(table[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            self._send(200, buf.getvalue(), "text/csv")
        else:
            self._send(200, json.dumps(rows))


def serve(port=8099, host="127.0.0.1", require_key=True, verbose=False):
    Handler.require_key = require_key
    server = ThreadingHTTPServer((host, port), Handler)
    server.verbose = verbose
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-key", action="store_true", help="do not require a subscription key")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    server = serve(args.port, args.host, require_key=not args.no_key, verbose=not args.quiet)
    base = f"http://{args.host}:{args.port}/eudamed"
    print(f"Fake EUDAMED Public API on {base}")
    print(f"  {len(DEVICES)} devices, {len(ACTORS)} actors, {len(REFERENCE)} reference codes")
    print(f"  try: python3 -m eudamed search --base {base} --key dummy --trade-name MindDoc")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
