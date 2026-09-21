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

### Do you actually need one? No.

**Verified against the live API: no subscription key is required.** Anonymous
requests are answered:

```
GET /udi?TRADE_NAME=MindDoc&format=json&api-version=v1.0   -> 200
GET /reference?LANGUAGE=en&format=json&api-version=v1.0    -> 200, 294 rows
```

The `security` block in the OpenAPI document is an Azure APIM portal artefact
and does not reflect what the gateway enforces. Running without a key is
therefore the default; `--require-key` opts back in to a strict pre-flight
check, and `--key` still works if you have one.

If a request does fail, distinguish the cause:

| Response | Meaning |
| --- | --- |
| `401` / `403` from the API | A key is being enforced after all |
| `404` | Wrong path — check `--base` ends in `/eudamed` |
| `Tunnel connection failed` / proxy error | A proxy on your network refused it. **Not** the API rejecting a key |

## Commands

`probe` first, then `search` for names you have, `manufacturer` for a company's
whole catalogue, and `discover` or the substring sweep for devices you cannot
name. `serve` puts the first three in a browser.

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

Three panels, matching the commands below: the name search at the top,
**Search by manufacturer** (name → SRN → devices), and **Discover by filter**.
The `Software only` option under *Options* applies to all three.

```bash
python3 -m eudamed serve --input devices.csv
# EUDAMED search UI on http://127.0.0.1:8100
#   querying https://api.datalake.sante.service.ec.europa.eu/eudamed
```

No `--base` means the live API, and no key is needed. Pass `--base` only to
point at the bundled stand-in for offline testing.

It opens your browser automatically (`--no-open` to suppress, `--port` to
change the port). Type a name, pick an expected country, press Search. Under
**Options** you can choose which `/udi` fields to search, adjust the minimum
score and the result count, and turn off `/reference` code resolution.

**Load your own list into the UI** with `--input`:

```bash
python3 -m eudamed serve --input devices.csv
```

The page then shows every device as a clickable chip, autocompletes the search
box from the list, and adds a **Run all** button that works through the whole
list with a progress bar and a sortable summary table — click any row for full
detail.

This matters for recall. Clicking a name from the list searches **all** of that
device's `keys` and `broad` terms, so `HelloBetter Stress und Burnout` runs
three queries (the full name, `HelloBetter Stress`, and the broad term
`HelloBetter`). Typing `HelloBetter` into the box by hand runs exactly one. If a
device has spelling variants, put them in the CSV and pick it from the list.

When `--base` points at localhost the page shows a **Demo mode** banner naming
the four fixture devices, so a correct `not found` for anything else is not
mistaken for a broken UI.

Searching an identifier — `MF_SRN`, `PRIMARY_DI` or `BASIC_UDI` — is treated as
an identifier match rather than a name comparison, so pasting an SRN lists
every device that manufacturer has registered.

**Downloads.** After any search a download bar offers `report.md`, `results.csv`
and `results.json` for that device, plus the whole loaded list as Markdown.
These are produced by the same writers the CLI uses, so a document downloaded
from the browser cannot drift from the command-line one. The link re-queries the
API, so it takes a moment for a long list.

**Discover by filter** is in the UI too, under *Discover by filter*: pick a risk
class (populated from `/reference`, since `RISK_CLASS_ID` is numeric), or give an
EMDN code or medical purpose, plus optional keywords. Keywords narrow the rows
the API returned rather than being sent as a filter, so the UI refuses a
keywords-only search instead of silently returning nothing.

### `discover` — find devices by filter, not by name

For questions like *"all class I software for psychological conditions"*, where
you have no list of trade names to start from:

```bash
python3 -m eudamed discover --risk-class I --keyword depression,anxiety,mental --out psych
```

`--risk-class` takes a human class (`I`, `IIa`, `IIb`, `III`) and resolves it
through `/reference`, since `RISK_CLASS_ID` is numeric. `--legislation-id` and
`--mf-srn` behave likewise.

