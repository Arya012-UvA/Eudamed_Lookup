# Eudamed_Lookup

Command-line application for the **official EUDAMED Public API v1.0**
(`api.datalake.sante.service.ec.europa.eu/eudamed`). Give it a list of medical
device names; it tells you which are registered in EUDAMED, under which
manufacturer, risk class and legislation, and writes a JSON, CSV and
self-contained HTML report.

Built against the OpenAPI document vendored at
[`docs/eudamed-public-openapi-v1.0.json`](docs/eudamed-public-openapi-v1.0.json).

```
python3 -m eudamed search --trade-name MindDoc --country DE
```

## Requirements

- Python 3.9+
- **No third-party runtime dependencies** — standard library only
- A **subscription key** for the live API (see below). Tests and the bundled
  fake server need no key.

## Install

```bash
git clone https://github.com/Arya012-UvA/Eudamed_Lookup.git
cd Eudamed_Lookup
pip install -e ".[dev]"     # optional; or just run python3 -m eudamed
```

## Getting a subscription key

The API is published through Azure API Management. The OpenAPI document
declares two credential schemes, and both are supported:

| Scheme | How it is sent | Flag |
| --- | --- | --- |
| `apiKeyHeader` | `Ocp-Apim-Subscription-Key` header | `--auth-mode header` (default) |
| `apiKeyQuery` | `subscription-key` query parameter | `--auth-mode query` |

Register at <https://developer.datalake.sante.service.ec.europa.eu> and
subscribe to *API - EUDAMED Public*. Then:

```bash
export EUDAMED_SUBSCRIPTION_KEY=your-key-here
```

Keys are never printed: they are redacted to `<key>` in all log and error output.

## Commands

### `probe` — run this first

The OpenAPI document declares **no response schemas** (every `200` is
`{"description": ""}`), so the field names in a response are unknown until you
make a real call. `probe` makes one call per operation and reports the actual
field names, whether the record mapping resolved them, and optionally saves the
raw bodies.

```bash
python3 -m eudamed probe --trade-name MindDoc --raw-dir raw/
```

If it reports fields it could not resolve, add the real spellings to
`Device.__init__` in [`eudamed/records.py`](eudamed/records.py) — that is the
only place field names live.

### `serve` — interactive web UI

A search box where you can check **any** name, one at a time, without preparing
a CSV. The subscription key stays in this process and is never sent to the
browser, which also sidesteps the CORS restrictions that block calling the API
directly from a page.

```bash
python3 -m eudamed serve --key YOUR_KEY
# EUDAMED search UI on http://127.0.0.1:8100
```

It opens your browser automatically (`--no-open` to suppress, `--port` to
change the port). Type a name, pick an expected country, press Search. Under
**Options** you can choose which `/udi` fields to search, adjust the minimum
score and the result count, and turn off `/reference` code resolution.

Searching an identifier — `MF_SRN`, `PRIMARY_DI` or `BASIC_UDI` — is treated as
an identifier match rather than a name comparison, so pasting an SRN lists
every device that manufacturer has registered.

### `search` — the main command

```bash
# one device
python3 -m eudamed search --trade-name MindDoc --country DE

# a list, searching both trade name and device name
python3 -m eudamed search --input devices.example.csv --out results \
    --fields TRADE_NAME,DEVICE_NAME
```

Outputs land in `--out` (default `eudamed_results/`):

| File | Contents |
| --- | --- |
| `results.json` | Everything: all candidates, every query, every error, run metadata |
| `results.csv` | One row per device — the best match, flattened |
| `report.html` | Browsable report; click a row for all candidates. No network needed to view |

### `actors`, `reference`

```bash
python3 -m eudamed actors --name "MindDoc Health" --country DE
python3 -m eudamed reference --language en --out reference.json
```

`devices.csv` in the repository root holds 23 German/EU digital-health devices
ready to run:

```bash
python3 -m eudamed search --input devices.csv --out results --fields TRADE_NAME,DEVICE_NAME
```

### Input CSV format

Only `name` is required. Headers are case- and space-insensitive, and a UTF-8 BOM is fine.

```csv
name,description,ca,country,keys,broad
MindDoc,Software for psychological diseases,Bavaria DE,DE,MindDoc,DE-MF-000025123
```

