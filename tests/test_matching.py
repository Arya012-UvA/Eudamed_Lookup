"""Scoring, including the manufacturer false-positive guard."""

import pytest

from eudamed.matching import FOUND, MANUFACTURER_CAP, POSSIBLE, classify, norm, score_device, squash
from eudamed.records import Device


def dev(**kw):
    return Device({"TRADE_NAME": kw.get("trade", ""), "DEVICE_NAME": kw.get("device", ""),
                   "MF_NAME": kw.get("mfr", ""), "MF_SRN": kw.get("srn", "")})


@pytest.mark.parametrize("raw,expected", [
    ("Oviva Direkt für Adipositas", "oviva direkt fur adipositas"),
    ("i.s.h.med", "i s h med"),
    ("PINK! Coach", "pink coach"),
    ("  MindDoc  ", "minddoc"),
    (None, ""),
    ("", ""),
])
def test_norm(raw, expected):
    assert norm(raw) == expected


def test_squash():
    assert squash("i.s.h. med") == "ishmed"
    assert squash("Mind Doc") == squash("MindDoc") == "minddoc"


@pytest.mark.parametrize("score,expected", [
    (1.0, "found"), (FOUND, "found"), (FOUND - 0.001, "possible"),
    (POSSIBLE, "possible"), (POSSIBLE - 0.001, "not found"), (0.0, "not found"),
])
def test_classify_boundaries(score, expected):
    assert classify(score) == expected


def test_exact_trade_name():
    score, how = score_device(["MindDoc"], "DE", dev(trade="MindDoc", srn="DE-MF-1"))
    assert score == 1.0 and how == "trade_name:exact"


def test_trade_name_contains_key():
    score, how = score_device(["MindDoc"], "", dev(trade="MindDoc - Your Companion"))
    assert score == 0.92 and how == "trade_name:contains"


def test_key_contains_trade_name():
    score, how = score_device(["PINK! Coach"], "", dev(trade="PINK"))
    assert score == 0.80 and how == "trade_name:contained_by"


def test_country_bonus_and_penalty():
    match = dev(trade="deprexis", srn="DE-MF-1")
    conflict = dev(trade="deprexis", srn="US-MF-1")
    assert score_device(["deprexis"], "DE", match)[0] == 1.0      # clamped
    assert score_device(["deprexis"], "NL", conflict)[0] == 0.9
    assert score_device(["deprexis"], "", conflict)[0] == 1.0     # no expectation, no adjustment


def test_device_name_is_weaker_than_trade_name():
    trade = score_device(["MindDoc"], "", dev(trade="MindDoc"))[0]
    device = score_device(["MindDoc"], "", dev(device="MindDoc"))[0]
    assert device < trade
    assert score_device(["MindDoc"], "", dev(device="MindDoc"))[1] == "device_name:exact"


# --- the regression this module exists for -----------------------------
def test_manufacturer_only_match_is_capped_below_possible():
    """A device whose MANUFACTURER is named after the product must never be
    reported as a match. Searching 'MindDoc' previously scored the unrelated
    device 'Moodpath' at 0.90 ('found') because the manufacturer is
    'MindDoc Health GmbH'."""
    score, how = score_device(
        ["MindDoc"], "DE", dev(trade="Moodpath", mfr="MindDoc Health GmbH", srn="DE-MF-1"))
    assert how == "manufacturer"
    assert score <= MANUFACTURER_CAP
    assert score < POSSIBLE
    assert classify(score) == "not found"


def test_country_bonus_cannot_lift_manufacturer_match_over_the_cap():
    score, how = score_device(
        ["Kranus"], "DE", dev(trade="Something Else", mfr="Kranus Health GmbH", srn="DE-MF-1"))
    assert how == "manufacturer" and score <= MANUFACTURER_CAP


def test_trade_name_wins_over_manufacturer_for_same_device():
    score, how = score_device(
        ["MindDoc"], "DE", dev(trade="MindDoc", mfr="MindDoc Health GmbH", srn="DE-MF-1"))
    assert how == "trade_name:exact" and score == 1.0


def test_fuzzy_alone_never_reaches_found():
    for trade in ("Moodpath", "Mindful", "MyndDok", "totally different"):
        score, how = score_device(["MindDoc"], "DE", dev(trade=trade, srn="DE-MF-1"))
        assert score < FOUND, (trade, score, how)


def test_unrelated_scores_not_found():
    score, _ = score_device(["MindDoc"], "DE", dev(trade="Kalmeda", mfr="mynoise", srn="DE-MF-1"))
    assert classify(score) == "not found"


def test_empty_keys_and_blank_rows():
    assert score_device([], "DE", dev(trade="MindDoc")) == (0.0, "none")
    assert score_device([""], "DE", dev(trade="MindDoc")) == (0.0, "none")
    assert score_device(["MindDoc"], "DE", dev()) == (0.0, "none")


def test_score_always_within_bounds():
    for keys in (["MindDoc"], ["x"], ["a b c"]):
        for country in ("DE", "NL", ""):
            device = dev(trade="MindDoc", mfr="MindDoc", srn="DE-MF-1")
            score, _ = score_device(keys, country, device)
            assert 0.0 <= score <= 1.0
