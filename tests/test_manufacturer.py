"""Search by manufacturer: /actors NAME -> MF_SRN -> /udi.

/udi has no manufacturer-name filter, so the two-step lookup is the whole
feature. These run over a real socket against the bundled fake API, which
matches the datalake routes exactly and the web-UI routes as substrings, the
way the live APIs do.
"""

import json

import pytest

from eudamed.cli import EXIT_OK, EXIT_USAGE, main
from eudamed.client import Client
from eudamed.search import manufacturer_results, search_manufacturer
from eudamed.ui_backend import UiClient

DL = ("--backend", "datalake", "--no-widen")


@pytest.fixture
def dl(live_server):
    return Client(base=live_server, key="dummy", delay=0, backoff_base=0)


@pytest.fixture
def ui(live_server):
    """The web-UI backend pointed at the fake server's substring routes."""
    return UiClient(base=live_server, delay=0, backoff_base=0)


def test_exact_actor_name_finds_that_actors_devices(dl):
    found = search_manufacturer(dl, "MindDoc Health GmbH")
    assert found["srns"] == ["DE-MF-000025123"]
    names = sorted(d["trade_name"] for d in found["devices"])
    assert names == ["MindDoc: Your Companion", "Moodpath"]
    # An MF_SRN hit is a match by construction, not a name similarity.
    assert all(d["matched_on"].startswith("identifier:MF_SRN") for d in found["devices"])
    assert all(d["score"] == 1.0 for d in found["devices"])


def test_a_partial_company_name_finds_nothing_on_the_documented_api(dl):
    """The constraint that makes the fallback necessary: /actors is exact."""
    found = search_manufacturer(dl, "PINK")
    assert found["srns"] == []
    assert found["devices"] == []


def test_the_substring_fallback_reaches_a_partial_company_name(dl, ui):
    found = search_manufacturer(dl, "PINK", actor_client=ui)
    assert found["srns"] == ["DE-MF-000031007"]
    assert [d["trade_name"] for d in found["devices"]] == [
        "PINK Coach - Breast Cancer Companion"]
    # Provenance: reached through the undocumented backend, so it is marked.
    assert found["actors"][0]["matched_via"] == "ui-substring"
    assert any(q.get("backend") == "ui" for q in found["queries"])


def test_the_fallback_is_not_used_when_the_exact_lookup_succeeds(dl, ui):
    found = search_manufacturer(dl, "Vitadio s.r.o.", actor_client=ui)
    assert found["srns"] == ["CZ-MF-000077001"]
    assert [q.get("backend") for q in found["queries"]] == ["primary", None]
    assert found["actors"][0]["matched_via"] == ""


def test_a_known_srn_skips_the_actor_lookup_entirely(dl):
    found = search_manufacturer(dl, "", srns=["DE-MF-000099001"])
    assert [q["param"] for q in found["queries"]] == ["MF_SRN"]
    assert [d["trade_name"] for d in found["devices"]] == ["Kalmeda"]
    assert found["actors"] == []


def test_duplicate_srns_are_queried_once(dl):
    found = search_manufacturer(dl, "", srns=["DE-MF-000099001", "DE-MF-000099001"])
    assert found["srns"] == ["DE-MF-000099001"]
    assert len(found["queries"]) == 1


def test_software_only_drops_a_manufacturers_non_software_devices(dl):
    """Schlafkomfort registers one hardware device and one undetermined one."""
    plain = search_manufacturer(dl, "Schlafkomfort GmbH")
    assert len(plain["devices"]) == 2

    filtered = search_manufacturer(dl, "Schlafkomfort GmbH", software_only=True)
    assert filtered["devices"] == []
    assert filtered["dropped_kinds"] == {"other": 1, "unknown": 1}
    # The distinction that matters: the actor WAS found and DOES have devices.
    assert filtered["srns"] == ["DE-MF-000044001"]


def test_a_request_error_is_recorded_rather_than_raised(dl):
    dl.base = "http://127.0.0.1:1/eudamed"      # nothing listening
    dl.retries = 1
    found = search_manufacturer(dl, "", srns=["DE-MF-000099001"])
    assert found["devices"] == []
    assert found["errors"] and "MF_SRN" in found["errors"][0]


def test_manufacturer_results_reshapes_for_the_report_writers(dl):
    found = search_manufacturer(dl, "MindDoc Health GmbH")
    results = manufacturer_results(found)
    assert {r["status"] for r in results} == {"found"}
    assert all(len(r["candidates"]) == 1 for r in results)
    assert sorted(r["name"] for r in results) == ["MindDoc: Your Companion", "Moodpath"]


# --- CLI --------------------------------------------------------------------
def test_cli_writes_a_report(live_server, tmp_path):
    out = tmp_path / "mfr"
    code = main(["manufacturer", *DL, "--base", live_server, "--key", "dummy",
                 "--name", "MindDoc Health GmbH", "--out", str(out), "--delay", "0"])
    assert code == EXIT_OK
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert len(payload["results"]) == 2
    assert "MF_SRN" in payload["meta"]["fields"]
    for kind in ("results.csv", "report.md", "report.html"):
        assert (out / kind).exists()


def test_cli_needs_a_name_or_an_srn(capsys):
    assert main(["manufacturer", "--base", "http://x/eudamed"]) == EXIT_USAGE


def test_cli_dry_run_shows_the_actor_lookup_it_would_make(live_server, capsys):
    code = main(["manufacturer", *DL, "--base", live_server, "--name", "GAIA AG",
                 "--dry-run"])
    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert "/actors?" in printed and "NAME=GAIA+AG" in printed


def test_cli_dry_run_with_an_srn_shows_the_device_query(live_server, capsys):
    code = main(["manufacturer", *DL, "--base", live_server,
                 "--srn", "DE-MF-000025123", "--dry-run"])
    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert "/udi?" in printed and "MF_SRN=DE-MF-000025123" in printed


def test_cli_reports_no_actor_without_inventing_one(live_server, capsys):
    code = main(["manufacturer", *DL, "--base", live_server, "--key", "dummy",
                 "--name", "No Such Company GmbH", "--delay", "0"])
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "no actor matched" in err
    assert "matches the name exactly" in err


def test_cli_uses_the_ui_backend_actor_path(live_server, capsys):
    """--backend ui must hit /actors/actorDataPublicView, not /actors."""
    code = main(["manufacturer", "--backend", "ui", "--base", live_server,
                 "--name", "PINK", "--dry-run"])
    assert code == EXIT_OK
    assert "/actors/actorDataPublicView?" in capsys.readouterr().out


# --- web UI -----------------------------------------------------------------
def test_ui_manufacturer_route(ui_server):
    payload = ui_server.json("/api/manufacturer?name=MindDoc+Health+GmbH")
    assert payload["srns"] == ["DE-MF-000025123"]
    assert len(payload["devices"]) == 2


def test_ui_manufacturer_route_accepts_an_srn(ui_server):
    payload = ui_server.json("/api/manufacturer?srn=DE-MF-000099001")
    assert [d["trade_name"] for d in payload["devices"]] == ["Kalmeda"]


def test_ui_manufacturer_route_needs_an_argument(ui_server):
    assert ui_server.status("/api/manufacturer") == 400


def test_ui_manufacturer_route_honours_software_only(ui_server):
    plain = ui_server.json("/api/manufacturer?name=Schlafkomfort+GmbH")
    assert len(plain["devices"]) == 2
    filtered = ui_server.json(
        "/api/manufacturer?name=Schlafkomfort+GmbH&software_only=1")
    assert filtered["devices"] == []
    assert sum(filtered["dropped_kinds"].values()) == 2
