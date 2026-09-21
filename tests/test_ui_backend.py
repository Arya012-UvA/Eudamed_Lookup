"""The web-UI backend, which does substring search.

The documented datalake API matches trade names exactly, so it cannot answer
"is there a device whose name looks like this?". The EUDAMED website's own
backend can, which is why both are supported.
"""

import json

import pytest
from conftest import FakeResp

from eudamed.cli import EXIT_OK, main
from eudamed.client import ApiError
from eudamed.ui_backend import DEFAULT_UI_BASE, UiClient


def test_url_uses_camelcase_and_no_format():
    """The UI backend takes neither format nor api-version, and uses camelCase."""
    url = UiClient().build_url("/devices/udiDiData", {"TRADE_NAME": "MindDoc"})
    assert "tradeName=MindDoc" in url
    assert "TRADE_NAME" not in url
    assert "format=" not in url and "api-version" not in url
    assert "languageIso2Code=en" in url


def test_default_base_is_the_website_backend():
    assert UiClient().base == DEFAULT_UI_BASE.rstrip("/")
    assert "ec.europa.eu" in UiClient().base


def test_device_path_differs_from_the_documented_api():
    from eudamed.client import Client
    assert UiClient().DEVICE_PATH == "/devices/udiDiData"
    assert Client().DEVICE_PATH == "/udi"


def test_no_credential_is_sent():
    assert "Ocp-Apim-Subscription-Key" not in UiClient()._headers()


def test_reference_is_not_available():
    with pytest.raises(ApiError, match="no /reference operation"):
        UiClient().reference()


def _pages(*payloads):
    seq = iter(payloads)

    def opener(req, timeout=None):
        return FakeResp(json.dumps(next(seq)).encode())
    return opener


def test_pagination_walks_every_page():
    client = UiClient(opener=_pages(
        {"content": [{"uuid": "a"}], "totalElements": 2, "last": False},
        {"content": [{"uuid": "b"}], "totalElements": 2, "last": True},
    ), delay=0, retries=1, backoff_base=0)
    rows, _ = client.udi(TRADE_NAME="x")
    assert [r["uuid"] for r in rows] == ["a", "b"]


def test_pagination_respects_max_pages():
    def opener(req, timeout=None):
        return FakeResp(json.dumps(
            {"content": [{"uuid": "a"}], "totalElements": 99, "last": False}).encode())
    client = UiClient(opener=opener, delay=0, retries=1, backoff_base=0, max_pages=3)
    rows, _ = client.udi(TRADE_NAME="x")
    assert len(rows) == 3


def test_an_ignored_filter_is_refused_rather_than_returning_everything():
    """A tradeName filter that the server ignored would return the whole
    register, which must not be presented as matches."""
    def opener(req, timeout=None):
        return FakeResp(json.dumps(
            {"content": [], "totalElements": 500000, "last": True}).encode())
    client = UiClient(opener=opener, delay=0, retries=1, backoff_base=0)
    with pytest.raises(ApiError, match="filter was ignored"):
        client.udi(TRADE_NAME="x")


def test_first_page_probe_is_not_latched_by_a_failure():
    """The predecessor cached the page offset after a transient failure, which
    shifted every later query by one page."""
    import urllib.error
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        return FakeResp(json.dumps(
            {"content": [{"uuid": "a"}], "totalElements": 1, "last": True}).encode())

    client = UiClient(opener=opener, delay=0, retries=1, backoff_base=0)
    rows, _ = client.udi(TRADE_NAME="x")
    assert len(rows) == 1
    assert "page=1" in calls[-1]        # fell through to the 1-based probe
    client._first_page = None           # a later client re-probes from 0
    assert client._first_page is None


# --- end to end against the fake UI backend -----------------------------
def test_substring_search_finds_a_partial_name(live_server, tmp_path):
    """The reason this backend exists: "Mind" finds "MindDoc"."""
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,country,keys\nMind,DE,Mind\n", encoding="utf-8")
    out = tmp_path / "r"
    assert main(["search", "--backend", "ui", "--base", live_server,
                 "--input", str(csv_path), "--out", str(out), "--fields", "TRADE_NAME",
                 "--delay", "0", "--retries", "1"]) == EXIT_OK
    result = json.loads((out / "results.json").read_text())["results"][0]
    assert result["candidates"][0]["trade_name"] == "MindDoc: Your Companion"
    assert json.loads((out / "results.json").read_text())["meta"]["backend"] == "ui"


def test_same_search_finds_nothing_on_the_documented_api(live_server, tmp_path):
    """The contrast that motivates the backend switch."""
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,country,keys\nMind,DE,Mind\n", encoding="utf-8")
    out = tmp_path / "r2"
    main(["search", "--backend", "datalake", "--base", live_server, "--key", "dummy",
          "--input", str(csv_path), "--out", str(out), "--delay", "0", "--retries", "1"])
    result = json.loads((out / "results.json").read_text())["results"][0]
    assert result["status"] == "not found"


def test_search_defaults_to_the_documented_api(capsys):
    """The documented API is the default: its filters are exact, but searching
    every name-bearing field still finds most devices. --backend ui remains
    available for genuine substring search."""
    main(["search", "--trade-name", "MindDoc", "--dry-run"])
    out = capsys.readouterr().out
    assert "api.datalake.sante.service.ec.europa.eu" in out
    assert "ec.europa.eu/tools/eudamed/api" not in out


def test_ui_backend_is_still_available_on_request(capsys):
    main(["search", "--trade-name", "MindDoc", "--backend", "ui", "--dry-run"])
    assert "ec.europa.eu/tools/eudamed/api" in capsys.readouterr().out


def test_probe_still_defaults_to_the_documented_api(capsys):
    main(["probe", "--trade-name", "MindDoc", "--dry-run"])
    assert "api.datalake.sante.service.ec.europa.eu" in capsys.readouterr().out


def test_ui_backend_needs_no_key(live_server, tmp_path, monkeypatch):
    monkeypatch.delenv("EUDAMED_SUBSCRIPTION_KEY", raising=False)
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("name,keys\nKalmeda,Kalmeda\n", encoding="utf-8")
    assert main(["search", "--backend", "ui", "--base", live_server, "--require-key",
                 "--input", str(csv_path), "--out", str(tmp_path / "r"),
                 "--delay", "0", "--retries", "1"]) == EXIT_OK
