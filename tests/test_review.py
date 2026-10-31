"""Unit tests for the title review list and change plan (no Google Cloud needed)."""

from us_hment_mmm.reference import review as R

TITLES = {
    "tt12921446": {"imdb_id": "tt12921446", "title_name": "THE EXORCIST BELIEVER",
                   "release_date": "2023-10-06", "pvod_date": "2023-10-24", "est_date": None,
                   "in_mmm": True, "in_mmm_reason": "x", "distributor": "UNIVERSAL",
                   "review_note": "IMDb ID also used for: SHOOTING STARS."},
    "tt1262426": {"imdb_id": "tt1262426", "title_name": "WICKED", "release_date": "2024-11-22",
                  "pvod_date": "2024-12-31", "est_date": "2025-02-18", "in_mmm": None,
                  "in_mmm_reason": None, "distributor": "UNIVERSAL", "review_note": None},
}
ALIASES = {("media_product_code", "UP_HE", "SSS"): {
    "alias_type": "media_product_code", "business_group": "UP_HE", "alias_value": "SSS",
    "category": "film", "imdb_id": "tt12921446", "label": "SHOOTING STARS"}}
ENRICH = {"tt13609648": {"imdb_id": "tt13609648", "title_name": "SENSE AND SENSIBILITY",
                         "distributor": "FOCUS", "release_date": "2026-09-01",
                         "pvod_date": "2026-11-03", "est_date": None}}


def test_parse_date_formats():
    assert R.parse_date("2026-06-30") == "2026-06-30"
    assert R.parse_date("6/30/2026") == "2026-06-30"
    assert R.parse_date("20260630") == "2026-06-30"
    assert R.parse_date("2026-06-30 00:00:00") == "2026-06-30"
    assert R.parse_date("XXXX") is None and R.parse_date("") is None


def test_norm_name():
    assert R.norm_name("Sense & Sensibility") == "SENSE AND SENSIBILITY"
    assert R.norm_name("EXORCIST: BELIEVER, THE") == "EXORCIST BELIEVER"


def test_suggest_for_code():
    assert R.suggest_for_code("SHREK 25TH ANNIVERSA", None, TITLES, ENRICH)[0] == "non_film"
    assert R.suggest_for_code("WICKED PT 2 DRAFTING", None, TITLES, ENRICH)[0] == "non_film"
    assert R.suggest_for_code("SENSE & SENSIBILITY", "tt13609648", TITLES, ENRICH)[1] == \
        "tt13609648"
    assert R.suggest_for_code("SENSE & SENSIB", None, TITLES, ENRICH)[1] == "tt13609648"
    assert R.suggest_for_code("WICKED", None, TITLES, ENRICH)[1] == "tt1262426"
    assert R.suggest_for_code("PLAYMOBIL", None, TITLES, ENRICH)[0] is None


def test_date_differences_merges_sources():
    enrich = {"tt1262426": {"pvod_date": "2024-12-30", "est_date": "2025-02-18"}}
    chtt = {"tt1262426": {"pvod_date": "2024-12-30", "est_date": "2025-02-20"}}
    diffs = R.date_differences(TITLES, enrich, chtt)
    assert [(d["field"], d["new"], d["source"]) for d in diffs] == [
        ("est_date", "2025-02-20", "Finance"), ("pvod_date", "2024-12-30", "NBC + Finance")]


def test_display_name_moves_article():
    assert R.display_name("UPRISING, THE") == "THE UPRISING"
    assert R.display_name("Wicked") == "WICKED"


def test_names_agree_on_real_examples():
    for prisma, film in [("HOTSPOT", "HOT SPOT"), ("THE UPRISING", "UPRISING, THE"),
                         ("GABBY'S DOLLHOUSE", "GABBYS DOLLHOUSE THE MOVIE"),
                         ("GOOD LUCK HAV FUN DD", "GOOD LUCK, HAVE FUN, DON'T DIE"),
                         ("SIX:THE MUSICAL LIVE", "SIX: THE MUSICAL LIVE!"),
                         ("SENSE & SENSIBILITY", "SENSE AND SENSIBILITY")]:
        assert R.names_agree(prisma, film), (prisma, film)
    assert not R.names_agree("SENSE & SENSIBILITY", "LORNE")
    assert not R.names_agree("MEET THE PARENTS 4", "FOCKER IN-LAW")


def test_suggest_flags_wrong_or_mixed_tags():
    enrich = dict(ENRICH, tt39847629={"imdb_id": "tt39847629", "title_name": "LORNE"})
    cat, imdb, why = R.suggest_for_code("SENSE & SENSIBILITY", ["tt39847629"], TITLES, enrich)
    assert cat is None and "LORNE" in why
    cat, imdb, why = R.suggest_for_code("SENSE & SENSIBILITY", ["tt39847629", "tt13609648"],
                                        TITLES, enrich)
    assert cat is None and "different IMDb IDs" in why


