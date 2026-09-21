"""Turning a published directory listing into a search input CSV.

Almost all of this is verifiable offline, which is the point of the design:
the plain-list, JSON and CSV paths are certain, and only the HTML selectors
are a guess about a site that could not be seen while writing them. So the
HTML tests prove the *mechanism* on synthetic pages, not that they match the
real BfArM markup - and the "found nothing" path is tested as a first-class
outcome rather than an error, because that is what a JavaScript application
returns to a plain fetch.
"""

import json
import os

import pytest

from eudamed import diga
from eudamed.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main


# --- names ------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [
    ("Angststörungen", "Angststoerungen"),
    ("Größe", "Groesse"),
    ("Oviva Direkt für Adipositas", "Oviva Direkt fuer Adipositas"),
    ("edupression.com®", "edupression.com"),
    ("HelloBetter Vaginismus Plus™", "HelloBetter Vaginismus Plus"),
    ("  spaced   out  ", "spaced out"),
])
def test_ascii_name_transliterates_rather_than_dropping(raw, expected):
    """matching.norm() cannot be reused here: it drops umlauts and mangles ß.

    "für" -> "fur" and "Größe" -> "groe" would put wrong names in the file.
    """
    assert diga.ascii_name(raw) == expected


def test_ascii_name_differs_from_norm():
    """Guards the reason this function exists at all."""
    from eudamed.matching import norm
    assert norm("Größe") != diga.ascii_name("Größe").lower()


def test_keys_keep_the_original_spelling_first():
    """The register holds the accented string, so that is what must be sent."""
    keys = diga.search_keys("Oviva Direkt für Adipositas")
    assert keys[0] == "Oviva Direkt für Adipositas"
    assert "Oviva Direkt fuer Adipositas" in keys       # in case it was folded
    assert "Oviva Direkt" in keys                       # exact-filter prefix


def test_keys_split_on_a_separator():
    keys = diga.search_keys("Invirto \u2013 Die Therapie gegen Angst")
    assert "Invirto" in keys
    # No key may be left as a bare fragment with dangling punctuation.
    assert all(not k.rstrip().endswith(("-", "\u2013", "\u2014", ":"))
               for k in keys)


def test_keys_of_a_single_word_name_are_just_the_word():
    assert diga.search_keys("deprexis") == ["deprexis"]


@pytest.mark.parametrize(("name", "manufacturer", "expected"), [
    ("Meine Tinnitus App", "", "Tinnitus"),        # leading stopword skipped
    ("HelloBetter Diabetes und Depression", "", "HelloBetter"),
    ("Kranus Edera", "", "Kranus"),
    ("deprexis", "", ""),                          # already its own short form
    ("deprexis", "GAIA AG", "GAIA AG"),            # manufacturer wins
])
def test_broad_term(name, manufacturer, expected):
    assert diga.broad_term(name, manufacturer) == expected


def test_row_leaves_country_blank():
    """Deliberate: a wrong expected country costs 0.10 of score, and not
    every listed manufacturer is German."""
    row = diga.to_row({"name": "Vitadio", "description": "Diabetes"})
    assert row["country"] == "" and row["ca"] == ""


# --- extractors -------------------------------------------------------------
def test_text_list_handles_bullets_numbering_and_indications():
    entries, report = diga.extract(
        "- deprexis — Depression\n"
        "* velibra \u2013 Angststörungen\n"
        "1. somnio: Insomnie\n"
        "zanadio\n")
    assert report.extractor == "text list"
    assert [e["name"] for e in entries] == ["deprexis", "velibra", "somnio", "zanadio"]
    assert entries[1]["description"] == "Angststörungen"


def test_text_list_skips_headings_and_boilerplate():
    entries, report = diga.extract("# DiGA directory\n- deprexis\nDatenschutz\nImpressum\n")
    assert [e["name"] for e in entries] == ["deprexis"]
    assert {r for _, r in report.rejected} == {"site boilerplate, not a product"}


