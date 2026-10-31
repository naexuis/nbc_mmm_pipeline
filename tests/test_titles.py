"""Unit tests for the title seed builders (no Google Cloud needed)."""

from us_hment_mmm.reference import titles as t

TITLE_MAP = {
    "1-800 FLOWERS": {"imdb_id": "tt00000000", "release_date": "1900-01-01", "pew_date": "XXXX",
                      "est_date": "XXXX", "distributor": "UNKNOWN", "product_id": "XXXX"},
    "WICKED": {"imdb_id": "tt1262426", "release_date": "2024-11-22", "pew_date": "2024-12-31",
               "est_date": "2025-02-18", "distributor": "UNIVERSAL", "product_id": "111"},
    "UNTITLED SPIELBERG": {"imdb_id": "tt999", "release_date": "XXXX", "pew_date": "XXXX",
                           "est_date": "XXXX", "distributor": "UNKNOWN", "product_id": "XXXX"},
    "DISCLOSURE DAY": {"imdb_id": "tt999", "release_date": "2026-06-12", "pew_date": "XXXX",
                       "est_date": "XXXX", "distributor": "UNIVERSAL", "product_id": "222"},
}
DDS = {"WKD": "WICKED", "WIK": "WICKD", "FLW": "1-800 FLOWERS", "SWK": "THE SECRETS WE KEEP"}
EXCEPTIONS = {"WICKD": "WICKED"}


def test_clean_turns_placeholders_into_none():
    assert t.clean("XXXX") is None
    assert t.clean("1900-01-01") is None
    assert t.clean("UNKNOWN") is None
    assert t.clean("2024-01-01") == "2024-01-01"


def test_build_titles_skips_placeholder_and_merges_shared_imdb():
    titles, notes = t.build_titles(TITLE_MAP, {})
    by = {r["imdb_id"]: r for r in titles}
    assert set(by) == {"tt1262426", "tt999"}
    assert by["tt999"]["title_name"] == "DISCLOSURE DAY"          # UNTITLED loses
    assert "UNTITLED SPIELBERG" in by["tt999"]["review_note"]
    assert by["tt1262426"]["pvod_date"] == "2024-12-31"
    assert by["tt999"]["pvod_date"] is None
    assert len(notes) == 1


def test_build_titles_applies_overrides():
    overrides = {
        "pvod_date": {"tt999": {"value": "2026-06-30", "reason": "official"}},
        "in_mmm": {"tt1262426": {"value": False, "reason": "excluded"},
                   "tt0": {"value": True, "reason": "unknown film"}},
    }
    titles, notes = t.build_titles(TITLE_MAP, overrides)
    by = {r["imdb_id"]: r for r in titles}
    assert by["tt999"]["pvod_date"] == "2026-06-30"
    assert by["tt1262426"]["in_mmm"] is False
    assert by["tt1262426"]["in_mmm_reason"] == "excluded"
    assert any("tt0" in n for n in notes)


def test_resolve_code_follows_exception_chain():
    assert t.resolve_code("WIK", DDS, EXCEPTIONS, TITLE_MAP)["imdb_id"] == "tt1262426"
    assert t.resolve_code("FLW", DDS, EXCEPTIONS, TITLE_MAP)["status"] == "non_film"
    assert t.resolve_code("SWK", DDS, EXCEPTIONS, TITLE_MAP)["status"] == "unmapped"
    assert t.resolve_code("ZZZ", DDS, EXCEPTIONS, TITLE_MAP)["status"] == "unmapped"


def test_build_code_aliases_keeps_group_and_reports_unmapped():
    observed = [("UP_HE", "WKD", "WICKED"), ("FOCUS", "FLW", "FLOWERS"),
                ("UP_HE", "SWK", "SECRETS"), ("UP_HE", "WKD", "WICKED")]
    aliases, unmapped = t.build_code_aliases(observed, DDS, EXCEPTIONS, TITLE_MAP)
    assert [(a["business_group"], a["alias_value"], a["category"]) for a in aliases] == [
        ("FOCUS", "FLW", "non_film"), ("UP_HE", "WKD", "film")]
    assert [u["product_code"] for u in unmapped] == ["SWK"]


def test_build_product_id_aliases():
    rows = t.build_product_id_aliases(TITLE_MAP, {"tt1262426", "tt999"})
    assert {(r["alias_value"], r["imdb_id"]) for r in rows} == {("111", "tt1262426"),
                                                                ("222", "tt999")}
    assert all(r["business_group"] is None for r in rows)


def test_history_rows_one_per_filled_value():
    rows = [{"imdb_id": "tt1", "title_name": "A", "pvod_date": None}]
    hist = t.history_rows("title", rows, ["imdb_id"], ["title_name", "pvod_date"],
                          lambda r, f: "seed", "me", None)
    assert len(hist) == 1 and hist[0]["record_key"] == "tt1" and hist[0]["old_value"] is None


def test_real_seed_files_resolve():
    import json
    load = lambda f: json.loads((t.SEED_DIR / f).read_text())  # noqa: E731
    title_map = load("title_mapping_data_dict.json")
    titles, _ = t.build_titles(title_map, load("seed_overrides.json"))
    assert len(titles) > 300
    assert sum(r["in_mmm"] is True for r in titles) == 54
    jw = next(r for r in titles if r["imdb_id"] == "tt31036941")
    assert jw["title_name"] == "JURASSIC WORLD REBIRTH"