def _items(rejected=frozenset()):
    codes = [{"business_group": "FOCUS", "product_code": "SEN",
              "sample_name": "SENSE & SENSIBILITY", "rows": 10, "tags": ["tt13609648"]},
             {"business_group": "UP_HE", "product_code": "PYM", "sample_name": "PLAYMOBIL",
              "rows": 3, "tags": []},
             {"business_group": "UP_HE", "product_code": "S25", "sample_name": "SHREK 25TH ANNIVERSA",
              "rows": 2, "tags": []}]
    dates = [{"imdb_id": "tt1262426", "title_name": "WICKED", "in_mmm": None,
              "field": "pvod_date", "current": "2024-12-31", "new": "2024-12-30", "source": "NBC"}]
    pids = [{"product_id": "999", "imdb_id": "tt1262426", "royalty": 1000.0}]
    return R.build_items(codes, TITLES, ENRICH, dates, pids, rejected)


def _row(item, n, decision, reason="ok", use_instead=None):
    return {"row": n, "tab": "1 · Product codes", "key": f"{item['key']}#{R.item_check(item)}",
            "label": item["what"], "decision": decision, "use_instead": use_instead,
            "reason": reason}


def _by(items):
    return {i["key"]: i for i in items}


def test_build_items_keys_and_sections():
    items = _items()
    assert [i["section"] for i in items] == [1, 1, 1, 2, 3, 4]
    b = _by(items)
    assert b["code|FOCUS|SEN"]["show"]["proposed"] == "Link to SENSE AND SENSIBILITY (tt13609648)"
    assert b["code|UP_HE|PYM"]["has_suggestion"] is False
    assert b["code|UP_HE|S25"]["suggested"] == "non_film"


def test_rejected_suggestions_are_left_out():
    first = _by(_items())
    rejected = {(first["date|tt1262426|pvod_date|NBC"]["key"], "2024-12-30"),
                (first["code|UP_HE|S25"]["key"], "non_film")}
    again = _by(_items(rejected))
    assert "date|tt1262426|pvod_date|NBC" not in again                 # date suggestion gone
    assert again["code|UP_HE|S25"]["has_suggestion"] is False     # code stays, suggestion gone


def test_workbook_decisions_approve_use_instead_reject():
    items = _items()
    b = _by(items)
    data = {"review": [_row(b["code|FOCUS|SEN"], 2, "Approve"),
                       _row(b["code|UP_HE|PYM"], 3, "Use instead", use_instead="non_film"),
                       _row(b["date|tt1262426|pvod_date|NBC"], 6, "Reject"),
                       _row(b["pid|999"], 7, "approve")]}
    p = R.plan_from_workbook(data, items, TITLES, ALIASES, ENRICH)
    assert p.problems == [] and p.skipped == []
    got = {(c["key"], c["field"]): c for c in p.changes}
    assert got[("media_product_code|FOCUS|SEN", "imdb_id")]["new"] == "tt13609648"
    assert "tt13609648" in p.new_titles                       # added from NBC enrichment
    assert got[("media_product_code|UP_HE|PYM", "category")]["new"] == "non_film"
    assert got[("date|tt1262426|pvod_date|NBC", "reject")]["new"] == "2024-12-30"
    assert got[("revenue_product_id||999", "imdb_id")]["where"] == "1 · Product codes row 7"
    assert all(c["reason"] == "ok" for c in p.changes)


def test_workbook_row_problems():
    items = _items()
    b = _by(items)
    data = {"review": [_row(b["code|FOCUS|SEN"], 2, "Approve", reason=""),
                       _row(b["code|UP_HE|PYM"], 3, "Approve"),
                       _row(b["code|UP_HE|S25"], 4, "Use instead"),
                       _row(b["date|tt1262426|pvod_date|NBC"], 6, "Maybe")]}
    p = R.plan_from_workbook(data, items, TITLES, ALIASES, ENRICH)
    assert len(p.problems) == 4
    assert p.problems[0].startswith("1 · Product codes row 2")
    assert p.changes == []


def test_workbook_stale_rows_are_skipped():
    items = _items()
    b = _by(items)
    old = _row(b["date|tt1262426|pvod_date|NBC"], 6, "Approve")
    old["key"] = old["key"].split("#")[0] + "#00000000"       # values changed since
    gone = dict(_row(b["code|FOCUS|SEN"], 2, "Approve"), key="code|FOCUS|XXX#1")
    p = R.plan_from_workbook({"review": [old, gone]}, items, TITLES, ALIASES, ENRICH)
    assert p.changes == [] and p.problems == [] and len(p.skipped) == 2


