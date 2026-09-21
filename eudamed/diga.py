"""Turn a published directory listing into a search input CSV.

Why this module exists
----------------------
`diga-seed.csv` was originally written from the model's own recall, because
this project's sandbox cannot reach `diga.bfarm.de` (the gateway answers 403
to CONNECT for every EU host). Recall gets product *existence* roughly right
and exact *wording* wrong, which is the worst failure mode here: the EUDAMED
API matches names exactly, so "Selfapy Depression" and "Selfapy Online-Kurs
bei Depression" are the difference between a hit and nothing at all.

This converts the real directory into the `devices.csv` shape on a machine
that can actually reach it.

Why it accepts so many input shapes
-----------------------------------
The markup of the target site was never visible while this was written, so a
single HTML parser would be a guess. Instead every plausible thing a person
can obtain is accepted - a live URL, a page saved from a browser, a JSON
response copied out of devtools, a spreadsheet export, or a plain list of
names typed into a file - and the shape is detected from the content. The
plain-list path is trivial and certain; the HTML path is the speculative one,
so it reports what it saw (see ``Report``) rather than quietly writing junk.

Nothing here is specific to one registry beyond the vocabulary in the key sets
below; the command is named for the use case, not the mechanism.
"""

import csv
import hashlib
import io
import json
import os.path
import re
import time
import unicodedata
import urllib.request
from html.parser import HTMLParser

from .fields import squash_key
from .matching import norm

DEFAULT_URL = "https://diga.bfarm.de/de/verzeichnis"

CSV_COLUMNS = ["name", "description", "ca", "country", "keys", "broad"]

#: German has no ASCII fold that preserves meaning, so it is spelled out.
#: matching.norm() must not be used for this: it *drops* umlauts rather than
#: transliterating them ("fuer" -> "fur") and mangles the eszett
#: ("Groesse" -> "groe"), which would produce wrong names in the output.
TRANSLITERATE = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    "æ": "ae", "Æ": "Ae", "ø": "oe", "Ø": "Oe", "å": "aa", "Å": "Aa",
}

#: Field names an entry's parts may hide behind, matched on squashed keys so
#: camelCase, snake_case and SCREAMING_CASE spellings all resolve.
NAME_KEYS = {"name", "title", "bezeichnung", "produktname", "productname",
             "diganame", "appname", "label", "displayname",
             # EUDAMED's own spellings, so a previous result set can be fed
             # back in as a seed list.
             "tradename", "devicename"}
DESCRIPTION_KEYS = {"indikation", "indikationen", "indication", "description",
                    "medicalpurpose",
                    "beschreibung", "anwendungsgebiet", "anwendungsgebiete",
                    "purpose", "kurzbeschreibung"}
MANUFACTURER_KEYS = {"hersteller", "manufacturer", "anbieter", "company",
                     "vendor", "unternehmen", "firma", "mfname"}

#: Leading words that make a useless substring stem. "Meine Tinnitus App"
#: should widen on "Tinnitus", not on "Meine".
STEM_STOPWORDS = {"meine", "mein", "die", "der", "das", "dein", "deine", "den",
                  "app", "the", "my", "your", "ihre", "ihr", "unser", "unsere"}

#: Boilerplate a wrong HTML selector picks up. A candidate matching any of
#: these is rejected and counted, so a bad selector reads as "47 rejected as
#: boilerplate" instead of 47 navigation links in the output.
BOILERPLATE = (
    "datenschutz", "impressum", "barrierefreiheit", "cookie", "anmelden",
    "startseite", "zur ubersicht", "ubersicht", "kontakt", "suche", "suchen",
    "navigation", "zum inhalt", "menu", "sitemap", "presse", "newsletter",
    "hilfe", "faq", "nutzungsbedingungen", "einwilligung", "weiter", "zuruck",
    "mehr erfahren", "alle akzeptieren", "login", "logout", "english",
    "leichte sprache", "gebardensprache", "erklarung zur",
)

MIN_NAME = 2
MAX_NAME = 80

#: Markup and code, which the plain-list fallback would otherwise accept as a
#: product name. Fetching a JavaScript application returns exactly one short
#: line of HTML, and without this it is offered as a device to search for.
#: No real product name contains any of these.
MARKUP = re.compile(r"""[<>{}]|=["']|=>|\bfunction\s*\(|;\s*}|/\*""")


