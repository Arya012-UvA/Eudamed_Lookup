"""Device-type classification: software, other, or undetermined.

The point of these is the three-way distinction. A two-way classifier would
have to guess about a row whose EMDN code and special device type are both
blank, and either guess drops real devices or keeps the noise the filter
exists to remove.
"""

import pytest

from eudamed import devicetype
from eudamed.devicetype import OTHER, SOFTWARE, UNKNOWN, Typer, classify, classify_values
from eudamed.records import Device


def dev(**raw):
    return Device(raw)


# --- classification from the row itself --------------------------------------
@pytest.mark.parametrize("code", ["Z12010203", "Z1201", "Z12", "z12 01 02", "Z12 010299"])
def test_emdn_software_category_in_any_spelling(code):
    kind, reason = classify(dev(NOMENCLATURE_CODE=code))
    assert kind == SOFTWARE
    assert "Z12" in reason


@pytest.mark.parametrize("code", ["Y120304", "A0101", "J0199"])
def test_emdn_outside_the_software_category_is_other(code):
    kind, reason = classify(dev(NOMENCLATURE_CODE=code))
    assert kind == OTHER
    assert "outside" in reason


def test_special_device_type_naming_software_settles_it():
    """The explicit flag wins, and does not need an EMDN code at all."""
    kind, reason = classify(dev(SPECIAL_DEVICE_TYPE="Software"))
    assert kind == SOFTWARE
    assert "special device type" in reason


def test_special_device_type_beats_a_conflicting_emdn_code():
    kind, _ = classify(dev(SPECIAL_DEVICE_TYPE="Software", NOMENCLATURE_CODE="Y120304"))
    assert kind == SOFTWARE


def test_empty_record_is_undetermined_not_other():
    """The distinction this module exists for.

    A blank field is not a statement that the device is hardware, so it must
    not be reported as one.
    """
    kind, reason = classify(dev(TRADE_NAME="Mystery"))
    assert kind == UNKNOWN
    assert "no EMDN code" in reason


def test_special_type_none_is_not_evidence_of_hardware():
    """"None" is the normal special-device-type value for software.

    Reading it as a denial would classify every DiGA-style app as hardware.
    """
    assert classify(dev(SPECIAL_DEVICE_TYPE="None"))[0] == UNKNOWN
    assert classify(dev(SPECIAL_DEVICE_TYPE="None", NOMENCLATURE_CODE="Z1201"))[0] == SOFTWARE


def test_physical_special_type_without_an_emdn_code_is_other():
    assert classify(dev(SPECIAL_DEVICE_TYPE="Procedure pack"))[0] == OTHER


def test_ui_backend_nested_code_is_flattened_before_classifying():
    """The web-UI backend nests the special type as {"code": ...}."""
    kind, _ = classify(dev(specialDeviceType={"code": "SPECIAL_DEVICE_TYPE.SOFTWARE"}))
    assert kind == SOFTWARE


def test_kind_is_on_every_device_dict():
    entry = dev(NOMENCLATURE_CODE="Z1203").to_dict()
    assert entry["device_kind"] == SOFTWARE
    assert "Z1203" in entry["device_kind_reason"]


# --- the detail record ------------------------------------------------------
def test_detail_values_reads_the_cnd_nomenclature_list():
    detail = {"cndNomenclatures": [
        {"code": "Z12010203", "description": {"texts": [{"text": "software"}]}}]}
    assert devicetype.detail_values(detail) == ("", "Z12010203")


def test_detail_values_tolerates_an_unexpected_shape():
    """An unrecognised detail record leaves the device undetermined."""
    assert devicetype.detail_values({"something": "else"}) == ("", "")
    assert devicetype.detail_values(None) == ("", "")
    assert devicetype.detail_values([1, 2]) == ("", "")


class StubDetail:
    """A detail client, counting calls so caching can be asserted."""

    def __init__(self, detail, fail=False):
        self.detail = detail
        self.fail = fail
        self.calls = []

    def device_detail(self, uuid):
        self.calls.append(uuid)
        if self.fail:
            from eudamed.client import ApiError
            raise ApiError("boom")
        return self.detail


def test_typer_resolves_an_undetermined_row_from_the_detail_record():
    """Why this exists: the web-UI backend's list rows carry no EMDN code.

    Without the detail lookup every row on that backend is undetermined, so
    --software-only would drop the whole register.
    """
    stub = StubDetail({"cndNomenclatures": [{"code": "Z12010203"}]})
    typer = Typer(detail_client=stub)
    kind, reason = typer.classify(dev(UUID="u1", TRADE_NAME="MindDoc"))
    assert kind == SOFTWARE
    assert "detail record" in reason
    assert stub.calls == ["u1"]


def test_typer_does_not_fetch_when_the_row_already_answers():
    stub = StubDetail({"cndNomenclatures": [{"code": "Z12010203"}]})
    typer = Typer(detail_client=stub)
    assert typer.classify(dev(UUID="u1", NOMENCLATURE_CODE="Y1"))[0] == OTHER
    assert stub.calls == []


def test_typer_caches_per_uuid():
    stub = StubDetail({"cndNomenclatures": [{"code": "Z12"}]})
    typer = Typer(detail_client=stub)
    for _ in range(3):
        typer.classify(dev(UUID="u1"))
    assert stub.calls == ["u1"]
    assert typer.detail_requests == 1


def test_typer_leaves_the_device_undetermined_when_the_detail_fetch_fails():
    """A failed lookup must not be reported as "not software"."""
    stub = StubDetail(None, fail=True)
    typer = Typer(detail_client=stub)
    kind, reason = typer.classify(dev(UUID="u1"))
    assert kind == UNKNOWN
    assert "could not be read" in reason
    assert typer.detail_errors == 1


def test_typer_without_a_detail_client_is_plain_classify():
    typer = Typer(detail_client=None)
    assert typer.classify(dev(UUID="u1")) == classify(dev(UUID="u1"))
    assert typer.detail_requests == 0


def test_typer_needs_a_uuid_to_ask_for_a_detail_record():
    stub = StubDetail({"cndNomenclatures": [{"code": "Z12"}]})
    typer = Typer(detail_client=stub)
    assert typer.classify(dev(TRADE_NAME="no uuid"))[0] == UNKNOWN
    assert stub.calls == []


def test_classify_values_is_the_shared_decision():
    assert classify_values("", "Z12")[0] == SOFTWARE
    assert classify_values("Software", "")[0] == SOFTWARE
    assert classify_values("", "")[0] == UNKNOWN
