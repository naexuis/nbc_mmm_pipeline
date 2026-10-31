"""Unit tests for the reference CSV loader's pure helpers."""

from us_hment_mmm import config
from us_hment_mmm.reference.load_csv import RefFile, check, clean_column, dated_files, parse_header, pick

ENR = config.REFERENCE_SOURCES["enrichment_title"]
CHTT = config.REFERENCE_SOURCES["std_chtt"]


def test_dated_files_matches_pattern_and_sorts():
    names = [("20261030_enrichments_title.csv", 300, 1), ("20260830_enrichments_title.csv", 100, 1),
             ("notes.txt", 5, 1), ("20261019_enrichments_title.csv", 300, 1)]
    files = dated_files(names, ENR["pattern"])
    assert [f.date for f in files] == ["20260830", "20261019", "20261030"]
    chtt = dated_files([("he_ent_std_chtt_20261030.csv", 238, 1)], CHTT["pattern"])
    assert chtt[0].date == "20261030"


def test_pick_latest_and_specific():
    files = [RefFile("a", "20260830", 1, 1), RefFile("b", "20261030", 1, 1)]
    chosen, prev = pick(files)
    assert chosen.name == "b" and prev.name == "a"
    assert pick(files, "20260830")[1] is None
    assert pick(files, "20990101") == (None, None)
    assert pick([]) == (None, None)


def test_header_and_columns():
    assert parse_header("﻿imdb_id,Title Name\nx,y\n".encode()) == ["imdb_id", "Title Name"]
    assert clean_column("Title Name") == "title_name"


def test_check_required_and_size():
    header = list(CHTT["required"])
    cur, prev = RefFile("c", "20261030", 100, 1), RefFile("p", "20261001", 100, 1)
    cols, problems, warnings = check(header, CHTT["required"], cur, prev)
    assert cols == list(CHTT["required"]) and not problems and not warnings
    _, problems, _ = check(header[:-1], CHTT["required"], cur, prev)
    assert any("missing" in p for p in problems)
    small = RefFile("c", "20261030", 50, 1)
    _, problems, _ = check(header, CHTT["required"], small, prev)
    assert any("smaller" in p for p in problems)
    _, problems, warnings = check(header, CHTT["required"], small, prev, allow_smaller=True)
    assert not problems and warnings