**The free-text filters are not keyword search.** `--medical-purpose`,
`--device-name`, `--trade-name` and `--nomenclature` are exact whole-string
matches on the datalake backend, like every other free-text `/udi` filter. So
`--medical-purpose depression` returns nothing unless a device's medical
purpose is *literally the single word* "depression". Earlier versions of this
README implied otherwise; that was wrong.

Only two kinds of filter are useful here:

| Kind | Params | Use |
| --- | --- | --- |
| Coded | `--risk-class`, `--legislation-id`, and the raw `*_ID` params | small integer sets, genuinely filterable |
| Free text | `--trade-name`, `--device-name`, `--medical-purpose`, `--nomenclature` | only if you already know the exact string |

`--keyword` is applied **locally** to the rows that come back, for concepts the
API cannot filter on server-side. It narrows, it does not search — anything the
server filter excluded was never retrieved.

If the result comes back at exactly 1000 rows it is **truncated** by the server
cap and the command says so. Narrow the filters rather than treating it as
complete.

### Finding devices you cannot name — the substring sweep

`discover` needs a filter you can state. When you cannot even name the devices
— *"what other apps exist for psychological conditions?"* — the only route is
substring matching on trade names, which means `--backend ui` (the EUDAMED
website's own backend; the documented API cannot do partial matches at all).

`psych-discovery.csv` ships with the repo: sixteen short stems (`depress`,
`anxiet`, `psych`, `mental`, `burnout`, `insomni`, `therap`, …) shaped as an
ordinary input file, so the existing `search` pipeline sweeps them:

```bash
python3 -m eudamed search --input psych-discovery.csv --backend ui \
    --fields TRADE_NAME --top 50 --out psych-discovery
```

Then read `psych-discovery/report.md` and compare against `devices.csv` for
devices you did not already know about.

Two caveats worth keeping:

- **A substring hit proves nothing about indication.** `mind`, `coach` or
  `sleep` will match devices that have nothing to do with mental health. The
  sweep produces *candidates to check* — open each EUDAMED link before
  describing a device as a psychological-health product.
- **`--backend ui` is undocumented** and can change without notice. Use it to
  discover names, then confirm each one against the documented API with a
  normal `search`.

A word-based sweep is the weakest of the three discovery routes, because it
filters on *wording*. Prefer `--software-only` (below) with it, and prefer the
DiGA seed list to guessing stems at all.

### `diga-seed.csv` — the DiGA directory as a seed list

Guessing substrings is a poor way to enumerate therapy apps. A better starting
point already exists: BfArM's **DiGA-Verzeichnis** at <https://diga.bfarm.de>
lists the digital health applications German statutory insurance reimburses,
and a DiGA must be a CE-marked class I or IIa medical device — so every entry
should be in EUDAMED. Twenty-four of the forty-seven rows are psychological or
psycho-oncological.

`diga-seed.csv` ships those names in the same shape as `devices.csv`, so it
feeds straight into the same pipeline:

```bash
python3 -m eudamed search --input diga-seed.csv --software-only \
    --out diga --delay 0.3
```

Each row carries the exact registered spelling in `keys` (umlauts included)
plus shorter variants, and the manufacturer or a product stem in `broad`, which
is what the substring fallback probes when exact matching finds nothing.

Budget for it: 47 rows with variant spellings, two name columns and the
fallback comes to roughly 230 requests, so about a minute at the default
`--delay 0.2`.

**Read [`diga-seed.NOTES.md`](diga-seed.NOTES.md) before quoting the file.**
The names were compiled from the model's own knowledge, not fetched from BfArM
— this sandbox blocks every EU host — so some may be misspelled, superseded or
delisted, and the list is not complete. A wrong name is cheap (that row just
reports *not found*), but do not cite the file as evidence that something is or
is not a DiGA. The notes explain how to refresh it from the directory.

### `--software-only` — filter by device type, not by wording

A name search is blunt: `mind`, `coach` and `sleep` appear in the trade names
of cushions, braces and monitors as readily as in those of therapy apps. This
option throws out candidates whose **own record** does not say they are
software, which removes hardware regardless of what you searched for:

```bash
python3 -m eudamed search --input psych-discovery.csv --backend ui \
    --fields TRADE_NAME --top 50 --software-only --out psych-discovery
```

It works off two fields that are already fetched on every row:

| Field | What it contributes |
| --- | --- |
| `NOMENCLATURE_CODE` (EMDN) | Category **Z12** is medical device software. This is the signal that actually fires. |
| `SPECIAL_DEVICE_TYPE` | Settles it when it names software — but it is usually `None` for software, because in EUDAMED it flags a handful of special cases rather than classifying every device. So it can confirm software and never rules it out. |

Classification is **three-valued**, not two:

- **software** — one of the two fields says so.
- **other** — the EMDN code sits outside the software category, or the special
  device type names something physical.
- **unknown** — both fields are blank. That is not evidence either way, so it
  gets its own bucket rather than being guessed into one of the others.

`--software-only` drops `other` *and* `unknown` — you asked for software — but
it reports the two counts separately, so a sparsely populated field shows up as
a large "says nothing either way" number instead of a quietly shorter result.
Every row also carries `device_kind` and `device_kind_reason` in the JSON, CSV
and Markdown output whether or not you filter, so you can see the call and the
field it rests on.

Two things worth knowing:

- **On `--backend ui` the filter needs an extra request per device.** The
  web-UI backend's list rows carry no EMDN code at all — it lives on the
  per-device detail record. So an undetermined device triggers one detail
  lookup, cached per device for the run. Without that, every row on that
  backend would be undetermined and the filter would empty the result.
- **A filtered-out device is not an unregistered one.** The report says
  *"Found, then filtered out"* with the counts, and the browser verdict reads
  **filtered out** rather than *not found*. The distinction matters as much as
  the `error` / `not found` one.

### `manufacturer` — every device one company registered

`/udi` has no manufacturer-*name* filter. It only has `MF_SRN`, the actor's
registration number, so finding a company's devices is a two-step lookup:
`/actors?NAME=` to resolve the name to SRNs, then `/udi?MF_SRN=` per SRN.
That is what this command does:

```bash
# by name
python3 -m eudamed manufacturer --name "GAIA AG"

# when you already know the SRN, skipping the actor lookup
python3 -m eudamed manufacturer --srn DE-MF-000025123

# only their software
python3 -m eudamed manufacturer --name "GAIA AG" --software-only
```

On the documented API step 1 is an **exact whole-string match on the company's
registered name**, like every other free-text filter. "HelloBetter" will not
find "GET.ON Institut für Online Gesundheitstrainings GmbH". So when the exact
lookup returns no actor, the substring fallback is tried for the *actor* search
too — the same mechanism the name search uses, and more useful here, because
registered company names are long and rarely what you would type. An actor
reached that way is marked `via substring search`.

Devices found this way score 1.0 with evidence `identifier:MF_SRN:<srn>`: the
API matched the registration number, so there is nothing to score for name
similarity. This is also the route to take when a device search comes back
empty — look up the manufacturer, then read their device list.

### `filtertest` — how does `/udi` filtering actually behave?

The spec documents the filter parameters but not their semantics, and a plain
`TRADE_NAME` query can return zero rows while the endpoint holds data. This
tries a battery of forms — exact, case variants, prefixes, `*` and `%`
wildcards, and OData options — and calibrates against a trade name taken from
the API's **own** unfiltered response, so one trial must always match:

```bash
python3 -m eudamed filtertest --term MindDoc
```

If the control matches and your term does not, the device is probably not
registered under that trade name. If even the control fails, the filter
mechanism is at fault, not your term. The two read differently on purpose.

### `raw` — arbitrary parameters

```bash
python3 -m eudamed raw /udi --param '$top=5' --param '$count=true'
python3 -m eudamed raw /udi --out udi.json
```

Unlike the other commands this does **not** restrict parameters to the spec,
which is how the OData options above get tested.

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
| `report.md` | Detailed per-device write-up: every populated field, grouped, plus the queries issued. Print or convert to PDF |
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
| `--fields` | `TRADE_NAME,DEVICE_NAME` | Which `/udi` parameters to search each term against. `BASIC_UDI`, `PRIMARY_DI` and `MF_SRN` are identifiers, available for lookups but not searched by default |
| `--top` | `5` | Candidates kept per device |
| `--min-score` | `0.0` | Discard candidates below this. Exact filters mean a floor mostly discards good matches |
| `--format` | `json` | `json` or `csv` — the API supports both |
| `--auth-mode` | `header` | Where to put the subscription key |
| `--no-resolve-codes` | off | Skip the `/reference` call that turns numeric ids into codes |
| `--require-key` | off | Refuse to run without a key. Off by default: the live API is open |
| `--dry-run` | off | Print the URLs that would be requested, then exit. Needs no key |
| `--delay` | `0.2` | Minimum seconds between requests |
| `--retries` | `4` | Attempts per request, with exponential backoff |
| `--software-only` | off | Keep only candidates whose record says software. See above |
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

### `error` is not `not found`

There is a fourth status. If **every** request for a device failed — the host is
unreachable, the key was rejected, the API is down — the status is `error`, not
`not found`.

That distinction is the point: "the register does not list this device" and "the
register could not be reached" are different answers, and reporting the second
as the first asserts something that was never checked. An `error` row never
promotes a candidate into the CSV's best-match columns, the HTML report counts
it separately and says how many devices could not be checked, and the web UI
shows **could not check** with the underlying failure and what to do about it.

A partial failure — one query erroring while another succeeds and returns
nothing — is a genuine `not found`.

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
pytest -q          # 329 tests
ruff check eudamed tests
```

The stand-in matches filters **exactly and case-insensitively, as the live API
does**. It originally did substring matching, which let tests pass while real
requests returned nothing — a stand-in that behaves unlike the thing it stands
in for is worse than none. `--substring` opts into the old behaviour if you want
to see what substring search would have given.

It also syntax-checks the JavaScript embedded in both HTML pages with `node`
(skipped if node is absent). Each page is one large Python string, so a bad
edit can close a template literal early and yield a page that lints clean,
imports fine and serves a 200 while being broken in the browser.

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
python3 -m eudamed serve --base $BASE --key dummy --input devices.csv
```

Try `MindDoc` (found), `velibra` (not found), and — with `MF_SRN` ticked under
Options — `DE-MF-000025123`, which lists both of that manufacturer's devices.
Then press **Run all** to work through all 23; expect 3 found (`MindDoc`,
`Kalmeda`, `Vitadio` — the only ones in the fixture) and 20 not found.

Remember the stand-in holds **only four devices**. Nothing else can be found
against it, no matter how it is spelled.

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

## Two backends

EUDAMED is served by two different APIs, and they differ in the one way that
matters for searching by name.

| | `ui` | `datalake` (default) |
| --- | --- | --- |
| Host | `ec.europa.eu/tools/eudamed/api` | `api.datalake.sante.service.ec.europa.eu` |
| What it is | the EUDAMED website's own backend | the documented public API |
| Name matching | **substring** | **exact only** |
| Pagination | yes | none |
| Documented | no — can change without notice | yes, OpenAPI |
| Credential | none | none needed in practice |

**`datalake` is the default everywhere**, with an automatic substring fallback
(below), because exact matching turns out not to be the obstacle it looks like. Searching every
name-bearing field at once is what makes it work:

> MindDoc is registered with `TRADE_NAME` = `MindDoc: Your Companion` and
> `DEVICE_NAME` = `MindDoc`. So `TRADE_NAME=MindDoc` misses it, and
> `DEVICE_NAME=MindDoc` matches exactly. The local scorer then sees `MindDoc`
> inside the trade name and reports `trade_name:contains` at 0.97.

That is why `--fields` defaults to `TRADE_NAME,DEVICE_NAME` — the two columns a
product name can appear in — and `--min-score` defaults to `0.0`: filters are exact, so a returned row is almost
always a real hit, and a score floor mostly discards good matches. Against the
real register this configuration finds the large majority of a 23-device list.

### The substring fallback

For the devices exact matching still cannot reach, `search` and `serve`
automatically retry through the `ui` backend — but only for devices the
primary pass did not find, so the cost is bounded:

```
searching "PINK! Coach"
  TRADE_NAME=PINK! Coach   -> 0 rows   (exact, documented API)
  DEVICE_NAME=PINK! Coach  -> 0 rows
  TRADE_NAME=PINK! Coach   -> 0 rows   (substring, ui backend)
  TRADE_NAME=pink coach    -> 1 row    <- found
```

Note the last probe. Substring matching still needs the term to *be* a
substring, and `PINK! Coach` is not contained in
`PINK Coach - Breast Cancer Companion` because of the `!`. So the fallback
also tries the **normalised** form of each term (punctuation collapsed,
lowercased). That matters most in the browser, where a typed name has no
`broad` terms to fall back on.

Every fallback hit is tagged **`matched_via: ui-substring`** in the JSON, CSV
and Markdown, and carries a `found via substring` chip plus a provenance note
in the browser — so a hit from the undocumented backend is never mistaken for
a confirmed exact match on the documented one.

`manufacturer` uses the same fallback for its **actor** lookup, where it earns
its keep even more: registered company names are long and almost never what you
would type.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--no-widen` | fallback on | Turn the fallback off; exact matching only |
| `--widen-base` | the ui backend | Point the fallback at another host |
| `--widen-page-size` | `100` | Rows per page for the fallback |
| `--widen-max-pages` | `2` | Pages per term for the fallback |

`--backend ui` is still available when you want genuine substring search — it is the
only way to find a device whose registered strings all differ from the name you
have:

```bash
python3 -m eudamed search --input devices.csv --out results        # datalake, 5 fields
python3 -m eudamed search --input devices.csv --backend ui         # substring
```

`--backend ui` adds `--page-size` and `--max-pages`, since that backend
paginates. It has no `/reference` operation, so numeric code labels come from
`--backend datalake`.

## Verified API behaviour

Established by probing the live API, none of it in the specification:

| Property | Finding |
| --- | --- |
| Authentication | **None required.** Anonymous requests return `200` |
| Filter matching | **Exact, case-insensitive.** `TRADE_NAME=Mind` returns the device called `MIND` — not a prefix or substring match |
| Wildcards | None. `*` and `%` return nothing |
| Pagination | **None.** `$top`, `$skip`, `$count` → `400 Invalid Query Parameter` |
| Row cap | **1000 rows** per request, with nothing in the response saying it truncated |
| `$filter` | Recognised but **unusable** — see below |
| `/udi` columns | 61, against 13 filterable parameters |
| `/reference` | 294 rows, keyed `(CODE, ID) → VALUE` |

### The exact-match consequence (datalake backend only)

On the documented API a device cannot be found unless you already know its
**exact** registered trade name. The `ui` backend does not have this problem —
prefer it for name searches. `TRADE_NAME=MindDoc` returns nothing whether or not MindDoc is on the
register, so a plain name search cannot distinguish "not registered" from
"registered under a different string".

Two things make a name search work anyway:

1. **Both name columns are queried.** A device registered as
   `MindDoc: Your Companion` has `DEVICE_NAME` `MindDoc`, so the device-name
   filter matches exactly where the trade-name filter does not. The local
   scorer then recognises the trade name and reports `trade_name:contains`.
2. **No score floor.** Exact filters mean a returned row is almost always a
   real hit.

Against the real register that finds the large majority of a 23-device list.
For the remainder, `--backend ui` does genuine substring search.

If you know a device's exact trade name, UDI-DI or Basic UDI-DI, query that
directly; those are exact identifiers.

### A server-side defect in `$filter`

`$filter` is accepted — it is not rejected as an invalid parameter — and its
value reaches an OData parser. But the gateway appends `,1 eq 1`:

```
sent:   $filter=TRADE_NAME eq 'MindDoc'
parsed: TRADE_NAME eq 'MindDoc',1 eq 1
error:  Syntax error at position 24     <- the comma the server added
```

The expression parses correctly up to that comma, and the same happens at
position 31 for `contains(...)`. So `$filter` cannot be used from outside, and
the fault is in their gateway rather than in the request.

## Known gaps in the API documentation

These are properties of the published spec, not of this tool:

1. **No response schemas.** Every `200` is `{"description": ""}`. The real
   `/udi` row has **61 columns**, confirmed by probing the live API — far more
   than the 13 filterable parameters suggest, including `DEVICE_STATUS_TYPE_ID`,
   `AR_NAME`/`AR_SRN`, `SECONDARY_DI`, `UUID` and a long tail of booleans
   (`IMPLANTABLE`, `STERILE`, `REUSABLE`, …). Run `probe` to print them.

   One correction this surfaced: `PLACED_ON_THE_MARKET_ID` resolves to a
   **country** (`Israel`), not a status — the device's market status is the
   separate `DEVICE_STATUS_TYPE_ID` column. They are reported as
   `placed_on_market` and `device_status` respectively.
2. **No pagination, and a 1000-row cap.** No `page`, `pageSize`, `limit` or
   `offset` on any operation. An unfiltered `/udi` request returns **exactly
   1000 rows** (2.5 MB), which for a register of this size is a server-side cap,
   so an unfiltered dump is truncated with nothing in the response saying so.
   Every command warns when a response comes back at exactly 1000 rows.

   The response envelope is `{"value": [...]}` — the OData shape — so `$top`,
   `$skip`, `$count` and `$filter` may work despite being undocumented. Find
   out with `filtertest`, or try one directly:

   ```bash
   python3 -m eudamed raw /udi --param '$skip=1000' --param '$top=5'
   ```
3. **`/reference` returns four columns, not three.** The spec lists `ID`, `CODE`
   and `LANGUAGE` as *query* parameters, which suggested a flat `ID → CODE` map
   with no way to tell code tables apart. The real response has a fourth column
   and a different meaning:

   ```json
   {"ID": -101.0, "CODE": "PLACED_ON_THE_MARKET_ID", "LANGUAGE": "en", "VALUE": "Israel"}
   ```

   `CODE` names the **code table** — and the names match the numeric `/udi`
   query parameters — while `VALUE` holds the label. So the table is keyed by
   `(CODE, ID)`, ids repeat across tables, and a lookup keyed on `ID` alone
   mislabels fields. Inspect the tables with:

   ```bash
   python3 -m eudamed reference --tables
   ```
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
  devicetype.py   is this row software? three-valued, with the detail lookup
  search.py       orchestration: targets -> ranked candidates, manufacturer -> devices
  report.py       JSON / CSV / HTML writers
  cli.py          argparse CLI: search, manufacturer, actors, reference, probe,
                  serve, filtertest, discover, raw
  ui_backend.py   the EUDAMED website's backend: substring search, paginated
  webui.py        local web UI: search box, server-side key, JSON endpoints
  fakeserver.py   local stand-in for testing without a key
tests/            329 tests, no network required
docs/             vendored OpenAPI document (JSON and YAML; same document)
legacy/           the original UI-backend script (see legacy/README.md)

devices.csv          the 23-device working list
diga-seed.csv        DiGA directory as a seed list (see diga-seed.NOTES.md)
psych-devices.csv    the mental-health subset of devices.csv
psych-discovery.csv  16 substring stems for a blind sweep
```

## Legacy script

The original single-file `eudamed_lookup.py` now lives in
[`legacy/`](legacy/README.md). It calls the undocumented EUDAMED **web-UI
backend** rather than this API, needs no key, and has the manufacturer
false-positive described above. Kept for reference and for the no-key case.
