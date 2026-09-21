"""Spec conformance and transport behaviour of the HTTP client."""

import urllib.parse

import pytest

from eudamed import config
from eudamed.client import ApiError, AuthError, Client
from conftest import error_opener, json_opener


def params_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# --- spec conformance -------------------------------------------------
def test_required_format_and_api_version_always_sent(client_factory):
    q = params_of(client_factory().build_url("/udi", {"TRADE_NAME": "MindDoc"}))
    assert q["format"] == "json"                       # required: true in the spec
    assert q["api-version"] == config.API_VERSION      # required by the gateway
    assert q["TRADE_NAME"] == "MindDoc"


def test_csv_format_is_supported(client_factory):
    assert params_of(client_factory(fmt="csv").build_url("/udi", {}))["format"] == "csv"


def test_api_version_can_be_omitted(client_factory):
    assert "api-version" not in params_of(
        client_factory(api_version="").build_url("/udi", {}))


def test_content_type_header_is_sent(client_factory):
    """Content-Type is required: true on every operation in the spec."""
    assert client_factory()._headers()["Content-Type"] == "application/json"


def test_key_in_header_mode(client_factory):
    c = client_factory(key="abc", auth_mode="header")
    assert c._headers()[config.KEY_HEADER] == "abc"
    assert config.KEY_QUERY not in params_of(c.build_url("/udi", {}))


def test_key_in_query_mode(client_factory):
    c = client_factory(key="abc", auth_mode="query")
    assert params_of(c.build_url("/udi", {}))[config.KEY_QUERY] == "abc"
    assert config.KEY_HEADER not in c._headers()


def test_no_key_sends_no_credential(client_factory):
    c = client_factory(key="")
    assert config.KEY_HEADER not in c._headers()
    assert config.KEY_QUERY not in params_of(c.build_url("/udi", {}))


@pytest.mark.parametrize("path,param", [
    ("/udi", "TRADE_NAME"), ("/udi", "MF_SRN"), ("/udi", "MEDICAL_PURPOSE"),
    ("/actors", "NAME"), ("/actors", "ACT_COUNTRY_ISO2_CODE"), ("/reference", "LANGUAGE"),
])
def test_documented_params_accepted(client_factory, path, param):
    assert param in client_factory().build_url(path, {param: "x"})


@pytest.mark.parametrize("path,param", [
    ("/udi", "tradeName"),      # the old UI API's spelling
    ("/udi", "page"),           # pagination: not in the spec
    ("/udi", "pageSize"),
    ("/udi", "NAME"),           # an /actors param, not a /udi one
    ("/actors", "TRADE_NAME"),
])
def test_undocumented_params_rejected(client_factory, path, param):
    with pytest.raises(ValueError, match="not a documented parameter"):
        client_factory().build_url(path, {param: "x"})


def test_empty_params_are_dropped(client_factory):
    q = params_of(client_factory().build_url("/udi", {"TRADE_NAME": "", "MF_SRN": None}))
    assert "TRADE_NAME" not in q and "MF_SRN" not in q


def test_invalid_constructor_args():
    with pytest.raises(ValueError):
        Client(fmt="xml")
    with pytest.raises(ValueError):
        Client(auth_mode="basic")


# --- transport --------------------------------------------------------
def test_always_makes_at_least_one_attempt(client_factory):
    """retries=0 must not mean 'never send the request'."""
    opener = error_opener(500)
    with pytest.raises(ApiError):
        client_factory(opener=opener, retries=0).udi(TRADE_NAME="x")
    assert opener.state["n"] == 1


def test_retries_then_succeeds(client_factory):
    opener = error_opener(503, times=2)
    rows, _ = client_factory(opener=opener, retries=4).udi(TRADE_NAME="x")
    assert rows == [] and opener.state["n"] == 3


def test_exhausted_retries_reports_the_real_cause(client_factory):
    opener = error_opener(503)
    with pytest.raises(ApiError) as exc:
        client_factory(opener=opener, retries=3).udi(TRADE_NAME="x")
    assert opener.state["n"] == 3
    assert "3 attempt" in str(exc.value)
    assert exc.value.cause is not None          # cause preserved, not discarded
    assert "HTTPError" in str(exc.value)


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_raise_immediately_and_explain(client_factory, code):
    opener = error_opener(code)
    with pytest.raises(AuthError) as exc:
        client_factory(opener=opener, retries=4).udi(TRADE_NAME="x")
    assert opener.state["n"] == 1                # no pointless retries
    assert config.KEY_ENV in str(exc.value)
    assert exc.value.status == code


@pytest.mark.parametrize("code", [400, 404])
def test_client_errors_are_not_retried(client_factory, code):
    opener = error_opener(code)
    with pytest.raises(ApiError) as exc:
        client_factory(opener=opener, retries=4).udi(TRADE_NAME="x")
    assert opener.state["n"] == 1
    assert not isinstance(exc.value, AuthError)


def test_error_message_redacts_the_key(client_factory):
    opener = error_opener(500)
    with pytest.raises(ApiError) as exc:
        client_factory(opener=opener, key="SUPERSECRET", auth_mode="query",
                       retries=1).udi(TRADE_NAME="x")
    assert "SUPERSECRET" not in str(exc.value)
    assert "<key>" in str(exc.value)


def test_non_json_body_reports_a_useful_error(client_factory):
    def opener(req, timeout=None):
        from conftest import FakeResp
        return FakeResp(b"<html>gateway error</html>")
    with pytest.raises(ApiError, match="not JSON"):
        client_factory(opener=opener).udi(TRADE_NAME="x")


def test_empty_body_is_no_rows(client_factory):
    def opener(req, timeout=None):
        from conftest import FakeResp
        return FakeResp(b"   ")
    rows, _ = client_factory(opener=opener).udi(TRADE_NAME="x")
    assert rows == []


def test_request_count_tracked(client_factory):
    c = client_factory(opener=json_opener([{"TRADE_NAME": "x"}]))
    c.udi(TRADE_NAME="a")
    c.udi(TRADE_NAME="b")
    assert c.request_count == 2


def test_csv_parsing(client_factory):
    def opener(req, timeout=None):
        from conftest import FakeResp
        return FakeResp(b"TRADE_NAME,MF_SRN\nMindDoc,DE-MF-1\n\n")
    rows, _ = client_factory(opener=opener, fmt="csv").udi(TRADE_NAME="MindDoc")
    assert rows == [{"TRADE_NAME": "MindDoc", "MF_SRN": "DE-MF-1"}]