def test_workbook_shooting_stars_fix_by_hand():
    items = _items()
    note = _by(items)["note|tt12921446"]
    data = {
        "add_films": [{"row": 2, "imdb_id": "tt3715152", "title_name": "Shooting Stars",
                       "distributor": "PEACOCK", "release_date": "2023-06-02", "pvod_date": None,
                       "est_date": "2023-11-07", "reason": "wrong IMDb ID in old dictionary"}],
        "map_codes": [{"row": 2, "business_code": "ZUE", "product_code": "sss",
                       "film": "tt3715152", "reason": "wrong IMDb ID in old dictionary"}],
        "review": [_row(note, 5, "Approve", reason="Shooting Stars split out")],
    }
    p = R.plan_from_workbook(data, items, TITLES, ALIASES, ENRICH)
    assert p.problems == []
    got = {(c["key"], c["field"]): (c["old"], c["new"]) for c in p.changes}
    assert got[("tt3715152", "title_name")] == (None, "SHOOTING STARS")
    assert got[("media_product_code|UP_HE|SSS", "imdb_id")] == ("tt12921446", "tt3715152")
    assert got[("tt12921446", "review_note")][1] is None


def test_workbook_change_films_and_bad_values():
    data = {
        "change_films": [
            {"row": 2, "film": "tt1262426", "field": "in_mmm", "new_value": "yes",
             "reason": "Spend final per Planning"},
            {"row": 3, "film": "tt1262426", "field": "pvod_date", "new_value": "soon",
             "reason": "x"},
            {"row": 4, "film": "tt1262426", "field": "imdb_id", "new_value": "x", "reason": "x"},
            {"row": 5, "film": "tt9999999", "field": "pvod_date", "new_value": "2024-01-01",
             "reason": "x"}],
        "map_codes": [{"row": 2, "business_code": "ZUP", "product_code": "AAA",
                       "film": "tt1262426", "reason": "x"},
                      {"row": 3, "business_code": "ZUE", "product_code": "BBB",
                       "film": "tt0000001", "reason": "x"}],
        "add_films": [{"row": 2, "imdb_id": "tt12921446", "title_name": "X", "reason": "x"}],
    }
    p = R.plan_from_workbook(data, [], TITLES, ALIASES, ENRICH)
    assert len(p.problems) == 6
    got = {c["field"]: c["new"] for c in p.changes}
    assert got == {"in_mmm": True, "in_mmm_reason": "Spend final per Planning"}


def _decide(ws, first, second, decision, reason):
    """Fill Decision and Reason on the row whose first (and second) column match."""
    header = [c.value for c in ws[2]]
    for r in range(3, ws.max_row + 1):
        if ws.cell(r, 1).value == first and (second is None or ws.cell(r, 2).value == second):
            ws.cell(r, header.index("Decision") + 1).value = decision
            ws.cell(r, header.index("Reason") + 1).value = reason
            return
    raise AssertionError(f"row {first} {second} not found")


def test_workbook_round_trip(tmp_path):
    from us_hment_mmm.reference import review_workbook as W

    items = _items()
    path = W.write_workbook(items, TITLES, tmp_path)
    import openpyxl
    wb = openpyxl.load_workbook(path)
    _decide(wb["1 · Product codes"], "Focus", "SEN", "Accept", "tag matches")
    wb["Change films"]["A3"] = "tt1262426"
    wb["Change films"]["B3"] = "pvod_date"
    wb["Change films"]["C3"] = __import__("datetime").datetime(2024, 12, 29)   # Excel date
    wb["Change films"]["D3"] = "Finance confirmed"
    _decide(wb["2 · Flagged films"], "THE EXORCIST BELIEVER", None, "Not yet", "")
    wb.save(path)
    data, problems = W.read_workbook(path)
    assert problems == []
    assert len(data["review"]) == 2 and len(data["change_films"]) == 1   # "Not yet" is read, then ignored
    p = R.plan_from_workbook(data, items, TITLES, ALIASES, ENRICH)
    assert p.problems == [] and p.skipped == []
    got = {(c["key"], c["field"]): c["new"] for c in p.changes}
    assert got[("media_product_code|FOCUS|SEN", "imdb_id")] == "tt13609648"
    assert got[("tt1262426", "pvod_date")] == "2024-12-29"


def test_read_workbook_reports_bad_file(tmp_path):
    from us_hment_mmm.reference import review_workbook as W

    bad = tmp_path / "x.xlsx"
    bad.write_text("not excel")
    data, problems = W.read_workbook(bad)
    assert data == {} and "could not be opened" in problems[0]


class _FakeResult(list):
    def result(self):
        return self


class _Row(dict):
    pass


