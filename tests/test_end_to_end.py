"""End-to-end over a real socket against the bundled fake API.

These exercise the whole stack - argparse, HTTP, parsing, scoring, writers -
with no mocking below the CLI entry point.
"""

import csv
import json

import pytest

from eudamed.cli import EXIT_AUTH, EXIT_OK, EXIT_USAGE, main


def test_search_single_device(live_server, tmp_path, capsys):
    out = tmp_path / "res"
    code = main(["search", "--base", live_server, "--key", "dummy",
                 "--trade-name", "MindDoc", "--country", "DE",
                 "--out", str(out), "--delay", "0"])
    assert code == EXIT_OK
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    result = payload["results"][0]
    assert result["status"] == "found"
    best = result["candidates"][0]
    assert best["trade_name"] == "MindDoc"
    assert best["matched_on"] == "trade_name:exact"
    assert best["mf_srn"] == "DE-MF-000025123"
    assert best["primary_di"] == "04260703120019"


def test_search_resolves_reference_codes(live_server, tmp_path):
    out = tmp_path / "res"
    main(["search", "--base", live_server, "--key", "dummy", "--trade-name", "MindDoc",
          "--out", str(out), "--delay", "0"])
    best = json.loads((out / "results.json").read_text())["results"][0]["candidates"][0]
    assert best["risk_class"] == "CLASS_IIA"        # from RISK_CLASS_ID=2 via /reference


def test_no_resolve_codes_leaves_numeric_ids(live_server, tmp_path):
    out = tmp_path / "res"
    main(["search", "--base", live_server, "--key", "dummy", "--trade-name", "MindDoc",
          "--out", str(out), "--delay", "0", "--no-resolve-codes"])
    best = json.loads((out / "results.json").read_text())["results"][0]["candidates"][0]
    assert best["risk_class"] == ""                 # unresolved, id still available
    assert not json.loads((out / "results.json").read_text())["meta"]["reference_loaded"]


def test_search_from_csv_writes_all_outputs(live_server, tmp_path, devices_csv):
    out = tmp_path / "res"
    assert main(["search", "--base", live_server, "--key", "dummy",
                 "--input", devices_csv, "--out", str(out), "--delay", "0"]) == EXIT_OK
    results = json.loads((out / "results.json").read_text())["results"]
    assert [r["status"] for r in results] == ["found", "found", "not found"]
    rows = list(csv.DictReader((out / "results.csv").open(encoding="utf-8")))
    assert [r["name"] for r in rows] == ["MindDoc", "Kalmeda", "NotRegistered"]
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "__DATA__" not in html and "MindDoc" in html


def test_manufacturer_sibling_is_a_lead_not_a_match(live_server, tmp_path):
    """Searching MF_SRN pulls in Moodpath; it must stay below the threshold."""
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,country,keys,broad\nMindDoc,DE,MindDoc,DE-MF-000025123\n",
                        encoding="utf-8")
    out = tmp_path / "res"
    main(["search", "--base", live_server, "--key", "dummy", "--input", str(csv_path),
          "--out", str(out), "--fields", "TRADE_NAME,MF_SRN", "--delay", "0"])
    cands = json.loads((out / "results.json").read_text())["results"][0]["candidates"]
    by_name = {c["trade_name"]: c for c in cands}
    assert by_name["MindDoc"]["matched_on"] == "trade_name:exact"
    assert by_name["Moodpath"]["matched_on"] == "manufacturer"
    assert by_name["Moodpath"]["score"] < 0.6
    assert cands[0]["trade_name"] == "MindDoc"      # real match ranks first


def test_csv_response_format_works_end_to_end(live_server, tmp_path):
    out = tmp_path / "res"
    assert main(["search", "--base", live_server, "--key", "dummy", "--format", "csv",
                 "--trade-name", "MindDoc", "--out", str(out), "--delay", "0"]) == EXIT_OK
    best = json.loads((out / "results.json").read_text())["results"][0]["candidates"][0]
    assert best["trade_name"] == "MindDoc"


