"""Shape tolerance: the spec declares no response schemas, so field names
are unknown until a live call. These tests pin the tolerance itself."""

import pytest

from eudamed.fields import describe_keys, index_row, pick, squash_key, unwrap_rows
from eudamed.records import Actor, Device


@pytest.mark.parametrize("a,b", [
    ("TRADE_NAME", "tradeName"), ("TRADE_NAME", "trade_name"),
    ("TRADE_NAME", "Trade Name"), ("MF_SRN", "mfSrn"), ("PRIMARY_DI", "primary-di"),
])
def test_key_spellings_are_equivalent(a, b):
    assert squash_key(a) == squash_key(b)


@pytest.mark.parametrize("raw", [
    {"TRADE_NAME": "MindDoc"},          # spec spelling
    {"tradeName": "MindDoc"},           # camelCase
    {"trade_name": "MindDoc"},          # snake_case
    {"Trade Name": "MindDoc"},          # spaced
])
def test_device_reads_any_spelling(raw):
    assert Device(raw).trade_name == "MindDoc"


def test_populated_value_wins_over_blank_duplicate():
    i = index_row({"TRADE_NAME": "", "tradeName": "MindDoc"})
    assert pick(i, "TRADE_NAME") == "MindDoc"


def test_pick_default():
    assert pick(index_row({}), "NOPE", default="-") == "-"


@pytest.mark.parametrize("payload,expected", [
    ([{"a": 1}], 1),
    ({"content": [{"a": 1}, {"b": 2}]}, 2),
    ({"items": [{"a": 1}]}, 1),
    ({"data": [{"a": 1}]}, 1),
    ({"results": [{"a": 1}]}, 1),
    ({"value": [{"a": 1}]}, 1),
    ({"TRADE_NAME": "x"}, 1),           # a single bare record
    ([], 0), (None, 0), ({}, 0), ("nonsense", 0), (42, 0),
])
def test_unwrap_handles_unknown_shapes(payload, expected):
    assert len(unwrap_rows(payload)) == expected


def test_unwrap_skips_non_dict_entries():
    assert unwrap_rows([{"a": 1}, "junk", None, 5]) == [{"a": 1}]


def test_describe_keys_reports_original_spellings():
    assert describe_keys([{"TRADE_NAME": 1}, {"MF_SRN": 2}, {"TRADE_NAME": 3}]) \
        == ["MF_SRN", "TRADE_NAME"]


def test_device_country_from_srn():
    assert Device({"MF_SRN": "DE-MF-000012345"}).country == "DE"
    assert Device({"MF_SRN": "cz-mf-1"}).country == "CZ"
    assert Device({"MF_SRN": ""}).country == ""


def test_device_link_prefers_uuid():
    assert "search-device/abc" in Device({"UUID": "abc", "PRIMARY_DI": "1"}).link


def test_device_link_falls_back_to_udi_search_without_uuid():
    """The datalake API may not return a UUID; the device screen needs one, so
    a UDI-DI search link is used instead of emitting a broken URL."""
    link = Device({"PRIMARY_DI": "04260703120019"}).link
    assert "deviceIdentifier=04260703120019" in link


def test_device_link_empty_when_nothing_identifies_it():
    assert Device({"TRADE_NAME": "x"}).link == ""


def test_identity_prefers_uuid_then_di_then_basic_udi():
    assert Device({"UUID": "u", "PRIMARY_DI": "d"}).identity() == "u"
    assert Device({"PRIMARY_DI": "d", "BASIC_UDI": "b"}).identity() == "d"
    assert Device({"BASIC_UDI": "b"}).identity() == "b"
    assert Device({"TRADE_NAME": "t", "MF_SRN": "s"}).identity() == "t|s|"


def test_device_to_dict_is_flat_and_serialisable():
    import json
    out = Device({"TRADE_NAME": "MindDoc", "RISK_CLASS_ID": 2}).to_dict()
    assert json.loads(json.dumps(out))["trade_name"] == "MindDoc"
    assert all(not isinstance(v, (dict, list)) for v in out.values())


def test_device_handles_empty_and_junk_rows():
    for raw in ({}, None):
        d = Device(raw)
        assert d.trade_name == "" and d.link == "" and d.country == ""


def test_actor_mapping():
    a = Actor({"ACTOR_ID": "DE-MF-1", "NAME": "MindDoc Health GmbH",
               "ACT_COUNTRY_ISO2_CODE": "de", "ACTOR_TYPE": "MANUFACTURER"})
    assert a.actor_id == "DE-MF-1" and a.country == "DE"
    assert a.to_dict()["name"] == "MindDoc Health GmbH"
