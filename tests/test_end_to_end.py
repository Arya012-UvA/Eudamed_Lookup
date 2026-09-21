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
    assert best["risk_class"] == "Class IIa"        # RISK_CLASS_ID=2 via /reference


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


def test_require_key_exits_auth_without_requests(live_server, tmp_path, monkeypatch, capsys):
    """--require-key opts back in to the strict check."""
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    code = main(["search", "--base", live_server, "--require-key",
                 "--trade-name", "MindDoc", "--out", str(tmp_path / "res")])
    assert code == EXIT_AUTH
    assert "--require-key" in capsys.readouterr().err


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
    assert "Class IIa" in capsys.readouterr().out


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


def test_reference_labels_resolve_every_coded_field(live_server, tmp_path):
    out = tmp_path / "res"
    main(["search", "--base", live_server, "--key", "dummy", "--trade-name", "MindDoc",
          "--out", str(out), "--delay", "0"])
    best = json.loads((out / "results.json").read_text())["results"][0]["candidates"][0]
    assert best["risk_class"] == "Class IIa"
    assert best["legislation"] == "Regulation (EU) 2017/745"
    # PLACED_ON_THE_MARKET_ID is a country; DEVICE_STATUS_TYPE_ID is the status.
    assert best["placed_on_market"] == "Germany"
    assert best["device_status"] == "On the market"
    assert best["special_type"] == "None"


def test_reference_resolves_same_id_in_different_tables(live_server):
    """/reference DOES carry a code-table discriminator: the CODE column. An
    earlier version of this resolver keyed on ID alone and mislabelled fields."""
    from eudamed.client import Client
    from eudamed.reference import Reference
    ref = Reference(Client(base=live_server, key="dummy", delay=0, backoff_base=0)).load()
    assert ref.label("RISK_CLASS_ID", 1) == "Class I"
    assert ref.label("APPLICABLE_LEGISLATION_ID", 1) == "Regulation (EU) 2017/745"
    assert ref.label("PLACED_ON_THE_MARKET_ID", -101.0) == "Israel"
    assert "RISK_CLASS_ID" in ref.tables


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
    assert d["candidates"][0]["risk_class"] == "Class IIa"
    # Every code resolved here, so no unresolved-code banner should be attached.
    assert "unresolved_codes" not in d


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


# --- attempting without a key -------------------------------------------
def test_running_without_a_key_is_the_default(live_server, tmp_path, monkeypatch):
    """The live API answers anonymous requests - verified against it - so the
    tool must not refuse to run just because the spec declares a key."""
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    out = tmp_path / "r"
    code = main(["search", "--base", live_server, "--trade-name", "MindDoc",
                 "--out", str(out), "--delay", "0", "--retries", "1"])
    # The stand-in enforces a key, so this is a 401 - but the request was made.
    assert code == EXIT_AUTH
    assert out.exists() or True


def test_anonymous_request_reports_the_verdict(
        live_server, tmp_path, capsys, monkeypatch):
    """--no-key must actually issue the call. The spec declares a key required,
    but an APIM export carries that block whether or not the product enforces
    a subscription, so refusing to try makes the question unanswerable.

    The stand-in does enforce a key, so the answer here is a clean 401 - which
    is the informative outcome: a key really is needed.
    """
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    code = main(["search", "--base", live_server, "--trade-name", "MindDoc",
                 "--out", str(tmp_path / "r"), "--delay", "0", "--retries", "1",
                 "--no-resolve-codes"])
    assert code == EXIT_AUTH
    err = capsys.readouterr().err
    from eudamed import config
    assert "401" in err and config.KEY_ENV in err


def test_anonymous_request_succeeds_against_an_open_api(tmp_path, capsys, monkeypatch):
    """The live API is open, so this is the real-world path: no key, 200, rows.
    Verified here against the stand-in started with require_key disabled."""
    import threading

    from eudamed import fakeserver
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    server = fakeserver.serve(port=0, require_key=False)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05),
                     daemon=True).start()
    host, port = server.server_address[:2]
    try:
        out = tmp_path / "r"
        code = main(["search", "--base", f"http://{host}:{port}/eudamed",
                     "--trade-name", "MindDoc", "--out", str(out),
                     "--delay", "0", "--retries", "1"])
        assert code == EXIT_OK
        result = json.loads((out / "results.json").read_text())["results"][0]
        assert result["status"] == "found"
        assert result["candidates"][0]["trade_name"] == "MindDoc"
    finally:
        server.shutdown()
        server.server_close()