def test_device_name_field_search(live_server, tmp_path):
    """MindDoc's DEVICE_NAME mentions 'depression'; TRADE_NAME does not."""
    out = tmp_path / "res"
    main(["search", "--base", live_server, "--key", "dummy", "--trade-name", "depression",
          "--out", str(out), "--fields", "DEVICE_NAME", "--delay", "0"])
    result = json.loads((out / "results.json").read_text())["results"][0]
    assert result["total_matches"] == 1


def test_missing_key_exits_auth_without_requests(live_server, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    code = main(["search", "--base", live_server, "--trade-name", "MindDoc",
                 "--out", str(tmp_path / "res")])
    assert code == EXIT_AUTH
    assert "subscription key" in capsys.readouterr().err


def test_bad_key_is_reported_as_auth(live_server, tmp_path, monkeypatch):
    """The fake server rejects a blank key the way APIM does."""
    monkeypatch.setenv("EUDAMED_SUBSCRIPTION_KEY", "")
    code = main(["search", "--base", live_server, "--key", "", "--trade-name", "MindDoc",
                 "--out", str(tmp_path / "res")])
    assert code == EXIT_AUTH


def test_probe_reports_real_field_names(live_server, capsys):
    assert main(["probe", "--base", live_server, "--key", "dummy",
                 "--trade-name", "MindDoc", "--delay", "0"]) == EXIT_OK
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert "TRADE_NAME" in report["/udi"]["fields"]
    assert "MF_SRN" in report["/udi"]["fields"]
    assert report["/udi"]["rows"] == 1
    assert "record mapping resolved" in captured.err


def test_probe_saves_raw_bodies(live_server, tmp_path):
    raw = tmp_path / "raw"
    main(["probe", "--base", live_server, "--key", "dummy", "--raw-dir", str(raw),
          "--delay", "0"])
    saved = json.loads((raw / "udi.json").read_text(encoding="utf-8"))
    assert saved[0]["TRADE_NAME"] == "MindDoc"


def test_actors_command(live_server, capsys):
    assert main(["actors", "--base", live_server, "--key", "dummy",
                 "--name", "MindDoc", "--delay", "0"]) == EXIT_OK
    actors = json.loads(capsys.readouterr().out)
    assert actors[0]["actor_id"] == "DE-MF-000025123"
    assert actors[0]["country"] == "DE"


def test_reference_command(live_server, capsys):
    assert main(["reference", "--base", live_server, "--key", "dummy",
                 "--language", "en", "--delay", "0"]) == EXIT_OK
    assert "CLASS_IIA" in capsys.readouterr().out


def test_dry_run_makes_no_requests(capsys):
    assert main(["search", "--trade-name", "MindDoc", "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "api.datalake.sante.service.ec.europa.eu" in out
    assert "TRADE_NAME=MindDoc" in out and "format=json" in out and "api-version=v1.0" in out


def test_dry_run_needs_no_key(monkeypatch, capsys):
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    assert main(["search", "--trade-name", "X", "--dry-run"]) == EXIT_OK


@pytest.mark.parametrize("argv", [
    ["search"],                                          # neither --trade-name nor --input
    ["search", "--trade-name", "X", "--fields", "tradeName"],   # UI-API spelling
    ["search", "--trade-name", "X", "--fields", "page"],        # pagination param
    ["actors"],                                          # no filter given
])
def test_usage_errors(argv, capsys):
    assert main(argv) == EXIT_USAGE


def test_unreadable_input_is_a_usage_error(tmp_path, capsys):
    assert main(["search", "--input", str(tmp_path / "nope.csv")]) == EXIT_USAGE


def test_reference_labels_resolve_all_four_coded_fields(live_server, tmp_path):
    out = tmp_path / "res"
    main(["search", "--base", live_server, "--key", "dummy", "--trade-name", "MindDoc",
          "--out", str(out), "--delay", "0"])
    best = json.loads((out / "results.json").read_text())["results"][0]["candidates"][0]
    assert best["risk_class"] == "CLASS_IIA"
    assert best["legislation"] == "MDR"
    assert best["market_status"] == "ON_THE_MARKET"
    assert best["special_type"] == "NONE"


def test_ambiguous_reference_ids_are_left_unresolved(live_server, client_factory):
    """/reference has no column identifying which code table an id belongs to,
    so an id mapping to several codes must not be resolved to an arbitrary one."""
    from eudamed.client import Client
    from eudamed.reference import Reference
    ref = Reference(Client(base=live_server, key="dummy", delay=0, backoff_base=0)).load()
    assert 99 in ref.ambiguous
    assert ref.ambiguous[99] == ["AMBIGUOUS_A", "AMBIGUOUS_B"]
    assert ref.label(99) == "99"             # raw id, not a guess
    assert ref.label(2) == "CLASS_IIA"       # unambiguous ids still resolve


# --- web UI -------------------------------------------------------------
def test_ui_serves_the_page_and_health(ui_server):
    html = ui_server.text("/")
    assert "<title>EUDAMED Search</title>" in html
    assert "__DATA__" not in html
    health = ui_server.json("/api/health")
    assert health["has_key"] is True
    assert health["thresholds"]["found"] == 0.85
    assert "TRADE_NAME" in health["udi_params"]


def test_ui_search_by_name(ui_server):
    d = ui_server.json("/api/search?name=MindDoc&country=DE")
    assert d["status"] == "found"
    assert d["candidates"][0]["matched_on"] == "trade_name:exact"
    assert d["candidates"][0]["risk_class"] == "CLASS_IIA"
    # Every code resolved here, so no ambiguity banner should be attached even
    # though the reference table does contain an ambiguous id.
    assert "reference_ambiguous" not in d


def test_ui_search_by_identifier(ui_server):
    """An SRN typed into the box is an identifier match, not a name comparison."""
    d = ui_server.json("/api/search?name=DE-MF-000025123&fields=MF_SRN")
    assert d["status"] == "found"
    assert len(d["candidates"]) == 2
    assert all(c["matched_on"] == "identifier:MF_SRN" for c in d["candidates"])


def test_ui_search_by_udi_di(ui_server):
    d = ui_server.json("/api/search?name=04260703120019&fields=PRIMARY_DI")
    assert d["status"] == "found"
    assert d["candidates"][0]["matched_on"] == "identifier:PRIMARY_DI"


def test_ui_not_found(ui_server):
    d = ui_server.json("/api/search?name=ZZZNotARealDevice")
    assert d["status"] == "not found" and d["candidates"] == []


@pytest.mark.parametrize("path,code", [
    ("/api/search?name=", 400),
    ("/api/search?name=X&fields=tradeName", 400),
    ("/api/search?name=X&fields=page", 400),
    ("/api/nope", 404),
])
def test_ui_rejects_bad_input(ui_server, path, code):
    assert ui_server.status(path) == code


def test_ui_actors(ui_server):
    d = ui_server.json("/api/actors?name=MindDoc")
    assert d["actors"][0]["actor_id"] == "DE-MF-000025123"


def test_ui_serves_a_favicon(ui_server):
    assert "<svg" in ui_server.text("/favicon.svg")
    assert ui_server.status("/favicon.ico") == 204


def test_ui_never_exposes_the_key(ui_server):
    """The key lives server side; it must not reach the page or any response."""
    assert "dummy" not in ui_server.text("/")
    assert "dummy" not in json.dumps(ui_server.json("/api/health"))
    assert "dummy" not in json.dumps(ui_server.json("/api/search?name=MindDoc"))


# --- the loaded device list in the UI ------------------------------------
def test_ui_exposes_the_loaded_device_list(ui_server):
    devices = ui_server.json("/api/devices")["devices"]
    assert [d["name"] for d in devices] == [
        "MindDoc", "HelloBetter Stress und Burnout", "Kalmeda"]
    assert ui_server.json("/api/health")["device_count"] == 3


def test_ui_target_uses_every_spelling_variant(ui_server):
    """Picking a name from the list must search all its `keys` and `broad`
    terms, not just the one string. Typing the name alone runs one query."""
    d = ui_server.json("/api/search?target=HelloBetter+Stress+und+Burnout")
    terms = [q["term"] for q in d["queries"]]
    assert terms == ["HelloBetter Stress und Burnout", "HelloBetter Stress", "HelloBetter"]

    typed = ui_server.json("/api/search?name=HelloBetter")
    assert [q["term"] for q in typed["queries"]] == ["HelloBetter"]


def test_ui_target_carries_country_and_description(ui_server):
    d = ui_server.json("/api/search?target=MindDoc")
    assert d["country"] == "DE" and d["description"] == "psych" and d["ca"] == "Bavaria DE"
    assert d["status"] == "found"


def test_ui_target_is_matched_case_insensitively(ui_server):
    assert ui_server.json("/api/search?target=minddoc")["name"] == "MindDoc"


def test_ui_unknown_target_is_a_404(ui_server):
    assert ui_server.status("/api/search?target=NotInTheList") == 404


def test_ui_flags_a_local_base_as_demo_mode(ui_server):
    """A localhost base is the bundled stand-in, not real EUDAMED. The page
    says so, because otherwise a correct 'not found' looks like a broken UI."""
    assert ui_server.json("/api/health")["is_local"] is True


@pytest.mark.parametrize("base,expected", [
    ("http://127.0.0.1:8099/eudamed", True),
    ("http://localhost:8099/eudamed", True),
    ("https://api.datalake.sante.service.ec.europa.eu/eudamed", False),
])
def test_is_local_detection(base, expected):
    from eudamed.webui import _is_local
    assert _is_local(base) is expected


# --- unreachable backend ------------------------------------------------
def test_error_status_when_the_api_cannot_be_reached(tmp_path, capsys):
    """A connection failure must not be reported as 'not found'. Registration
    is unknown, not absent, and conflating the two asserts something that was
    never checked."""
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,country,keys\nMindDoc,DE,MindDoc\n", encoding="utf-8")
    out = tmp_path / "res"
    # Port 9 (discard) with nothing bound: a refused connection.
    code = main(["search", "--base", "http://127.0.0.1:9/eudamed", "--key", "dummy",
                 "--input", str(csv_path), "--out", str(out),
                 "--delay", "0", "--retries", "1", "--timeout", "2",
                 "--no-resolve-codes"])
    assert code == EXIT_OK
    result = json.loads((out / "results.json").read_text())["results"][0]
    assert result["status"] == "error"
    assert result["candidates"] == [] and result["errors"]
    assert "nothing checked" in capsys.readouterr().err

    rows = list(csv.DictReader((out / "results.csv").open(encoding="utf-8")))
    assert rows[0]["status"] == "error"
    assert rows[0]["trade_name"] == ""          # nothing may be promoted as a match

    html = (out / "report.html").read_text(encoding="utf-8")
    assert "could not be checked" in html


def test_ui_reports_error_not_not_found_when_backend_is_down(live_server):
    """Same guarantee through the web UI."""
    import threading

    from eudamed.client import Client
    from eudamed.webui import serve as make_ui
    client = Client(base="http://127.0.0.1:9/eudamed", key="dummy", delay=0,
                    backoff_base=0, retries=1, timeout=2)
    server = make_ui(client, port=0)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05),
                     daemon=True).start()
    host, port = server.server_address[:2]
    try:
        from conftest import UIClient
        ui = UIClient(f"http://{host}:{port}")
        d = ui.json("/api/search?name=MindDoc")
        assert d["status"] == "error"
        assert d["candidates"] == [] and d["errors"]
        page = ui.text("/")
        assert "could not check" in page and "This is not a" in page
    finally:
        server.shutdown()
        server.server_close()
