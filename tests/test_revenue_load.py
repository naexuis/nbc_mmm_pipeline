"""Unit tests for the revenue loader's pure helpers (no Google Cloud needed)."""

import pytest

from us_hment_mmm.revenue.load_raw import (
    Delivery,
    SourceFile,
    check_delivery,
    group_deliveries,
    parse_header,
    pick_delivery,
    sanitize_column,
)

PREFIX = "hment_rev_usa/"
REQUIRED = ("product_id1", "royalty_total_usd")


def _files(folder, n, size=100, stem="usa"):
    return [SourceFile(f"{PREFIX}{folder}/he_{stem}_{folder}-{i:012d}.csv", size, 1)
            for i in range(n)]


def test_group_deliveries_by_folder_and_ignores_other_paths():
    blobs = _files("20260601", 2, stem="uas") + _files("20261008", 3) + [
        SourceFile(f"{PREFIX}20261008/readme.txt", 5, 1),
        SourceFile(f"{PREFIX}notes.txt", 5, 1),
        SourceFile(f"{PREFIX}scratch/x.csv", 5, 1),
    ]
    d = group_deliveries(PREFIX, blobs)
    assert sorted(d) == ["20260601", "20261008"]
    assert len(d["20260601"].files) == 2          # misspelled 'uas' names still counted
    assert len(d["20261008"].files) == 3
    assert d["20261008"].other_files == [f"{PREFIX}20261008/readme.txt"]


def test_pick_latest_and_previous():
    d = group_deliveries(PREFIX, _files("20260501", 1) + _files("20261008", 1))
    chosen, prev = pick_delivery(d, "latest")
    assert chosen.folder == "20261008" and prev.folder == "20260501"
    chosen, prev = pick_delivery(d, "20260501")
    assert prev is None


def test_pick_unknown_delivery_raises():
    d = group_deliveries(PREFIX, _files("20261008", 1))
    with pytest.raises(ValueError):
        pick_delivery(d, "20990101")


def test_parse_header_handles_bom_and_quotes():
    raw = '﻿product_id1,"imdb title id",royalty_total_usd\n1,tt1,2.5\n'.encode("utf-8")
    assert parse_header(raw) == ["product_id1", "imdb title id", "royalty_total_usd"]


def test_sanitize_column():
    assert sanitize_column("Royalty Total (USD)") == "royalty_total__usd_"
    assert sanitize_column("2nd_col") == "c_2nd_col"


def _check(cur_files, prev_files=None, header=("product_id1", "royalty_total_usd"), **kw):
    cur = Delivery("20261008", cur_files)
    prev = Delivery("20260713", prev_files) if prev_files else None
    headers = {f.name: list(header) for f in cur_files}
    return check_delivery(cur, prev, headers, REQUIRED, **kw)


def test_clean_delivery_passes():
    cols, problems, warnings = _check(_files("20261008", 3, 100), _files("20260713", 3, 90))
    assert cols == ["product_id1", "royalty_total_usd"]
    assert problems == [] and warnings == []


def test_smaller_than_previous_blocks_unless_allowed():
    _, problems, _ = _check(_files("20261008", 2, 100), _files("20260713", 3, 100))
    assert any("smaller" in p for p in problems)
    _, problems, warnings = _check(_files("20261008", 2, 100), _files("20260713", 3, 100),
                                   allow_smaller=True)
    assert problems == [] and any("smaller" in w for w in warnings)


def test_missing_required_column_blocks():
    _, problems, _ = _check(_files("20261008", 1), header=("product_id1",))
    assert any("Required columns missing" in p for p in problems)


def test_mismatched_headers_block():
    files = _files("20261008", 2)
    cur = Delivery("20261008", files)
    headers = {files[0].name: ["product_id1", "royalty_total_usd"],
               files[1].name: ["royalty_total_usd", "product_id1"]}
    _, problems, _ = check_delivery(cur, None, headers, REQUIRED)
    assert any("header" in p for p in problems)


def test_fingerprint_changes_when_a_file_changes():
    a = Delivery("20261008", _files("20261008", 2))
    b = Delivery("20261008", _files("20261008", 2))
    assert a.fingerprint() == b.fingerprint()
    b.files[0] = SourceFile(b.files[0].name, b.files[0].size, 2)   # new GCS generation
    assert a.fingerprint() != b.fingerprint()


def test_canada_has_its_own_folder_and_table():
    from us_hment_mmm import config
    from us_hment_mmm.revenue import load_raw

    can = load_raw._spec("can")
    assert can["prefix"] == "hment_rev_can/" and can["table"] == "revenue_pos_can"
    assert can["source"] != config.REVENUE_SPEC["source"]
    assert load_raw._spec("USA") is config.REVENUE_SPEC
