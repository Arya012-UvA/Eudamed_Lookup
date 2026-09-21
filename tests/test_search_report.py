"""Search orchestration, CSV input handling and the three output writers."""

import csv
import json
from pathlib import Path

import pytest
from conftest import error_opener, json_opener

from eudamed.client import AuthError
from eudamed.reference import Reference
from eudamed.report import safe_json, write_all, write_csv, write_html
from eudamed.search import Target, load_targets, run, search_target

ROWS = [
    {"TRADE_NAME": "MindDoc", "MF_NAME": "MindDoc Health GmbH", "MF_SRN": "DE-MF-1",
     "PRIMARY_DI": "111", "RISK_CLASS_ID": 2, "UUID": "u1", "LATEST_VERSION": True},
    {"TRADE_NAME": "Moodpath", "MF_NAME": "MindDoc Health GmbH", "MF_SRN": "DE-MF-1",
     "PRIMARY_DI": "222", "RISK_CLASS_ID": 1, "UUID": "u2", "LATEST_VERSION": True},
]


# --- input loading ----------------------------------------------------
def test_load_targets(devices_csv):
    targets = load_targets(devices_csv)
    assert [t.name for t in targets] == ["MindDoc", "Kalmeda", "NotRegistered"]
    assert targets[0].keys == ["MindDoc"] and targets[0].broad == ["Mind Doc"]
    assert targets[0].country == "DE"


def test_keys_fall_back_to_name(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("name\nMindDoc\n", encoding="utf-8")
    assert load_targets(str(p))[0].keys == ["MindDoc"]


def test_headers_are_case_and_space_insensitive(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(" Name , Country , Keys \nMindDoc, de ,MindDoc|Mind Doc\n", encoding="utf-8")
    t = load_targets(str(p))[0]
    assert t.name == "MindDoc" and t.country == "DE" and len(t.keys) == 2


def test_bom_is_tolerated(tmp_path):
    p = tmp_path / "d.csv"
    p.write_bytes("﻿name,country\nMindDoc,DE\n".encode())
    assert load_targets(str(p))[0].name == "MindDoc"


def test_blank_rows_skipped(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("name\nMindDoc\n\n,\nKalmeda\n", encoding="utf-8")
    assert len(load_targets(str(p))) == 2


def test_missing_name_column_is_a_clear_error(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("device,country\nMindDoc,DE\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'name' column"):
        load_targets(str(p))


def test_empty_file_is_a_clear_error(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_targets(str(p))


# --- orchestration ----------------------------------------------------
def test_search_target_ranks_and_flags_evidence(client_factory):
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("MindDoc", country="DE", keys=["MindDoc"]))
    assert result["status"] == "found"
    assert [c["trade_name"] for c in result["candidates"]] == ["MindDoc", "Moodpath"]
    assert result["candidates"][0]["matched_on"] == "trade_name:exact"
    assert result["candidates"][1]["matched_on"] == "manufacturer"
    assert result["candidates"][1]["score"] < 0.6


def test_manufacturer_lead_never_becomes_the_status(client_factory):
    """Only the sibling device exists: status must be 'not found', not 'found'."""
    client = client_factory(opener=json_opener([ROWS[1]]))
    result = search_target(client, Target("MindDoc", country="DE", keys=["MindDoc"]))
    assert result["status"] == "not found"
    assert result["candidates"][0]["matched_on"] == "manufacturer"


def test_deduplicates_across_query_terms(client_factory):
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("MindDoc", keys=["MindDoc", "Mind Doc"],
                                          broad=["MindDoc Health"]))
    assert result["total_matches"] == 2          # 3 terms, same 2 devices
    assert len(result["queries"]) == 3


def test_multiple_fields_are_searched(client_factory):
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("MindDoc", keys=["MindDoc"]),
                           fields="TRADE_NAME,DEVICE_NAME")
    assert {q["param"] for q in result["queries"]} == {"TRADE_NAME", "DEVICE_NAME"}


def test_min_score_and_top(client_factory):
    client = client_factory(opener=json_opener(ROWS))
    tight = search_target(client, Target("MindDoc", keys=["MindDoc"]), min_score=0.9)
    assert len(tight["candidates"]) == 1
    capped = search_target(client, Target("MindDoc", keys=["MindDoc"]), top=1)
    assert len(capped["candidates"]) == 1


def test_request_errors_report_error_not_not_found(client_factory):
    """If every query failed, the register was never consulted. Reporting
    "not found" would assert a device is unregistered when nothing was
    checked - a materially wrong answer, not a cosmetic one."""
    client = client_factory(opener=error_opener(500), retries=1)
    result = search_target(client, Target("MindDoc", keys=["MindDoc"]))
    assert result["status"] == "error"
    assert result["errors"] and "500" in result["errors"][0]
    assert result["queries"][0]["error"]


def test_partial_failure_still_reports_not_found(client_factory):
    """One query failing while another succeeds and returns nothing is a
    genuine 'not found', not an error."""
    calls = {"n": 0}

    def opener(req, timeout=None):
        import urllib.error

        from conftest import FakeResp
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)
        return FakeResp(b"[]")

    client = client_factory(opener=opener, retries=1)
    result = search_target(client, Target("MindDoc", keys=["MindDoc", "Mind Doc"]))
    assert result["status"] == "not found"
    assert len(result["errors"]) == 1


def test_auth_error_aborts_the_whole_run(client_factory):
    """A bad key is not a per-device problem; fail fast instead of 23 times."""
    client = client_factory(opener=error_opener(401))
    with pytest.raises(AuthError):
        run(client, [Target("A"), Target("B")])


def test_response_fields_are_reported(client_factory):
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("MindDoc", keys=["MindDoc"]))
    assert "TRADE_NAME" in result["response_fields"]


