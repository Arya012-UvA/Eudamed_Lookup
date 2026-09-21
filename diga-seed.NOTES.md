# `diga-seed.csv` — provenance and how to refresh it

## What this file is

A seed list of **DiGA** — *Digitale Gesundheitsanwendungen*, the digital health
applications that German statutory health insurance reimburses. BfArM keeps the
authoritative register, the DiGA-Verzeichnis, at <https://diga.bfarm.de>.

DiGA are a good seed list for this tool because they are *by definition* medical
device software: a DiGA must be a CE-marked class I or IIa medical device, so
every entry should appear in EUDAMED. That makes the directory a ready-made
answer to "which software devices should I be looking for?" — and 24 of the 47
rows here are psychological or psycho-oncological indications.

Feed it in exactly like the hand-made list:

```bash
python3 -m eudamed search --input diga-seed.csv --software-only --out diga
```

## Provenance — read this before quoting the file

**These names were compiled from the model's own knowledge, not fetched from
BfArM.** The sandbox this was written in blocks all EU hosts, so
`diga.bfarm.de` could not be read (`CONNECT` returns 403). Consequences:

- Some names may be **misspelled**, may use a **superseded product name**, or
  may name an app that has since been **delisted** — the directory changes as
  applications are added and removed.
- The list is **not complete**. It is a starting point, not the register.
- Nothing here has been checked against EUDAMED either.

A wrong name is cheap rather than dangerous: the search simply reports *not
found* for that row, costing a couple of requests. The risk runs the other way
— do **not** cite this file as evidence that something is or is not a DiGA.
Check <https://diga.bfarm.de> for that.

## Refreshing it

The directory has no public bulk export that this tool can rely on, so:

1. Open <https://diga.bfarm.de> and list the directory.
2. For each entry, add a row: `name` (ASCII, for tidy filenames and shell use),
   `description` (indication), and `keys` — the **exact** registered spelling,
   umlauts and all, plus any shorter variants, separated by `|`.
3. Put the manufacturer or a short stem of the product name in `broad`. Those
   terms are never scored; they only widen *querying*, and they are what the
   substring fallback probes when exact matching finds nothing.

### Two deliberate choices in the file

- **`name` is ASCII, `keys` is not.** `Oviva Direkt fuer Adipositas` is the
  row's label; `Oviva Direkt für Adipositas` is what gets sent to the API.
  The register holds the umlaut, so the query must too.
- **`country` is empty throughout.** It is the *expected manufacturer* country,
  and a mismatch costs 0.10 of score. Several DiGA manufacturers are not
  German (Vitadio is Czech, Oviva is Swiss), so guessing `DE` across the board
  would penalise correct matches. Fill it in per row only where you know it.