| Column | Meaning |
| --- | --- |
| `name` | The device as you know it. Report label, and fallback search key |
| `description`, `ca` | Free text, carried through to the report |
| `country` | Expected manufacturer ISO2. A matching SRN prefix adds to the score, a conflicting one subtracts |
| `keys` | `\|`-separated precise search terms (spelling variants). **Only these are scored** |
| `broad` | `\|`-separated wider terms (e.g. an SRN) — searched for recall, not scored |

### Key options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--fields` | `TRADE_NAME` | Which `/udi` parameters to search each term against |
| `--top` | `5` | Candidates kept per device |
| `--min-score` | `0.45` | Discard candidates below this |
| `--format` | `json` | `json` or `csv` — the API supports both |
| `--auth-mode` | `header` | Where to put the subscription key |
| `--no-resolve-codes` | off | Skip the `/reference` call that turns numeric ids into codes |
| `--dry-run` | off | Print the URLs that would be requested, then exit. Needs no key |
| `--delay` | `0.2` | Minimum seconds between requests |
| `--retries` | `4` | Attempts per request, with exponential backoff |
| `--keep-raw` | off | Include each raw API row in `results.json` |
| `-v` | off | Log every request |

`--fields` only accepts parameters the spec documents for `/udi`; anything else
is rejected locally with the valid list, rather than being silently dropped by
the gateway.

Exit codes: `0` ok, `1` error, `2` auth problem, `3` usage problem.

## How matching works

Every candidate is scored 0–1 against the device's `keys`, and **each score
carries the evidence that produced it** (`matched_on`):

| Evidence | Score |
| --- | --- |
| `trade_name:exact` | `1.00` |
| `trade_name:contains` — key inside the trade name | `0.92` |
| `trade_name:contained_by` — trade name inside the key | `0.80` |
| `device_name:*` | trade-name score × `0.9` |
| `trade_name:fuzzy` | `0.85 ×` best of fuzzy ratio / token overlap, capped `0.84` |
| `identifier:<PARAM>` | `1.00` exact, `0.95` partial — see below |
| `manufacturer` | capped at **`0.55`** |

Then `+0.05` if the manufacturer SRN country matches `country`, `-0.10` if it
conflicts. Buckets: **found** ≥ 0.85, **possible** ≥ 0.60, else **not found**.

### Identifier searches are not name comparisons

When a term is matched against an identifier field (`PRIMARY_DI`, `BASIC_UDI`,
`MF_SRN`, `REFERENCE`, `NOMENCLATURE_CODE`), the API has already matched it —
so scoring the row's *name* against that identifier would give near zero and
discard a correct hit. Such rows score as `identifier:<PARAM>` instead.

This applies only to terms in `keys`. An identifier in `broad` stays a recall
helper, so putting an SRN there does not turn every device from that
manufacturer into a full-confidence match.

### Why manufacturer evidence is capped

A device whose *manufacturer* is named after the product would otherwise be
reported as a confident match. Searching `MindDoc` matched the unrelated device
`Moodpath` at 0.90 (`found`) in the predecessor script, because the manufacturer
is `MindDoc Health GmbH` — so a device that is not on the register was reported
as registered.

Manufacturer-name evidence is now hard-capped below the `possible` threshold and
cannot be lifted over it by the country bonus. Such candidates still appear —
they are genuine leads — flagged `manufacturer` in the CSV, chipped and
warning-boxed in the HTML report, and listed in the run summary. They are never
counted as matches.

**Scores rank candidates; they do not confirm registration.** Always open the
EUDAMED link before relying on a match.

## Testing

### 1. The offline suite — no key, no network

```bash
pytest -q          # 154 tests
ruff check eudamed tests
```

Covers spec conformance (required `format`, `api-version`, `Content-Type`, both
credential schemes, rejection of undocumented parameters), retry and backoff
behaviour, auth handling, response-shape tolerance, scoring — including the
manufacturer-cap regression — CSV input parsing, and all three writers
including HTML injection escaping.

### 2. End to end against the bundled fake API

The repo ships a stand-in that enforces the parts of the contract the spec does
pin down — the required `format`, the subscription key, SCREAMING_SNAKE
parameters, all three operations, JSON and CSV. This is the way to exercise the
whole stack without a key.