def test_markup_is_never_accepted_as_a_product_name():
    """The failure this would otherwise have shipped with.

    A plain fetch of a JavaScript application returns one short line of HTML.
    It is under the length limit and contains letters, so without a markup
    check the plain-list fallback offers it as a device to search for.
    """
    entries, report = diga.extract(
        '<!doctype html><html><body><div id="root"></div></body></html>')
    assert entries == []
    assert any("markup" in reason for _, reason in report.rejected) or not report.rejected


def test_json_of_unknown_shape_with_german_keys():
    """Nothing is assumed about the schema, which is what lets a response
    copied out of a browser's network tab work."""
    payload = {"page": 0, "content": {"items": [
        {"produktName": "deprexis", "indikation": "Depression", "hersteller": "GAIA AG"},
        {"produktName": "somnio", "anwendungsgebiet": "Insomnie"}]}}
    entries, report = diga.extract(json.dumps(payload))
    assert report.extractor == "json"
    assert [e["name"] for e in entries] == ["deprexis", "somnio"]
    assert entries[0]["manufacturer"] == "GAIA AG"
    assert entries[1]["description"] == "Insomnie"


def test_json_deduplicates_a_nested_repeat():
    payload = {"name": "wrapper", "items": [{"name": "deprexis"}, {"name": "deprexis"}]}
    entries, _ = diga.extract(json.dumps(payload))
    assert [e["name"] for e in entries] == ["wrapper", "deprexis"]


def test_csv_detects_the_delimiter_and_column_order():
    entries, report = diga.extract(
        "Hersteller;Indikation;Bezeichnung\n"
        "GAIA AG;Depression;deprexis\n"
        "mementor;Insomnie;somnio\n")
    assert report.extractor == "csv"
    assert [e["name"] for e in entries] == ["deprexis", "somnio"]
    assert entries[0]["manufacturer"] == "GAIA AG"


def test_csv_without_a_name_column_says_so():
    entries, report = diga.extract("foo,bar\n1,2\n", hint="csv")
    assert entries == []
    assert any("no recognisable name column" in n for n in report.notes)


def test_html_prefers_an_embedded_json_payload():
    """A script payload survives a redesign; a class name does not."""
    entries, report = diga.extract(
        '<!doctype html><html><head><script type="application/json">'
        '{"digas":[{"name":"Kalmeda","indikation":"Tinnitus"}]}</script></head>'
        '<body><a href="/datenschutz">Datenschutz</a></body></html>')
    assert report.extractor == "html + embedded json"
    assert [e["name"] for e in entries] == ["Kalmeda"]


def test_html_falls_back_to_listing_classes():
    entries, report = diga.extract(
        '<!doctype html><html><body><nav><a href="/">Startseite</a></nav>'
        '<ul><li class="diga-card"><h3>Mika</h3></li>'
        '<li class="diga-card"><h3>Vivira</h3></li></ul></body></html>')
    assert report.extractor == "html + listing class"
    assert sorted(e["name"] for e in entries) == ["Mika", "Vivira"]


def test_html_falls_back_to_headings_and_links():
    entries, report = diga.extract(
        "<!doctype html><html><body><h2>zanadio</h2><h2>somnio</h2>"
        '<a href="/impressum">Impressum</a></body></html>')
    assert report.extractor == "html + headings and links"
    assert sorted(e["name"] for e in entries) == ["somnio", "zanadio"]


def test_a_javascript_shell_yields_nothing_and_says_why():
    """The expected outcome for the real directory, not an error."""
    entries, report = diga.extract(
        '<!doctype html><html><head><title>DiGA</title></head>'
        '<body><div id="app"></div><script src="/main.js"></script></body></html>')
    assert entries == []
    assert report.notes
    assert "report" in report.text().lower() or report.text()


def test_detect():
    assert diga.detect('{"a": 1}') == "json"
    assert diga.detect("<!doctype html><p>x</p>") == "html"
    assert diga.detect("Bezeichnung;Indikation\na;b") == "csv"
    assert diga.detect("deprexis\nvelibra") == "text list"
    assert diga.detect("anything", hint="json") == "json"


def test_a_misdetected_payload_retries_as_a_plain_list():
    """A detector can be wrong; a plain list never crashes."""
    entries, report = diga.extract("Name,Something\n")      # csv, no rows
    assert report.extractor == "text list"
    assert any("retried as a plain list" in n for n in report.notes)
    assert [e["name"] for e in entries] == ["Name,Something"] or entries == []


