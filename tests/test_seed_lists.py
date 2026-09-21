"""The shipped CSV seed lists must stay loadable.

These files are data, not code, so nothing else would catch a stray comma or a
row that lost its search keys - and a row with no keys searches for nothing
while still looking like it was checked.
"""

import csv
import os

import pytest

from eudamed.search import load_targets

# Resolved against the repository, not the working directory, so the suite
# passes whatever directory pytest was started from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEEDS = ["devices.csv", "devices.example.csv", "psych-devices.csv",
         "psych-discovery.csv", "diga-seed.csv"]


def seed(name):
    return os.path.join(ROOT, name)


@pytest.mark.parametrize("path", SEEDS)
def test_seed_list_loads(path):
    targets = load_targets(seed(path))
    assert targets
    for target in targets:
        assert target.name
        assert target.keys, f"{target.name} in {path} has no search keys"
        assert all(k.strip() for k in target.keys)


@pytest.mark.parametrize("path", SEEDS)
def test_no_duplicate_names(path):
    names = [t.name.lower() for t in load_targets(seed(path))]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"{path} repeats {duplicates}"


def test_diga_seed_covers_psychological_indications():
    """Its reason for being in this repo: DiGA are largely mental-health apps."""
    targets = load_targets(seed("diga-seed.csv"))
    assert len(targets) >= 40
    psych = [t for t in targets
             if t.description.startswith(("mental health", "psycho-oncology"))]
    assert len(psych) >= 20


def test_diga_seed_names_are_ascii_but_keys_may_not_be():
    """`name` labels files and shell arguments; `keys` must hold the real string.

    "Oviva Direkt fuer Adipositas" is the label; the register holds
    "Oviva Direkt fur Adipositas" with an umlaut, so that is what gets sent.
    """
    targets = load_targets(seed("diga-seed.csv"))
    for target in targets:
        target.name.encode("ascii")            # raises if a name is not ASCII
    assert any(not _is_ascii(k) for t in targets for k in t.keys), (
        "no row carries a non-ASCII key, so the umlaut spellings were lost")


def _is_ascii(value):
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def test_diga_seed_has_the_expected_columns():
    with open(seed("diga-seed.csv"), newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == ["name", "description", "ca", "country", "keys", "broad"]


def test_diga_seed_leaves_country_blank():
    """Deliberate: a wrong expected country costs 0.10 of score.

    Not every DiGA manufacturer is German, so guessing DE across the board
    would penalise correct matches. Documented in diga-seed.NOTES.md.
    """
    assert all(t.country == "" for t in load_targets(seed("diga-seed.csv")))
