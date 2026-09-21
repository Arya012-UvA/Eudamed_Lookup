# Eudamed_Lookup

Searches the EU [EUDAMED](https://ec.europa.eu/tools/eudamed) device database for a
list of devices by trade name, scores each hit against the name you were looking for,
and writes a JSON, CSV and standalone HTML report.

Built for the case where you have a spreadsheet of device names (e.g. reimbursable
digital health applications) and need to know which of them are actually registered
in EUDAMED, under which manufacturer, risk class and legislation.

## Requirements

Python 3.9+. No third-party runtime dependencies — standard library only.
Running the tests needs `pytest`.

## Usage

Search the built-in device list (23 mostly German DiGA-style devices):

```bash
python3 eudamed_lookup.py
```

Search your own list:

```bash
python3 eudamed_lookup.py --input devices.example.csv --out results
```

Outputs land in `--out` (default `eudamed_results/`):

| File | Contents |
| --- | --- |
| `results.json` | Full result per device: every candidate, every query run, every error |
| `results.csv` | One row per device — the best match, flattened |
| `report.html` | Self-contained browsable report; click a row to expand all candidates |

### Input CSV format

```csv
name,description,ca,country,keys,broad
deprexis,Software for depression,Hamburg DE,DE,deprexis|Deprexis 2,GAIA
```

- `name` — the device as you know it; used as the report label and as a fallback search key
- `description`, `ca` — free text, carried through to the report
- `country` — ISO2 code of the expected manufacturer; matching SRN prefix adds to the
  score, a conflicting one subtracts
- `keys` — `|`-separated precise search terms (spelling variants)
- `broad` — `|`-separated wider terms (e.g. manufacturer name), searched for recall

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--input` | built-in list | Input CSV |
| `--out` | `eudamed_results` | Output directory |
| `--no-details` | off | Skip the per-device detail and basic-UDI calls (much faster, fewer fields) |
| `--delay` | `0.8` | Seconds to wait before each request |
| `--retries` | `4` | Attempts per request, with exponential backoff |
| `--timeout` | `60` | Per-request timeout in seconds |
| `--page-size` | `100` | Results per search page |
| `--max-pages` | `3` | Maximum pages to walk per search term |
| `--top` | `5` | Candidates to keep per device |
| `--min-score` | `0.45` | Discard candidates scoring below this |
| `--verbose` | off | Log every request to stderr |

`EUDAMED_BASE` overrides the API base URL.

## How matching works

Each candidate is scored 0–1 against the device's `keys`:

| Condition | Score |
| --- | --- |
| Trade name equals the key (punctuation/case/accents ignored) | `1.00` |
| Key appears inside the trade name | `0.92` |
| Trade name appears inside the key | `0.80` |
| Otherwise | `0.85 ×` best of fuzzy ratio / token overlap |

Then `+0.05` if the manufacturer SRN country matches `country`, `-0.10` if it conflicts.
The result is bucketed: `found` at ≥0.85, `possible` at ≥0.60, else `not found`.

Scores are a ranking aid, not a verdict — always confirm a match via the EUDAMED
link in the report before relying on it.

## Tests

```bash
pytest test_eudamed_lookup.py
```

22 tests cover the scoring and normalisation helpers, the HTTP client's retry and
pagination behaviour, CSV loading, error handling, and the JSON/CSV/HTML writers
(including HTML injection escaping). The HTTP layer is mocked, so the suite needs
no network access.