```bash
# terminal 1
python3 -m eudamed.fakeserver
# Fake EUDAMED Public API on http://127.0.0.1:8099/eudamed

# terminal 2
BASE=http://127.0.0.1:8099/eudamed

python3 -m eudamed probe  --base $BASE --key dummy --trade-name MindDoc
python3 -m eudamed search --base $BASE --key dummy --trade-name MindDoc --country DE --out /tmp/res
python3 -m eudamed actors --base $BASE --key dummy --name MindDoc
python3 -m eudamed search --base $BASE --key dummy --input devices.example.csv \
        --out /tmp/res2 --fields TRADE_NAME,MF_SRN
open /tmp/res2/report.html
```

The fixture deliberately includes `Moodpath` — same manufacturer as `MindDoc`,
different trade name — so you can see the manufacturer-only lead being capped
rather than reported as found. Expect `MindDoc` at `1.0 trade_name:exact` and
`Moodpath` at `0.55 manufacturer`.

And the interactive UI against the same stand-in:

```bash
# terminal 2, with the fake API still running in terminal 1
python3 -m eudamed serve --base $BASE --key dummy
```

Try `MindDoc` (found), `velibra` (not found), and — with `MF_SRN` ticked under
Options — `DE-MF-000025123`, which lists both of that manufacturer's devices.

Checks that need no server at all:

```bash
python3 -m eudamed search --trade-name MindDoc --dry-run     # prints the exact URL
python3 -m eudamed search --trade-name X --fields tradeName  # rejects UI-API spelling
```

### 3. Against the live API

```bash
export EUDAMED_SUBSCRIPTION_KEY=your-key
python3 -m eudamed probe --trade-name MindDoc --raw-dir raw/ -v
```

Check the reported `/udi` field names against `Device.__init__`, then:

```bash
python3 -m eudamed search --input devices.example.csv --out results -v
```

## Known gaps in the API documentation

These are properties of the published spec, not of this tool:

1. **No response schemas.** Every `200` is `{"description": ""}`, so response
   field names are unverified. Handled by matching field names on a normalised
   key (`TRADE_NAME` ≡ `tradeName` ≡ `trade_name`) and by `probe`.
2. **No pagination, anywhere.** No `page`, `pageSize`, `limit` or `offset` on any
   operation. How the API caps large result sets is unknown; a broad search may
   be silently truncated. `results.json` records the row count per query so a
   suspicious round number is visible.
3. **`/reference` has no code-table column.** Only `ID`, `CODE`, `LANGUAGE`. If
   risk-class id 1 and legislation id 1 are different things, a flat `ID → CODE`
   map mislabels fields. This tool therefore **refuses to resolve an id that maps
   to more than one code**, reports the ambiguity, and shows the numeric id
   instead of guessing.
4. **`api-version` is undeclared** as a parameter, though the portal's own
   request template requires it and `servers.url` is unversioned. Sent by
   default; `--api-version ''` omits it.
5. **No `UUID` is guaranteed.** The EUDAMED device screen needs one for a deep
   link, so when a row has none the report falls back to a UDI-DI search link
   rather than emitting a broken URL.
6. **Only `500` is documented** besides `200`. Error handling is therefore
   defensive: `401`/`403` fail fast with a key-specific message, `400`/`404`
   fail without retrying, `408`/`429`/`5xx` retry with backoff and honour
   `Retry-After`.

## Layout

```
eudamed/
  config.py       spec facts: base URL, operations, documented parameters, auth schemes
  client.py       HTTP: auth, required params, retries, error classification
  fields.py       shape-tolerant response access (unknown schemas)
  records.py      Device / Actor mapping   <- field-name spellings live here
  reference.py    /reference id -> code resolution, ambiguity-safe
  matching.py     normalisation and typed-evidence scoring
  search.py       orchestration: targets -> ranked candidates
  report.py       JSON / CSV / HTML writers
  cli.py          argparse CLI: search, actors, reference, probe, serve
  webui.py        local web UI: search box, server-side key, JSON endpoints
  fakeserver.py   local stand-in for testing without a key
tests/            154 tests, no network required
docs/             vendored OpenAPI document
legacy/           the original UI-backend script (see legacy/README.md)
```

## Legacy script

The original single-file `eudamed_lookup.py` now lives in
[`legacy/`](legacy/README.md). It calls the undocumented EUDAMED **web-UI
backend** rather than this API, needs no key, and has the manufacturer
false-positive described above. Kept for reference and for the no-key case.