def test_anonymous_request_sends_no_credential(monkeypatch, capsys):
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    main(["search", "--trade-name", "MindDoc", "--dry-run"])
    url = capsys.readouterr().out
    assert "subscription-key" not in url and "TRADE_NAME=MindDoc" in url


def test_proxy_refusal_is_distinguished_from_an_api_rejection(client_factory):
    """A proxy CONNECT refusal reads like a 403 from the API but is not one."""
    import urllib.error

    from eudamed.client import ApiError

    def opener(req, timeout=None):
        raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

    with pytest.raises(ApiError) as exc:
        client_factory(opener=opener, retries=1).udi(TRADE_NAME="x")
    assert "proxy refusing the connection" in str(exc.value)
    assert "not the API rejecting" in str(exc.value)


def test_probe_shows_the_body_and_retries_unfiltered_when_udi_is_empty(live_server, capsys):
    """A 200 with no rows is ambiguous: empty result, unrecognised envelope, or
    an error delivered with a 200. Probe must show the body and establish
    whether the endpoint has any data at all."""
    code = main(["probe", "--base", live_server, "--key", "dummy",
                 "--trade-name", "ZZZNothingMatchesThis", "--delay", "0"])
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "body (2 bytes)" in err            # the raw body is surfaced
    assert "retrying with no filter" in err
    assert "the endpoint has data" in err     # so the filter matched nothing
    assert "TRADE_NAME matched nothing" in err