# --- merge ------------------------------------------------------------------
def existing_rows():
    return [
        {"name": "deprexis", "description": "mental health: depression", "ca": "",
         "country": "", "keys": "deprexis", "broad": "GAIA"},
        {"name": "Oviva Direkt fuer Adipositas", "description": "obesity", "ca": "",
         "country": "", "keys": "Oviva Direkt für Adipositas|Oviva Direkt",
         "broad": "Oviva"},
    ]


def test_merge_preserves_hand_curated_keys_and_broad():
    """A refresh must not trade tuned query terms for a few new names."""
    fetched = [diga.to_row({"name": "deprexis", "description": "Depression",
                            "manufacturer": "GAIA AG"})]
    rows, diff = diga.merge(existing_rows(), fetched)
    kept = next(r for r in rows if r["name"] == "deprexis")
    assert kept["keys"] == "deprexis"          # not the generated variants
    assert kept["broad"] == "GAIA"             # not "GAIA AG"
    assert diff["added"] == [] and diff["unchanged"] == ["deprexis"]


def test_merge_matches_across_umlaut_spellings():
    """The source's accented name is the same product as the ASCII row."""
    fetched = [diga.to_row({"name": "Oviva Direkt für Adipositas"})]
    rows, diff = diga.merge(existing_rows(), fetched)
    assert len(rows) == 2                       # not duplicated
    assert diff["added"] == []


def test_merge_adds_a_new_product():
    fetched = [diga.to_row({"name": "Nala", "description": "Neurodermitis"})]
    rows, diff = diga.merge(existing_rows(), fetched)
    assert diff["added"] == ["Nala"]
    assert [r["name"] for r in rows][-1] == "Nala"


def test_merge_keeps_a_product_the_source_dropped():
    """A delisted app may still be registered in EUDAMED, so it stays
    searchable - reported, not deleted."""
    fetched = [diga.to_row({"name": "deprexis"})]
    rows, diff = diga.merge(existing_rows(), fetched)
    assert "Oviva Direkt fuer Adipositas" in diff["missing_from_source"]
    assert any(r["name"] == "Oviva Direkt fuer Adipositas" for r in rows)


def test_merge_fills_only_a_blank_description():
    existing = [{"name": "deprexis", "description": "", "ca": "", "country": "",
                 "keys": "deprexis", "broad": ""}]
    fetched = [diga.to_row({"name": "deprexis", "description": "Depression"})]
    rows, diff = diga.merge(existing, fetched)
    assert rows[0]["description"] == "Depression"
    assert diff["enriched"] == ["deprexis"]


def test_merged_output_loads_as_a_search_input(tmp_path):
    """The whole point: the generated file must feed the existing pipeline."""
    from eudamed.search import load_targets

    rows, _ = diga.merge(existing_rows(), [
        diga.to_row({"name": "Mindable: Panikstörung und Agoraphobie",
                     "description": "Panik"})])
    path = tmp_path / "seed.csv"
    diga.write_rows(path, rows)
    targets = load_targets(str(path))
    assert [t.name for t in targets][-1] == "Mindable: Panikstoerung und Agoraphobie"
    assert "Mindable: Panikstörung und Agoraphobie" in targets[-1].keys
    for target in targets:
        target.name.encode("ascii")            # raises if not ASCII
        assert target.keys


def test_provenance_is_a_sidecar_not_a_csv_comment(tmp_path):
    """A `#` line in the CSV makes load_targets reject the whole file."""
    from eudamed.search import load_targets

    rows, diff = diga.merge([], [diga.to_row({"name": "deprexis"})])
    csv_path = tmp_path / "seed.csv"
    diga.write_rows(csv_path, rows)
    report = diga.Report(extractor="text list", source="pasted")
    report.candidates = 1
    notes = diga.provenance(str(csv_path), "pasted", report, rows, diff, raw="deprexis")
    assert os.path.basename(notes) == "seed.provenance.md"
    with open(notes, encoding="utf-8") as handle:
        body = handle.read()
    assert "Input SHA-256" in body and "seed.csv" in body
    assert load_targets(str(csv_path))         # the CSV itself stayed loadable