class _FakeClient:
    """Answers each review query by which table it reads; enough to run gather() end to end."""

    def __init__(self, history=()):
        self.history = list(history)

    def query(self, sql, job_config=None):
        if "title_history" in sql:
            rows = self.history
        elif "title_alias" in sql:
            rows = list(ALIASES.values())
        elif ".title`" in sql:
            rows = list(TITLES.values())
        elif "prisma_placements" in sql:
            rows = [{"biz": "ZFF", "code": "SEN", "name": "SENSE & SENSIBILITY", "n": 5,
                     "tags": ["tt39847629"]},
                    {"biz": "ZUE", "code": "SEN", "name": "SENSE & SENSIBILITY", "n": 2,
                     "tags": ["tt13609648"]},
                    {"biz": "ZUE", "code": "SSS", "name": "SHOOTING STARS", "n": 1, "tags": None}]
        elif "nbc_enrichment_title" in sql:
            rows = list(ENRICH.values()) + [{"imdb_id": "tt39847629", "title_name": "LORNE",
                                             "distributor": "FOCUS", "release_date": None,
                                             "pvod_date": None, "est_date": None}]
        elif "finance_std_chtt" in sql:
            rows = [{"imdb_id": "tt1262426", "pvod_date": "12/30/2024", "est_date": None}]
        elif "revenue_pos_usa" in sql:
            rows = [{"product_id": "999", "imdb_id": "tt1262426", "royalty": 5.0}]
        else:
            raise AssertionError("unexpected query")
        return _FakeResult(_Row(r) for r in rows)


def test_gather_end_to_end_with_fake_client():
    history = [{"record_key": "date|tt1262426|pvod_date|Finance", "new_value": "2024-12-30"}]
    g = R.gather(_FakeClient(history))
    by_what = {i["what"]: i for i in g["items"]}
    assert by_what["FOCUS SEN"]["has_suggestion"] is False
    assert "LORNE" in by_what["FOCUS SEN"]["why"]
    assert by_what["UP_HE SEN"]["suggested"] == "SENSE AND SENSIBILITY (tt13609648)"
    assert "UP_HE SSS" not in by_what                    # already mapped
    assert not any(i["section"] == 3 for i in g["items"])   # Finance date was rejected before
    assert by_what["Product ID 999"]["section"] == 4


def test_review_folders_and_move(tmp_path):
    f = R.review_folders(tmp_path)
    assert all(p.is_dir() for p in f.values())
    a = f["returned"] / "a.xlsx"
    a.write_text("x")
    (f["returned"] / "~$a.xlsx").write_text("lock")
    assert [p.name for p in R.returned_workbooks(f["returned"])] == ["a.xlsx"]
    (f["imported"] / "a.xlsx").write_text("earlier")
    assert R.move_to_imported(a, f["imported"]).name == "a_2.xlsx"
    assert not a.exists()


def test_run_issue_check_and_save_cycle(tmp_path, monkeypatch):
    import openpyxl

    monkeypatch.setattr(R, "bq_client", lambda: _FakeClient())
    applied = []
    monkeypatch.setattr(R, "_apply", lambda client, plan: applied.append(plan) or 7)

    first = R.run(root=tmp_path)                          # nothing returned -> issue
    assert first["status"] == "ISSUED"
    issued = tmp_path / "issued" / first["workbook"]
    assert issued.exists()

    wb = openpyxl.load_workbook(issued)                    # analyst fills it in
    _decide(wb["1 · Product codes"], "UP Home Ent", "SEN", "Accept", "tag matches")
    returned = tmp_path / "returned" / first["workbook"]
    wb.save(returned)

    check = R.run(root=tmp_path)                           # dry run: nothing moves
    assert check["status"] == "READY" and returned.exists() and not applied

    saved = R.run(dry_run=False, root=tmp_path)
    assert saved["status"] == "APPLIED" and saved["history_rows"] == 7
    assert not returned.exists() and (tmp_path / "imported" / saved["moved_to"]).exists()

    again = R.run(root=tmp_path)                           # returned is empty again
    assert again["status"] == "ISSUED"


def test_status_markdown_every_status_says_where():
    base = {"counts": {1: 1, 2: 0, 3: 0, 4: 0}, "changes": [], "problems": ["x"],
            "workbook": "w.xlsx", "moved_to": "w.xlsx", "history_rows": 0, "waiting": ["v.xlsx"]}
    for status in ("ISSUED", "PROBLEMS", "NOTHING", "READY", "IMPORTED_NOTHING", "APPLIED"):
        text_ = R.status_markdown(dict(base, status=status)).data
        assert "w.xlsx" in text_ and "v.xlsx" in text_
    issued = R.status_markdown(dict(base, status="ISSUED")).data
    assert "nbcu_mmm_dev" in issued and "issued" in issued and "returned" in issued


def test_explain_note_is_plain_english():
    text_ = R.explain_note("IMDb ID also used for: SHOOTING STARS. Check which name is right.")
    assert "SHOOTING STARS" in text_ and "Resolved" in text_
    assert R.explain_note("something else") == "something else"
