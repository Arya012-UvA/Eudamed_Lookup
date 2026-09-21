# `diga-seed.csv` — where it comes from

## What this file is

A seed list of **DiGA** — *Digitale Gesundheitsanwendungen*, the digital health
applications German statutory health insurance reimburses. BfArM keeps the
authoritative register, the DiGA-Verzeichnis, at <https://diga.bfarm.de>.

DiGA make a good seed list because they are *by definition* medical device
software: a DiGA must be a CE-marked class I or IIa device, so every entry
should appear in EUDAMED. Twenty-four of the forty-seven rows are
psychological or psycho-oncological.

```bash
python3 -m eudamed search --input diga-seed.csv --software-only --out diga
```

## Provenance of the version in git — read this before quoting it

**The rows currently committed were compiled from the model's own knowledge,
not fetched from BfArM.** The sandbox they were written in blocks every EU
host (`diga.bfarm.de` answers 403 to `CONNECT`). Consequences:

- Of the 47 rows, **17 are independently corroborated** by `devices.csv`,
  which the repository owner compiled separately. The other 30 rest on recall
  alone.
- The likely error is not that an app is invented but that its **exact wording
  is wrong** — and wording is what matters here, because the API matches names
  exactly. `Selfapy Depression` and `Selfapy Online-Kurs bei Depression` are
  the difference between a hit and nothing. The `broad` column mitigates this
  through the substring fallback; it does not fix it.
- The list is **not complete**, and entries may have been delisted since.

A wrong name is cheap: that row reports *not found* and costs two requests.
The risk runs the other way — **do not cite this file as evidence that
something is or is not a DiGA.** Check <https://diga.bfarm.de> for that.

## Replacing it with sourced data

Once you run the fetcher, the row provenance stops being recall and the file
gets a generated `diga-seed.provenance.md` recording exactly where it came
from. Either interface works.

### Command line

```bash
# preview only, nothing is written
python3 -m eudamed diga --url https://diga.bfarm.de/de/verzeichnis

# from a page you saved in the browser, or a JSON response from devtools
python3 -m eudamed diga --from saved-page.html --report diga-report.txt

# write, merging into the existing list
python3 -m eudamed diga --from saved-page.html --out diga-seed.csv
```

### Browser

`python3 -m eudamed serve` → **Build a device list**. Paste the names, choose
a saved file, or fetch a URL; then *Use as My list* to search them straight
away, or *Download CSV* to keep the file. Same extractors as the command.

### Expect the plain fetch to find nothing

The directory is most likely a JavaScript application, so a plain `GET`
returns the page shell and the entries arrive afterwards, in the browser.
That is a designed-for outcome, not a bug: the command says so and prints the
alternatives. In rough order of effort:

1. **Save the page.** Open the directory, wait for the list, `Ctrl+S`
   ("Webpage, Complete" or "Single File"), then `--from that-file.html`.
2. **Copy the API response.** `F12` → Network → XHR → reload → click the
   request that returns the list → copy its response into a file, then
   `--from response.json`. Or pass that request's URL to `--url` directly.
3. **Type the names.** A plain list, one per line, optionally
   `Name – indication`. Never mis-parsed, and it is what the shipped rows
   would have come from had the directory been reachable.

If the HTML path returns junk or nothing, `--report FILE` records what the
extractor actually saw — that is the fastest route to getting the selectors
fixed for this site, since they were written without ever seeing its markup.

## Two deliberate encoding choices

- **`name` is ASCII, `keys` is not.** `Oviva Direkt fuer Adipositas` is the
  row's label, for tidy filenames and shell use; `Oviva Direkt für Adipositas`
  is what gets sent to the API, because that is what the register holds. The
  fetcher transliterates properly (`ä→ae`, `ß→ss`) rather than stripping
  accents — `matching.norm()` would turn `für` into `fur` and `Größe` into
  `groe`, which match nothing.
- **`country` is empty throughout.** It is the *expected manufacturer*
  country and a mismatch costs 0.10 of score. Several DiGA manufacturers are
  not German (Vitadio is Czech, Oviva is Swiss), so a blanket `DE` would
  penalise correct matches. Fill it per row only where you know it.

## Refreshing keeps your curation

`--out` **merges** by default: a product already in the file keeps its
hand-tuned `keys` and `broad` terms, only a blank `description` is filled in,
and the diff tells you what the directory added. A product the directory no
longer lists is **kept and reported**, not deleted — a delisted DiGA may still
be registered in EUDAMED, so it stays worth searching. `--replace` discards
all of that and needs `--force`.