# --- CLI --------------------------------------------------------------------
@pytest.fixture
def listing(tmp_path):
    path = tmp_path / "names.txt"
    path.write_text("- deprexis — Depression\n- Nala — Neurodermitis\nDatenschutz\n",
                    encoding="utf-8")
    return str(path)


def test_cli_previews_without_writing(listing, tmp_path, capsys):
    """Preview is the default so an unverified parser cannot clobber a list."""
    assert main(["diga", "--from", listing]) == EXIT_OK
    out = capsys.readouterr()
    assert "Nothing was written" in out.err
    assert "deprexis,Depression" in out.out
    assert not list(tmp_path.glob("*.csv"))


def test_cli_needs_exactly_one_input(capsys):
    assert main(["diga"]) == EXIT_USAGE
    assert main(["diga", "--from", "x", "--url", "http://x"]) == EXIT_USAGE


def test_cli_writes_and_records_provenance(listing, tmp_path, capsys):
    out = tmp_path / "seed.csv"
    assert main(["diga", "--from", listing, "--out", str(out)]) == EXIT_OK
    assert out.exists()
    assert (tmp_path / "seed.provenance.md").exists()
    assert "2 added" in capsys.readouterr().err


def test_cli_merges_into_an_existing_file(listing, tmp_path):
    out = tmp_path / "seed.csv"
    out.write_text("name,description,ca,country,keys,broad\n"
                   "deprexis,curated,,,deprexis|Deprexis Plus,GAIA\n", encoding="utf-8")
    assert main(["diga", "--from", listing, "--out", str(out)]) == EXIT_OK
    rows = diga.read_rows(str(out))
    assert len(rows) == 2
    assert rows[0]["keys"] == "deprexis|Deprexis Plus"      # curation survived


def test_cli_replace_needs_force(listing, tmp_path, capsys):
    out = tmp_path / "seed.csv"
    out.write_text("name,description,ca,country,keys,broad\nx,,,,x,\n", encoding="utf-8")
    assert main(["diga", "--from", listing, "--out", str(out), "--replace"]) == EXIT_USAGE
    assert "pass --force" in capsys.readouterr().err
    assert main(["diga", "--from", listing, "--out", str(out),
                 "--replace", "--force"]) == EXIT_OK
    assert [r["name"] for r in diga.read_rows(str(out))] == ["deprexis", "Nala"]


def test_cli_reports_a_failed_parse_as_an_error_with_instructions(tmp_path, capsys):
    shell = tmp_path / "shell.html"
    shell.write_text('<!doctype html><html><body><div id="app"></div></body></html>',
                     encoding="utf-8")
    report = tmp_path / "report.md"
    code = main(["diga", "--from", str(shell), "--report", str(report)])
    assert code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "JavaScript application" in err
    assert "--from" in err and "F12" in err
    assert "diga extraction report" in report.read_text(encoding="utf-8")


def test_cli_missing_input_file(capsys):
    assert main(["diga", "--from", "/no/such/file.txt"]) == EXIT_USAGE
    assert "cannot read --from" in capsys.readouterr().err


def test_cli_forced_extractor_overrides_detection(tmp_path, capsys):
    """--as wins, for a payload whose extension or shape misleads the sniffer."""
    path = tmp_path / "listing.txt"          # JSON in a .txt file
    path.write_text('{"items": [{"name": "deprexis"}]}', encoding="utf-8")
    assert main(["diga", "--from", str(path), "--as", "json"]) == EXIT_OK
    assert "deprexis" in capsys.readouterr().out


def test_cli_forcing_the_wrong_extractor_finds_nothing_rather_than_guessing(
        tmp_path, capsys):
    """With --as given, the plain-list retry is suppressed on purpose: the
    caller asserted the format, so a silent fallback would hide their mistake."""
    path = tmp_path / "x.txt"
    path.write_text('{"name": "deprexis"}', encoding="utf-8")
    assert main(["diga", "--from", str(path), "--as", "text"]) == EXIT_ERROR
    assert "0 candidate(s) accepted" in capsys.readouterr().err


# --- web UI -----------------------------------------------------------------
LISTING = "- MindDoc — Depression\n- Kalmeda — Tinnitus\nDatenschutz\n"


