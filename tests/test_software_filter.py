"""--software-only: filter on what the record says, not on words in the name.

The motivating case is in the fixtures. "SleepMind Positioning Cushion" is a
cushion whose trade name contains both "sleep" and "mind", so any word-based
sweep for psychological software pulls it in. Its EMDN code does not start
Z12, so the record itself rules it out.
"""

import json

import pytest

from eudamed.cli import EXIT_OK, main
from eudamed.client import Client
from eudamed.search import Target, search_target
from eudamed.ui_backend import UiClient

DL = ("--backend", "datalake", "--no-widen")
#: discover never widens, so it has no --no-widen flag to pin.
DISC = ("--backend", "datalake")


@pytest.fixture
def dl(live_server):
    return Client(base=live_server, key="dummy", delay=0, backoff_base=0)


def sweep(path, *terms):
    path.write_text(
        "name,description,ca,country,keys,broad\n"
        + "".join(f"{t},probe,,,{t},\n" for t in terms), encoding="utf-8")
    return str(path)


# --- the filter itself ------------------------------------------------------
def test_a_hardware_hit_is_kept_without_the_filter(dl):
    """Baseline: the noise is real, and the tool does return it by default."""
    result = search_target(dl, Target("SleepMind Positioning Cushion"),
                           fields="TRADE_NAME", min_score=0.0)
    assert result["status"] == "found"
    best = result["candidates"][0]
    assert best["device_kind"] == "other"
    assert "outside the software category" in best["device_kind_reason"]


def test_software_only_drops_it(dl):
    result = search_target(dl, Target("SleepMind Positioning Cushion"),
                           fields="TRADE_NAME", min_score=0.0, software_only=True)
    assert result["candidates"] == []
    assert result["dropped_kinds"]["other"] == 1
    assert result["software_only"] is True


def test_software_only_keeps_software(dl):
    result = search_target(dl, Target("MindDoc"), fields="TRADE_NAME,DEVICE_NAME",
                           min_score=0.0, software_only=True)
    assert result["status"] == "found"
    assert result["candidates"][0]["trade_name"] == "MindDoc: Your Companion"
    assert sum(result["dropped_kinds"].values()) == 0


def test_an_undetermined_device_is_dropped_but_counted_separately(dl):
    """"Mindful Monitor" has neither an EMDN code nor a special device type.

    It is dropped - the caller asked for software - but it is counted as
    undetermined, not as non-software, so a sparsely populated field is
    visible rather than looking like a register full of hardware.
    """
    result = search_target(dl, Target("Mindful Monitor"), fields="TRADE_NAME",
                           min_score=0.0, software_only=True)
    assert result["candidates"] == []
    assert result["dropped_kinds"] == {"other": 0, "unknown": 1}


def test_filtering_does_not_change_the_error_status(dl):
    """A filtered-away candidate is "not found", never "error"."""
    result = search_target(dl, Target("SleepMind Positioning Cushion"),
                           fields="TRADE_NAME", min_score=0.0, software_only=True)
    assert result["status"] == "not found"


def test_the_filter_applies_to_substring_fallback_hits_too(dl, live_server):
    """The widen pass must not smuggle hardware past the filter."""
    widen = UiClient(base=live_server, delay=0, backoff_base=0)
    target = Target("SleepMind", keys=["SleepMind"])
    plain = search_target(dl, target, fields="TRADE_NAME", min_score=0.0,
                          widen_client=widen)
    assert [c["trade_name"] for c in plain["candidates"]] == [
        "SleepMind Positioning Cushion"]
    assert plain["candidates"][0]["matched_via"] == "ui-substring"

    filtered = search_target(dl, target, fields="TRADE_NAME", min_score=0.0,
                             widen_client=widen, software_only=True)
    assert filtered["candidates"] == []
    assert sum(filtered["dropped_kinds"].values()) >= 1


def test_the_ui_backend_needs_the_detail_lookup_to_classify_anything(live_server):
    """The defect this feature would have shipped with.

    The web-UI backend's list rows carry no nomenclature code, so classifying
    from the row alone marks every device undetermined and --software-only
    empties the result. With a Typer the detail record supplies the code.
    """
    from eudamed.devicetype import Typer

    ui = UiClient(base=live_server, delay=0, backoff_base=0)
    target = Target("MindDoc", keys=["MindDoc"])

    without = search_target(ui, target, fields="TRADE_NAME", min_score=0.0,
                            software_only=True)
    assert without["candidates"] == []
    assert without["dropped_kinds"]["unknown"] >= 1

    typer = Typer(detail_client=ui)
    with_detail = search_target(ui, target, fields="TRADE_NAME", min_score=0.0,
                                software_only=True, typer=typer)
    assert [c["trade_name"] for c in with_detail["candidates"]] == [
        "MindDoc: Your Companion"]
    assert "detail record" in with_detail["candidates"][0]["device_kind_reason"]
    assert typer.detail_requests == 1