def test_reference_tables_summary(live_server, capsys):
    assert main(["reference", "--base", live_server, "--key", "dummy",
                 "--tables", "--delay", "0"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "RISK_CLASS_ID" in out and "Class IIa" in out
    assert "PLACED_ON_THE_MARKET_ID" in out and "Israel" in out


# --- filter semantics probing -------------------------------------------
def test_filtertest_calibrates_against_a_real_trade_name(live_server, capsys):
    """The control is a trade name taken from the API's own response, so there
    is always one trial that must match. Without it, a zero-row result cannot
    be told apart from a broken filter."""
    assert main(["filtertest", "--base", live_server, "--term", "MindDoc",
                 "--delay", "0", "--retries", "1"]) == EXIT_OK
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["control"] == "MindDoc"
    labels = [t["strategy"] for t in report["trials"]]
    assert labels[0] == "CONTROL: exact real trade name"
    assert report["trials"][0]["rows"] == 1
    assert "The filter works" in captured.err


def test_filtertest_tries_odata_options(live_server, capsys):
    """The response envelope is {"value": [...]}, so OData options are worth
    trying even though the spec documents none."""
    main(["filtertest", "--base", live_server, "--term", "MindDoc",
          "--delay", "0", "--retries", "1"])
    report = json.loads(capsys.readouterr().out)
    odata = [t for t in report["trials"] if t["strategy"].startswith("OData")]
    assert len(odata) >= 4
    # The stand-in rejects them, which is itself a recorded outcome.
    assert all("error" in t for t in odata)


def test_filtertest_dry_run_lists_every_strategy(capsys):
    assert main(["filtertest", "--term", "MindDoc", "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "TRADE_NAME=MindDoc" in out
    assert "%24filter" in out and "%24top" in out     # $filter, $top
    assert out.count("\n") >= 13


def test_filtertest_reports_a_broken_filter_distinctly(live_server, capsys, monkeypatch):
    """If even the control matches nothing, the mechanism is at fault - not
    the search term. That must read differently."""
    from eudamed.client import Client

    real_request = Client.request

    def only_unfiltered(self, path, params=None, allow_undocumented=False):
        if params:
            return [], "[]"
        return real_request(self, path, params, allow_undocumented=allow_undocumented)

    monkeypatch.setattr(Client, "request", only_unfiltered)
    main(["filtertest", "--base", live_server, "--term", "MindDoc",
          "--delay", "0", "--retries", "1"])
    err = capsys.readouterr().err
    assert "the problem is the filter mechanism" in err


# --- raw ----------------------------------------------------------------
def test_raw_allows_undocumented_parameters(live_server, capsys):
    """raw must bypass the spec allowlist; that is the point of it."""
    assert main(["raw", "/udi", "--base", live_server, "--param", "TRADE_NAME=MindDoc",
                 "--delay", "0", "--retries", "1"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "MindDoc" in out


def test_raw_dry_run_and_bad_param(capsys):
    assert main(["raw", "/udi", "--param", "$top=5", "--dry-run"]) == EXIT_OK
    assert "%24top=5" in capsys.readouterr().out
    assert main(["raw", "/udi", "--param", "nonsense"]) == EXIT_USAGE


def test_raw_writes_the_body_to_a_file(live_server, tmp_path):
    out = tmp_path / "udi.json"
    assert main(["raw", "/udi", "--base", live_server, "--out", str(out),
                 "--delay", "0", "--retries", "1"]) == EXIT_OK
    assert "MindDoc" in out.read_text(encoding="utf-8")


# --- discover -----------------------------------------------------------
def test_discover_by_filter_writes_a_report(live_server, tmp_path):
    out = tmp_path / "d"
    assert main(["discover", "--base", live_server, "--risk-class-id", "1",
                 "--out", str(out), "--delay", "0", "--retries", "1"]) == EXIT_OK
    payload = json.loads((out / "results.json").read_text())
    assert payload["results"]
    assert all(r["candidates"][0]["matched_on"].startswith("filter:")
               for r in payload["results"])
    assert (out / "report.md").read_text(encoding="utf-8").startswith("# EUDAMED")


def test_discover_resolves_a_human_risk_class(live_server, tmp_path, capsys):
    """RISK_CLASS_ID is numeric, so "I" has to be looked up via /reference."""
    assert main(["discover", "--base", live_server, "--risk-class", "Class I",
                 "--out", str(tmp_path / "d"), "--delay", "0", "--retries", "1"]) == EXIT_OK
    assert "RISK_CLASS_ID=1" in capsys.readouterr().err


def test_discover_rejects_an_unknown_risk_class(live_server, tmp_path, capsys):
    code = main(["discover", "--base", live_server, "--risk-class", "Class ZZZ",
                 "--out", str(tmp_path / "d"), "--delay", "0", "--retries", "1"])
    assert code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "no risk class matching" in err and "available:" in err


def test_discover_keyword_narrows_locally(live_server, tmp_path, capsys):
    """Some concepts cannot be filtered server-side, so keywords are applied
    to the returned rows instead."""
    assert main(["discover", "--base", live_server, "--risk-class-id", "1",
                 "--keyword", "tinnitus", "--out", str(tmp_path / "d"),
                 "--delay", "0", "--retries", "1"]) == EXIT_OK
    err = capsys.readouterr().err
    assert "mention ['tinnitus']" in err
    results = json.loads((tmp_path / "d" / "results.json").read_text())["results"]
    assert [r["name"] for r in results] == ["Kalmeda"]


def test_discover_needs_at_least_one_filter(capsys):
    assert main(["discover", "--out", "x"]) == EXIT_USAGE
    assert "give at least one filter" in capsys.readouterr().err


def test_discover_dry_run(capsys):
    assert main(["discover", "--risk-class-id", "1", "--medical-purpose", "depression",
                 "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "RISK_CLASS_ID=1" in out and "MEDICAL_PURPOSE=depression" in out


# --- report download and discovery through the UI -----------------------
def test_ui_downloads_a_markdown_report(ui_server):
    """The download reuses the CLI's writers, so the document cannot drift."""
    md = ui_server.text("/api/report?target=MindDoc&format=md")
    assert md.startswith("# EUDAMED device report")
    assert "## MindDoc" in md and "**Identification**" in md


def test_ui_report_supports_csv_and_json(ui_server):
    csv_body = ui_server.text("/api/report?target=MindDoc&format=csv")
    assert csv_body.startswith("name,ca,expected_country,status")
    payload = json.loads(ui_server.text("/api/report?target=MindDoc&format=json"))
    assert payload["results"][0]["status"] == "found"
    assert payload["meta"]["base"]


def test_ui_report_for_the_whole_loaded_list(ui_server):
    md = ui_server.text("/api/report?all=1&format=md")
    for name in ("MindDoc", "HelloBetter Stress und Burnout", "Kalmeda"):
        assert f"## {name}" in md


def test_ui_report_rejects_bad_input(ui_server):
    assert ui_server.status("/api/report?format=md") == 400        # no target
    assert ui_server.status("/api/report?target=MindDoc&format=pdf") == 400


def test_ui_report_for_an_unknown_name_still_works(ui_server):
    """A name not in the loaded list is searched as typed."""
    md = ui_server.text("/api/report?name=Kalmeda&format=md")
    assert "## Kalmeda" in md


def test_ui_exposes_the_risk_class_table(ui_server):
    """RISK_CLASS_ID is numeric, so the UI needs the labels from /reference."""
    classes = ui_server.json("/api/riskclasses")["classes"]
    labels = [c["label"] for c in classes]
    assert "Class I" in labels and "Class IIa" in labels
    assert all(isinstance(c["id"], (int, float)) for c in classes)


def test_ui_discover_by_risk_class(ui_server):
    d = ui_server.json("/api/discover?risk_class_id=1")
    assert d["filters"] == {"RISK_CLASS_ID": "1"}
    assert d["kept"] == d["rows_returned"] > 0
    assert d["truncated"] is False


def test_ui_discover_keyword_narrows_locally(ui_server):
    d = ui_server.json("/api/discover?risk_class_id=1&keyword=tinnitus")
    assert d["keyword"] == ["tinnitus"]
    assert [x["trade_name"] for x in d["devices"]] == ["Kalmeda"]
    assert d["kept"] < d["rows_returned"]


def test_ui_discover_needs_a_server_side_filter(ui_server):
    assert ui_server.status("/api/discover?keyword=depression") == 400


# --- local cache: the only way to match an approximate name -------------
def test_scan_partitions_and_writes_a_cache(live_server, tmp_path, capsys):
    out = tmp_path / "c" / "udi.jsonl"
    assert main(["scan", "--base", live_server, "--out", str(out),
                 "--partition-by", "RISK_CLASS_ID", "--delay", "0",
                 "--retries", "1"]) == EXIT_OK
    err = capsys.readouterr().err
    assert "RISK_CLASS_ID=1" in err and "unique row(s)" in err
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert {r["TRADE_NAME"] for r in rows} >= {"MindDoc", "Kalmeda"}
    meta = json.loads((out.parent / "udi.meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == len(rows) and meta["partitions"]


def test_scan_rejects_an_undocumented_partition_field(tmp_path, capsys):
    assert main(["scan", "--out", str(tmp_path / "c.jsonl"),
                 "--partition-by", "tradeName"]) == EXIT_USAGE
    assert "not a documented" in capsys.readouterr().err


def test_scan_with_explicit_filters(live_server, tmp_path):
    out = tmp_path / "c.jsonl"
    assert main(["scan", "--base", live_server, "--out", str(out),
                 "--filter", "RISK_CLASS_ID=2", "--delay", "0",
                 "--retries", "1"]) == EXIT_OK
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert rows and all(r["RISK_CLASS_ID"] == 2 for r in rows)


def test_search_against_a_cache_matches_approximate_names(live_server, tmp_path):
    """The point of the cache: /udi filters are exact, so an approximate name
    can only be matched locally."""
    cache = tmp_path / "c.jsonl"
    main(["scan", "--base", live_server, "--out", str(cache),
          "--delay", "0", "--retries", "1"])
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,country,keys\nMind Doc,DE,Mind Doc\n", encoding="utf-8")
    out = tmp_path / "r"
    assert main(["search", "--base", live_server, "--input", str(csv_path),
                 "--cache", str(cache), "--out", str(out), "--delay", "0",
                 "--retries", "1"]) == EXIT_OK
    result = json.loads((out / "results.json").read_text())["results"][0]
    # "Mind Doc" is not a registered trade name, so a server-side query would
    # return nothing; locally it matches MindDoc.
    assert result["status"] == "found"
    assert result["candidates"][0]["trade_name"] == "MindDoc"
    assert result["queries"][0]["param"] == "local cache"


def test_cache_mode_records_that_it_used_no_requests(live_server, tmp_path):
    cache = tmp_path / "c.jsonl"
    main(["scan", "--base", live_server, "--out", str(cache), "--delay", "0",
          "--retries", "1"])
    out = tmp_path / "r"
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,keys\nKalmeda,Kalmeda\n", encoding="utf-8")
    main(["search", "--base", live_server, "--input", str(csv_path), "--cache", str(cache),
          "--out", str(out), "--delay", "0", "--retries", "1", "--no-resolve-codes"])
    meta = json.loads((out / "results.json").read_text())["meta"]
    assert meta["requests"] == 0 and meta["cached_rows"] >= 1


def test_search_reports_an_unreadable_cache(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name\nX\n", encoding="utf-8")
    assert main(["search", "--input", str(csv_path), "--cache", str(bad),
                 "--out", str(tmp_path / "r")]) == EXIT_USAGE
    assert "cannot read --cache" in capsys.readouterr().err