def test_reference_labels_are_applied(client_factory):
    """Real shape: (CODE, ID) -> VALUE, where CODE names the code table."""
    ref = Reference(client_factory(opener=json_opener([
        {"ID": 1.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class I"},
        {"ID": 2.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class IIa"},
    ]))).load()
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("MindDoc", keys=["MindDoc"]), reference=ref)
    assert result["candidates"][0]["risk_class"] == "Class IIa"


def test_reference_disambiguates_by_code_table(client_factory):
    """The same id means different things in different tables. A lookup keyed on
    ID alone - which is what this resolver used to do - mislabels fields."""
    ref = Reference(client_factory(opener=json_opener([
        {"ID": 1.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class I"},
        {"ID": 1.0, "CODE": "APPLICABLE_LEGISLATION_ID", "LANGUAGE": "en",
         "VALUE": "Regulation (EU) 2017/745"},
        {"ID": -101.0, "CODE": "PLACED_ON_THE_MARKET_ID", "LANGUAGE": "en",
         "VALUE": "Israel"},
    ]))).load()
    assert ref.label("RISK_CLASS_ID", 1) == "Class I"
    assert ref.label("APPLICABLE_LEGISLATION_ID", 1) == "Regulation (EU) 2017/745"
    assert ref.label("PLACED_ON_THE_MARKET_ID", -101.0) == "Israel"
    assert set(ref.tables) == {"RISK_CLASS_ID", "APPLICABLE_LEGISLATION_ID",
                               "PLACED_ON_THE_MARKET_ID"}


def test_reference_normalises_numeric_ids(client_factory):
    """IDs arrive as JSON floats and may be negative; -101.0 must equal -101."""
    from eudamed.reference import normalise_id
    assert normalise_id(-101.0) == normalise_id(-101) == -101
    assert normalise_id("2") == 2
    assert normalise_id(None) is None
    assert normalise_id(2.5) == 2.5


def test_reference_failure_is_not_fatal(client_factory):
    ref = Reference(client_factory(opener=error_opener(500), retries=1)).load()
    assert ref.error
    assert ref.label("RISK_CLASS_ID", 2) == "2"     # falls back to the raw id


def test_reference_unknown_id_falls_back_to_the_number(client_factory):
    ref = Reference(client_factory(opener=json_opener([
        {"ID": 1.0, "CODE": "RISK_CLASS_ID", "LANGUAGE": "en", "VALUE": "Class I"},
    ]))).load()
    assert ref.label("RISK_CLASS_ID", 99) == "99"
    assert ref.label("NO_SUCH_TABLE", 1) == "1"
    assert ref.label("RISK_CLASS_ID", None) == ""


# --- writers ----------------------------------------------------------
def _results(client_factory):
    client = client_factory(opener=json_opener(ROWS))
    return [search_target(client, Target("MindDoc", country="DE", keys=["MindDoc"],
                                         description="psych", ca="Bavaria DE")),
            search_target(client, Target("Nope", country="DE", keys=["ZZZNothing"]))]


def test_write_all_produces_three_files(tmp_path, client_factory):
    paths = write_all(_results(client_factory), str(tmp_path / "out"), {"base": "x"})
    for kind in ("json", "csv", "html"):
        assert paths[kind] and Path(paths[kind]).read_text(encoding="utf-8")


def test_json_has_meta_and_results(tmp_path, client_factory):
    paths = write_all(_results(client_factory), str(tmp_path / "out"), {"base": "x"})
    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert payload["meta"]["base"] == "x"
    assert payload["results"][0]["status"] == "found"


def test_csv_flat_row_and_not_found_row(tmp_path, client_factory):
    path = tmp_path / "r.csv"
    write_csv(_results(client_factory), path)
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["name"] == "MindDoc" and rows[0]["matched_on"] == "trade_name:exact"
    assert rows[0]["risk_class"] in ("", "2", "CLASS_IIA")
    assert rows[1]["status"] == "not found" and rows[1]["trade_name"] == ""


def test_csv_does_not_promote_a_sub_threshold_lead(tmp_path, client_factory):
    """A manufacturer-only lead must not appear in the flat 'best match' columns."""
    client = client_factory(opener=json_opener([ROWS[1]]))
    result = search_target(client, Target("MindDoc", country="DE", keys=["MindDoc"]))
    path = tmp_path / "r.csv"
    write_csv([result], path)
    with path.open(encoding="utf-8") as handle:
        row = next(iter(csv.DictReader(handle)))
    assert row["status"] == "not found"
    assert row["trade_name"] == ""          # Moodpath must NOT be presented as the match
    assert row["candidates"] == "1"         # but it is still counted as a lead


def test_html_substitution_is_single_pass(tmp_path):
    """Data containing a placeholder token must not corrupt the next replace."""
    results = [{"name": "__META__", "description": "__DATA__", "ca": "", "country": "DE",
                "status": "not found", "candidates": [], "queries": [], "errors": [],
                "total_matches": 0}]
    path = tmp_path / "r.html"
    write_html(results, path, {"generated": "now"})
    html = path.read_text(encoding="utf-8")
    body = html.split("const DATA = ")[1].split(", META = ")[0]
    assert json.loads(body)[0]["name"] == "__META__"


def test_html_escapes_script_injection(tmp_path):
    results = [{"name": "</script><script>alert(1)</script>", "description": "",
                "ca": "", "country": "", "status": "not found", "candidates": [],
                "queries": [], "errors": [], "total_matches": 0}]
    path = tmp_path / "r.html"
    write_html(results, path)
    html = path.read_text(encoding="utf-8")
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html


def test_safe_json_escapes_line_separators():
    assert "\u2028" not in safe_json({"a": "x\u2028y"})


def test_html_is_valid_without_candidates(tmp_path, client_factory):
    paths = write_all(_results(client_factory), str(tmp_path / "out"))
    html = Path(paths["html"]).read_text(encoding="utf-8")
    assert "__DATA__" not in html and "__META__" not in html


def test_identifier_key_term_scores_as_a_match(client_factory):
    """An SRN typed as the search term itself is an identifier match."""
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("DE-MF-1", keys=["DE-MF-1"]), fields="MF_SRN")
    assert result["status"] == "found"
    assert all(c["matched_on"] == "identifier:MF_SRN" for c in result["candidates"])
    assert len(result["candidates"]) == 2


def test_identifier_in_broad_does_not_become_a_match(client_factory):
    """A `broad` term is a recall helper. An SRN there must not turn every
    device from that manufacturer into a full-confidence hit."""
    client = client_factory(opener=json_opener(ROWS))
    result = search_target(client, Target("MindDoc", country="DE", keys=["MindDoc"],
                                          broad=["DE-MF-1"]), fields="TRADE_NAME,MF_SRN")
    by_name = {c["trade_name"]: c for c in result["candidates"]}
    assert by_name["MindDoc"]["matched_on"] == "trade_name:exact"
    assert by_name["Moodpath"]["matched_on"] == "manufacturer"
    assert by_name["Moodpath"]["score"] < 0.6