# --- CLI --------------------------------------------------------------------
def test_cli_search_software_only(live_server, tmp_path, capsys):
    out = tmp_path / "res"
    code = main(["search", *DL, "--base", live_server, "--key", "dummy",
                 "--input", sweep(tmp_path / "s.csv", "mind", "sleep"),
                 "--fields", "TRADE_NAME", "--software-only",
                 "--out", str(out), "--delay", "0"])
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "--software-only dropped" in err
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert payload["meta"]["software_only"] is True


def test_cli_search_reports_what_the_filter_removed(live_server, tmp_path, capsys):
    code = main(["search", *DL, "--base", live_server, "--key", "dummy",
                 "--trade-name", "SleepMind Positioning Cushion",
                 "--fields", "TRADE_NAME", "--software-only",
                 "--out", str(tmp_path / "r"), "--delay", "0"])
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "dropped as not software" in err


def test_cli_discover_software_only(live_server, tmp_path, capsys):
    """discover returns whole result pages, so the filter matters most here."""
    out = tmp_path / "disc"
    code = main(["discover", *DISC, "--base", live_server, "--key", "dummy",
                 "--risk-class-id", "1", "--software-only",
                 "--out", str(out), "--delay", "0"])
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "--software-only dropped" in err
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    kept = [r["candidates"][0]["trade_name"] for r in payload["results"]]
    assert "SleepMind Positioning Cushion" not in kept
    assert "Mindful Monitor" not in kept
    assert "Kalmeda" in kept


def test_cli_discover_without_the_filter_keeps_the_hardware(live_server, tmp_path):
    out = tmp_path / "disc2"
    code = main(["discover", *DISC, "--base", live_server, "--key", "dummy",
                 "--risk-class-id", "1", "--out", str(out), "--delay", "0"])
    assert code == EXIT_OK
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    kept = [r["candidates"][0]["trade_name"] for r in payload["results"]]
    assert "SleepMind Positioning Cushion" in kept


# --- reports ----------------------------------------------------------------
def test_markdown_says_found_then_filtered_rather_than_not_found(dl, tmp_path):
    """The report must not claim absence from the register."""
    from eudamed.report import write_markdown

    result = search_target(dl, Target("SleepMind Positioning Cushion"),
                           fields="TRADE_NAME", min_score=0.0, software_only=True)
    path = tmp_path / "report.md"
    write_markdown([result], path, {"software_only": True})
    md = path.read_text(encoding="utf-8")
    assert "Found, then filtered out." in md
    assert "not an absence from the register" in md
    assert "software only" in md.lower()


def test_csv_carries_the_device_kind(dl, tmp_path):
    import csv as csvmod

    from eudamed.report import write_csv

    result = search_target(dl, Target("MindDoc"), fields="DEVICE_NAME", min_score=0.0)
    path = tmp_path / "out.csv"
    write_csv([result], path)
    rows = list(csvmod.DictReader(path.open(encoding="utf-8")))
    assert rows[0]["device_kind"] == "software"
    assert "Z12" in rows[0]["device_kind_reason"]


def test_markdown_omits_an_undetermined_kind(dl, tmp_path):
    """"unknown" only restates that two fields were blank, so it is not printed."""
    from eudamed.report import write_markdown

    result = search_target(dl, Target("Mindful Monitor"), fields="TRADE_NAME",
                           min_score=0.0)
    path = tmp_path / "r.md"
    write_markdown([result], path, {})
    md = path.read_text(encoding="utf-8")
    assert "Device type (derived)" not in md


# --- web UI -----------------------------------------------------------------
def test_ui_search_honours_software_only(ui_server):
    plain = ui_server.json("/api/search?name=SleepMind+Positioning+Cushion")
    assert plain["candidates"]
    filtered = ui_server.json(
        "/api/search?name=SleepMind+Positioning+Cushion&software_only=1")
    assert filtered["candidates"] == []
    assert filtered["dropped_kinds"]["other"] == 1


def test_ui_discover_honours_software_only(ui_server):
    payload = ui_server.json("/api/discover?risk_class_id=1&software_only=1")
    names = [d["trade_name"] for d in payload["devices"]]
    assert "SleepMind Positioning Cushion" not in names
    assert payload["software_only"] is True
    assert sum(payload["dropped_kinds"].values()) >= 1


def test_ui_health_reports_whether_a_detail_endpoint_exists(ui_server):
    """The page needs this to warn that the filter cannot resolve blank rows."""
    assert ui_server.json("/api/health")["has_detail"] is False


def test_the_page_words_a_filtered_result_as_filtered_not_not_found():
    """Browser-verified; guarded here so the wording cannot quietly regress."""
    from eudamed.webui import PAGE

    assert "filtered out" in PAGE
    assert "Found, then filtered out." in PAGE
    assert "dropCount(d)" in PAGE


def test_the_download_bar_can_actually_be_hidden():
    """`.dl{display:flex}` beats the hidden attribute's UA display:none.

    Without an explicit rule, `dl.hidden = true` does nothing and download
    links from the previous query stay clickable under a new result - so a
    report could be downloaded for something other than what is on screen.
    """
    from eudamed.webui import PAGE

    assert ".dl[hidden]{display:none}" in PAGE