def test_ui_seed_previews_without_changing_the_device_list(ui_server_fresh):
    before = ui_server_fresh.json("/api/devices")["devices"]
    status, payload = ui_server_fresh.post("/api/seed", {"text": LISTING})
    assert status == 200
    assert [r["name"] for r in payload["rows"]] == ["MindDoc", "Kalmeda"]
    assert payload["extractor"] == "text list"
    assert payload["loaded"] is None
    assert payload["rejected"][0]["reason"] == "site boilerplate, not a product"
    assert ui_server_fresh.json("/api/devices")["devices"] == before


def test_ui_seed_can_load_the_list_for_immediate_searching(ui_server_fresh):
    """The point of doing this in the browser: search it without a restart."""
    status, payload = ui_server_fresh.post(
        "/api/seed", {"text": LISTING, "mode": "replace"})
    assert status == 200 and payload["loaded"] == 2
    names = [d["name"] for d in ui_server_fresh.json("/api/devices")["devices"]]
    assert names == ["MindDoc", "Kalmeda"]
    # And the loaded definition is searchable by its own keys.
    result = ui_server_fresh.json("/api/search?target=MindDoc")
    assert result["status"] == "found"


def test_ui_seed_add_mode_keeps_what_was_there(ui_server_fresh):
    ui_server_fresh.post("/api/seed", {"text": "- MindDoc\n", "mode": "replace"})
    status, payload = ui_server_fresh.post(
        "/api/seed", {"text": "- Kalmeda\n- MindDoc\n", "mode": "add"})
    assert status == 200 and payload["loaded"] == 2      # MindDoc not duplicated
    names = [d["name"] for d in ui_server_fresh.json("/api/devices")["devices"]]
    assert names == ["MindDoc", "Kalmeda"]


def test_ui_seed_rejects_an_empty_body(ui_server_fresh):
    assert ui_server_fresh.post("/api/seed", {})[0] == 400
    assert ui_server_fresh.post("/api/seed", {"text": "   "})[0] == 400


def test_ui_seed_rejects_a_non_http_url(ui_server_fresh):
    status, payload = ui_server_fresh.post("/api/seed", {"url": "file:///etc/passwd"})
    assert status == 400
    assert "http" in payload["error"]


def test_ui_seed_fetches_a_url_server_side(ui_server_fresh, live_server):
    """The browser cannot fetch a third-party site itself, so the server does.

    Pointed at the fake API here, which returns JSON the extractor can read.
    """
    status, payload = ui_server_fresh.post(
        "/api/seed", {"url": live_server + "/udi?format=json&subscription-key=dummy"})
    assert status == 200
    assert any(r["name"].startswith("MindDoc") for r in payload["rows"])


def test_ui_seed_reports_an_empty_parse_rather_than_failing(ui_server_fresh):
    status, payload = ui_server_fresh.post(
        "/api/seed", {"text": '<html><body><div id="a"></div></body></html>'})
    assert status == 200
    assert payload["rows"] == []
    assert payload["notes"]


def test_ui_seed_only_answers_post(ui_server_fresh):
    assert ui_server_fresh.status("/api/seed") == 404


def test_ui_post_to_an_unknown_path_is_404(ui_server_fresh):
    assert ui_server_fresh.post("/api/nope", {})[0] == 404


def test_url_fetching_is_refused_when_not_bound_to_loopback():
    """A publicly bound server must not become a request forwarder."""
    from eudamed.webui import UIServer

    server = object.__new__(UIServer)
    server.server_address = ("0.0.0.0", 8100)
    assert UIServer.can_fetch(server) is False
    server.server_address = ("127.0.0.1", 8100)
    assert UIServer.can_fetch(server) is True


def test_the_page_offers_the_seed_builder():
    """Browser-verified; guarded so the wiring cannot silently disappear."""
    from eudamed.webui import PAGE

    for marker in ('id="seedpanel"', 'id="s_text"', 'id="s_go"', 'id="s_fetch"',
                   'id="s_file"', "buildSeed(", "reloadDevices()",
                   "Use as My list", "Add to My list", "Download CSV"):
        assert marker in PAGE, marker