# --------------------------------------------------------------- normalising
#: Stripped rather than transliterated. NFKD turns U+2122 into the letters
#: "TM", so "HelloBetter Plus(tm)" would become "HelloBetter PlusTM" - a name
#: that matches nothing - unless these go first.
MARKS = "\u2122\u00ae\u00a9\u2120\u2117"


def ascii_name(value):
    """A printable ASCII form of a name, for the `name` column.

    The column labels files and shell arguments, so it must be ASCII; the
    `keys` column carries the original string, which is what reaches the API.
    """
    text = str(value or "").translate({ord(m): None for m in MARKS})
    text = "".join(TRANSLITERATE.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip()


def identity(name):
    """Comparison key for "is this the same product", across spellings."""
    return norm(ascii_name(name))


#: Real product names use these to append an indication, e.g.
#: "Invirto \u2013 Die Therapie gegen Angst". Spelled as escapes so the
#: em/en dashes cannot be mistaken for a typo for "-".
SEPARATORS = ("\u2014", "\u2013", ":", " - ", " | ")


def split_label(value):
    """Split "Name - indication" into its two halves.

    Returns (name, description); description is "" when there is no separator.
    """
    text = str(value or "").strip()
    for sep in (*SEPARATORS, "\t"):
        if sep in text:
            head, _, tail = text.partition(sep)
            if len(head.strip()) >= MIN_NAME:
                return head.strip(), tail.strip()
    return text, ""


def search_keys(name):
    """Query terms for one product name, best first.

    The original string comes first because the register holds it verbatim.
    Shorter variants follow, since the API's filters are exact: a device
    registered under a prefix of its marketing name is only reachable by that
    prefix.
    """
    original = str(name or "").strip()
    keys = []

    def add(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ,;.-|:\u2013\u2014")
        if len(value) >= MIN_NAME and value not in keys:
            keys.append(value)

    add(original)
    plain = ascii_name(original)
    if plain != original:
        # Cheap insurance in case the register stored the name unaccented.
        add(plain)
    head, _ = split_label(original)
    if head != original:
        add(head)
    words = original.split()
    if len(words) >= 3:
        add(" ".join(words[:2]))
    return keys


def broad_term(name, manufacturer=""):
    """A widening term: never scored, only used to widen querying.

    The manufacturer wins when the source supplies one, because it groups a
    whole product family. Otherwise the first meaningful word of the name.
    """
    if manufacturer:
        return re.sub(r"\s+", " ", manufacturer).strip()
    words = [w.strip(" ,;.:-") for w in str(name or "").split()]
    words = [w for w in words if w]
    if len(words) < 2:
        return ""        # the name is already its own short form
    for word in words:
        if word.lower() not in STEM_STOPWORDS and len(word) >= 3:
            return word
    return ""


def to_row(entry):
    """One directory entry as a `devices.csv` row.

    `ca` and `country` are left blank deliberately. `country` is the
    *expected manufacturer* country and a mismatch costs score, and not every
    listed manufacturer is German - guessing DE across the board would
    penalise correct matches.
    """
    name = str(entry.get("name") or "").strip()
    return {
        "name": ascii_name(name),
        "description": re.sub(r"\s+", " ", str(entry.get("description") or "")).strip(),
        "ca": "",
        "country": "",
        "keys": "|".join(search_keys(name)),
        "broad": broad_term(name, entry.get("manufacturer") or ""),
    }


# ------------------------------------------------------------------- report
class Report:
    """What an extraction saw, so a failure is diagnosable rather than silent."""

    def __init__(self, extractor="", source=""):
        self.extractor = extractor
        self.source = source
        self.candidates = 0
        self.rejected = []          # (value, reason)
        self.notes = []

    def reject(self, value, reason):
        self.rejected.append((str(value)[:120], reason))

    def summary(self):
        return (f"{self.extractor or 'none'}: {self.candidates} candidate(s) accepted, "
                f"{len(self.rejected)} rejected")

    def text(self, sample=""):
        lines = ["# diga extraction report",
                 "",
                 f"- Source: {self.source or '(none)'}",
                 f"- Generated: {time.strftime('%Y-%m-%d %H:%M')}",
                 f"- Extractor: {self.extractor or '(none matched)'}",
                 f"- Accepted: {self.candidates}",
                 f"- Rejected: {len(self.rejected)}",
                 ""]
        for note in self.notes:
            lines.append(f"- {note}")
        if self.notes:
            lines.append("")
        if self.rejected:
            lines += ["## Rejected candidates", ""]
            counts = {}
            for value, reason in self.rejected:
                counts.setdefault(reason, []).append(value)
            for reason, values in sorted(counts.items()):
                lines.append(f"### {reason} ({len(values)})")
                lines.append("")
                for value in values[:40]:
                    lines.append(f"- `{value}`")
                if len(values) > 40:
                    lines.append(f"- ... and {len(values) - 40} more")
                lines.append("")
        if sample:
            lines += ["## Input sample (first 4000 chars)", "", "```",
                      sample[:4000], "```", ""]
        return "\n".join(lines)


# --------------------------------------------------------------- extractors
def _acceptable(name, report):
    """Filter candidates that cannot be product names."""
    text = str(name or "").strip()
    if len(text) < MIN_NAME:
        report.reject(text, "too short")
        return False
    if len(text) > MAX_NAME:
        report.reject(text, "too long to be a product name")
        return False
    if not re.search(r"[A-Za-zÀ-ɏ]", text):
        report.reject(text, "no letters")
        return False
    if MARKUP.search(text):
        report.reject(text, "markup or code, not a name")
        return False
    flat = norm(text)
    if any(word in flat for word in BOILERPLATE):
        report.reject(text, "site boilerplate, not a product")
        return False
    return True


def _collect(entries, report):
    """De-duplicate accepted entries, keeping the first and richest."""
    out, seen = [], {}
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not _acceptable(name, report):
            continue
        key = identity(name)
        if key in seen:
            # Fill gaps from a later duplicate rather than discarding it.
            for field in ("description", "manufacturer"):
                if not seen[key].get(field) and entry.get(field):
                    seen[key][field] = entry[field]
            continue
        row = {"name": name,
               "description": entry.get("description", ""),
               "manufacturer": entry.get("manufacturer", "")}
        seen[key] = row
        out.append(row)
    report.candidates = len(out)
    return out


def from_text(text, report):
    """A plain or Markdown list: one product per line."""
    report.extractor = report.extractor or "text list"
    entries = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            # A Markdown heading or a comment names a section, not a product.
            continue
        line = re.sub(r"^[-*•·>]+\s*", "", line)         # bullets
        line = re.sub(r"^\d+[.)]\s*", "", line)                   # numbering
        line = re.sub(r"^\|\s*|\s*\|$", "", line)                 # table pipes
        if not line or set(line) <= set("-|= "):                  # table rules
            continue
        name, description = split_label(line)
        entries.append({"name": name, "description": description})
    return _collect(entries, report)


def _walk_json(node, entries):
    """Find every object that looks like a directory entry, at any depth."""
    if isinstance(node, list):
        for item in node:
            _walk_json(item, entries)
        return
    if not isinstance(node, dict):
        return

    picked = {}
    for key, value in node.items():
        if not isinstance(value, str):
            continue
        squashed = squash_key(key)
        if squashed in NAME_KEYS and "name" not in picked:
            picked["name"] = value
        elif squashed in DESCRIPTION_KEYS and "description" not in picked:
            picked["description"] = value
        elif squashed in MANUFACTURER_KEYS and "manufacturer" not in picked:
            picked["manufacturer"] = value
    if picked.get("name"):
        entries.append(picked)

    # Recurse regardless: a listing is often nested under a wrapper that also
    # carries a "name" of its own. Duplicates are collapsed later.
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_json(value, entries)


def from_json(payload, report):
    """A JSON response, of unknown shape.

    Nothing is assumed about the schema beyond the field vocabulary above,
    which is what lets a response copied out of a browser's network tab work
    without anyone having documented it.
    """
    report.extractor = report.extractor or "json"
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            report.notes.append(f"not JSON: {exc}")
            return []
    entries = []
    _walk_json(payload, entries)
    return _collect(entries, report)


def from_csv(text, report):
    """A spreadsheet export. The delimiter and columns are detected."""
    report.extractor = report.extractor or "csv"
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0]
    delimiter = max((";", ",", "\t"), key=header.count)
    if header.count(delimiter) == 0:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        return []

    columns = {}
    for field in reader.fieldnames:
        squashed = squash_key(field)
        for role, vocabulary in (("name", NAME_KEYS),
                                 ("description", DESCRIPTION_KEYS),
                                 ("manufacturer", MANUFACTURER_KEYS)):
            if squashed in vocabulary and role not in columns:
                columns[role] = field
    if "name" not in columns:
        report.notes.append(
            "no recognisable name column in: " + ", ".join(reader.fieldnames))
        return []
    report.notes.append(f"columns used: {columns}")

    entries = []
    for row in reader:
        entries.append({role: (row.get(column) or "").strip()
                        for role, column in columns.items()})
    return _collect(entries, report)


class _Page(HTMLParser):
    """Collects scripts and element text in one pass.

    A regex cannot do this reliably and a dependency is not worth taking, so
    the stdlib parser builds just enough structure for the strategies below.
    """

    INTERESTING = frozenset({"h1", "h2", "h3", "h4", "h5", "a", "span", "div",
                             "li", "p", "strong", "td", "th", "figcaption",
                             "article", "section", "header"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.elements = []          # (tag, attr_blob, text)
        self._stack = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._script = []
            return
        if tag in self.INTERESTING:
            blob = " ".join(f"{k}={v}" for k, v in attrs if v).lower()
            self._stack.append([tag, blob, []])

    def handle_endtag(self, tag):
        if tag == "script":
            if self._script:
                self.scripts.append("".join(self._script))
            self._script = None
            return
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                popped = self._stack.pop(index)
                del self._stack[index:]
                text = re.sub(r"\s+", " ", "".join(popped[2])).strip()
                if text:
                    self.elements.append((popped[0], popped[1], text))
                    # The text belongs to the ancestors too.
                    for parent in self._stack:
                        parent[2].append(" " + text + " ")
                return

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif self._stack:
            self._stack[-1][2].append(data)


CARD_HINTS = ("diga", "verzeichnis", "card", "result", "listitem", "list-item",
              "product", "teaser", "tile", "entry")


def from_html(text, report):
    """A saved page. The speculative path - see the module docstring.

    Three strategies, most reliable first. A single-page application usually
    ships its data as JSON inside a script tag, which is why that is tried
    before any markup shape: it survives a redesign, where a class name does
    not.
    """
    report.extractor = report.extractor or "html"
    page = _Page()
    try:
        page.feed(str(text or ""))
        page.close()
    except AssertionError as exc:                      # malformed markup
        report.notes.append(f"HTML parser gave up: {exc}")

    # 1. embedded JSON (JSON-LD, or a hydration state blob)
    for script in page.scripts:
        blob = script.strip()
        start = min((i for i in (blob.find("{"), blob.find("[")) if i >= 0),
                    default=-1)
        if start < 0:
            continue
        try:
            payload = json.loads(blob[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        nested = Report(extractor="html + embedded json", source=report.source)
        found = from_json(payload, nested)
        if found:
            report.extractor = nested.extractor
            report.candidates = nested.candidates
            report.rejected = nested.rejected
            report.notes.append("data came from a <script> JSON payload, "
                                "which survives a redesign better than markup")
            return found

    # 2. repeated elements whose class or data attribute looks like a listing
    carded = [(tag, text) for tag, blob, text in page.elements
              if any(hint in blob for hint in CARD_HINTS)]
    if carded:
        report.notes.append(f"{len(carded)} element(s) matched a listing class")
        found = _collect([_html_entry(t) for _, t in carded], report)
        if found:
            report.extractor = "html + listing class"
            return found
        report.notes.append("every listing-class element was rejected; "
                            "falling back to headings and links")

    # 3. last resort: headings and link text
    plain = [text for tag, _, text in page.elements
             if tag in ("h1", "h2", "h3", "h4", "h5", "a")]
    report.notes.append(f"{len(plain)} heading(s)/link(s) considered")
    report.extractor = "html + headings and links"
    return _collect([_html_entry(t) for t in plain], report)


def _html_entry(text):
    name, description = split_label(text)
    return {"name": name, "description": description}


def detect(text, hint=None):
    """Which extractor suits this payload."""
    if hint:
        return hint
    sample = str(text or "").lstrip()
    if not sample:
        return "text list"
    if sample[:1] in "{[":
        try:
            json.loads(sample)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    head = sample[:2000].lower()
    if "<html" in head or "<!doctype html" in head or ("</" in head and "<" in head):
        return "html"
    first = sample.splitlines()[0]
    if any(first.count(d) >= 1 for d in (";", ",", "\t")):
        squashed = {squash_key(c) for c in re.split(r"[;,\t]", first)}
        if squashed & NAME_KEYS:
            return "csv"
    return "text list"


EXTRACTORS = {"json": from_json, "csv": from_csv, "html": from_html,
              "text list": from_text}


def extract(text, hint=None, source=""):
    """Entries plus a Report, from a payload of unknown shape."""
    kind = detect(text, hint)
    report = Report(extractor=kind, source=source)
    entries = EXTRACTORS[kind](text, report)
    if not entries and kind != "text list" and not hint:
        # A detector can be wrong; a plain list never crashes, so try it
        # rather than reporting nothing at all.
        report.notes.append(f"the {kind} extractor found nothing; "
                            "retried as a plain list")
        fallback = Report(extractor="text list", source=source)
        entries = from_text(text, fallback)
        if entries:
            fallback.notes = report.notes + fallback.notes
            return entries, fallback
    return entries, report


# ------------------------------------------------------------------ fetching
def fetch(url, timeout=30):
    """One GET. Returns (body, content_type).

    Deliberately not client.Client: that appends EUDAMED's mandatory `format`
    and `api-version` parameters and unwraps OData envelopes, none of which
    applies to an unrelated website.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": "eudamed-lookup (+https://github.com/Arya012-UvA/Eudamed_Lookup)",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.1",
        "Accept-Language": "de,en;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return (response.read().decode(charset, "replace"),
                response.headers.get("Content-Type", ""))


# -------------------------------------------------------------------- merge
def read_rows(path):
    """Existing CSV rows, verbatim, or [] when the file does not exist."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)
                    if (row.get("name") or "").strip()]
    except FileNotFoundError:
        return []


def merge(existing, fetched):
    """Fold fetched rows into existing ones, preserving hand curation.

    A refresh must not throw away tuned `keys` and `broad` terms to gain a
    handful of new names, so for a product already present only a missing
    `description` is filled in. A product the source no longer lists is
    **kept** and reported: a delisted app may still be registered in EUDAMED,
    so it remains a valid thing to search for.

    Returns (rows, diff).
    """
    by_id = {identity(row.get("name", "")): row for row in existing}
    order = [identity(row.get("name", "")) for row in existing]

    added, unchanged, enriched = [], [], []
    for row in fetched:
        key = identity(row["name"])
        if key in by_id:
            current = by_id[key]
            if not (current.get("description") or "").strip() and row["description"]:
                current["description"] = row["description"]
                enriched.append(current["name"])
            unchanged.append(current.get("name", ""))
            continue
        by_id[key] = row
        order.append(key)
        added.append(row["name"])

    seen_source = {identity(row["name"]) for row in fetched}
    missing = [by_id[key].get("name", "") for key in order
               if key not in seen_source] if fetched else []

    rows, emitted = [], set()
    for key in order:
        if key in emitted:
            continue
        emitted.add(key)
        rows.append({column: by_id[key].get(column, "") for column in CSV_COLUMNS})
    return rows, {"added": added, "unchanged": unchanged, "enriched": enriched,
                  "missing_from_source": missing}


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def provenance_path(csv_path):
    return os.path.splitext(csv_path)[0] + ".provenance.md"


def provenance(csv_path, source, report, rows, diff, raw=""):
    """Record where a generated file came from; returns the path written.

    A sidecar rather than a comment header: a `#` line in the CSV makes
    search.load_targets reject the whole file for having no `name` column.
    """
    path = provenance_path(csv_path)
    digest = hashlib.sha256((raw or "").encode("utf-8", "replace")).hexdigest()
    lines = [
        "# Provenance of the generated seed list",
        "",
        "Written by `python -m eudamed diga`. Do not edit by hand - it is",
        "regenerated on every write.",
        "",
        f"- Source: `{source}`",
        f"- Retrieved: {time.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- Extractor: {report.extractor}",
        f"- Input SHA-256: `{digest}`" if raw else "- Input SHA-256: (none)",
        f"- Entries accepted from the source: {report.candidates}",
        f"- Candidates rejected: {len(report.rejected)}",
        f"- Rows in `{csv_path}`: {len(rows)}",
        "",
        "## Changes in this write",
        "",
        f"- Added: {len(diff['added'])}"
        + (f" ({', '.join(diff['added'][:20])}"
           + (", ..." if len(diff["added"]) > 20 else "") + ")" if diff["added"] else ""),
        f"- Already present, curation kept: {len(diff['unchanged'])}",
        f"- Descriptions filled in: {len(diff['enriched'])}",
        f"- No longer listed by the source, kept anyway: {len(diff['missing_from_source'])}"
        + (f" ({', '.join(diff['missing_from_source'][:20])})"
           if diff["missing_from_source"] else ""),
        "",
        "A product the directory no longer lists is kept rather than deleted:",
        "it may still be registered in EUDAMED, so it stays worth searching.",
        "",
    ]
    if report.notes:
        lines += ["## Extractor notes", ""] + [f"- {n}" for n in report.notes] + [""]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path
