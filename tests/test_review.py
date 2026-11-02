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
        if "film_first_revenue" in sql:
            rows = [{"imdb_id": "tt1262426", "first_revenue_week": "2024-12-29",
                     "first_week_over_threshold": "2024-12-29",
                     "first_threshold_week_avg_price": 24.5,
                     "first_revenue_week_quality": "DELIVERED", "usable_as_evidence": True}]
        elif "amazon_week_quality" in sql:
            rows = [{"week_start": "2024-12-22", "status": "MISSING"},
                    {"week_start": "2024-12-29", "status": "DELIVERED"}]
        elif "title_history" in sql:
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


def test_suggest_for_pid_bundles_and_mismatches():
    assert R.suggest_for_pid("Wicked (2024)", "tt1262426", TITLES)[0] is True
    assert R.suggest_for_pid(None, "tt1262426", TITLES)[0] is True
    ok, why = R.suggest_for_pid("Wicked 2-Movie Collection", "tt1262426", TITLES)
    assert ok is False and "bundle" in why
    ok, why = R.suggest_for_pid("Twisters", "tt1262426", TITLES)
    assert ok is False and "does not match" in why


def test_pid_without_proposal_needs_an_answer():
    pids = [{"product_id": "555", "imdb_id": "tt1262426", "royalty": 9.0,
             "name": "Wicked 2-Movie Collection", "first_sale": "2025-01-01",
             "last_sale": "2026-10-29"}]
    items = R.build_items([], TITLES, ENRICH, [], pids)
    it = next(i for i in items if i["section"] == 4)
    assert it["has_suggestion"] is False and it["show"]["sales"] == "2025-01-01 to 2026-10-29"
    p = R.plan_from_workbook({"review": [_row(it, 3, "approve")]}, items, TITLES, ALIASES, ENRICH)
    assert "no proposed fix" in p.problems[0]


def test_old_placeholder_means_non_film():
    p = R.plan_from_workbook({"map_codes": [{"row": 3, "business_code": "ZUE", "product_code": "M2D",
                                             "film": "tt00000000", "reason": "draft"}]},
                             [], TITLES, ALIASES, ENRICH)
    assert p.problems == []
    got = {c["field"]: c["new"] for c in p.changes}
    assert got == {"category": "non_film"}          # imdb_id stays empty


def test_placeholder_dates_are_blank():
    assert R.parse_date("1899-12-30") is None
    assert R.parse_date("12/30/1899") is None
    assert R.parse_date(__import__("datetime").date(1899, 12, 30)) is None
    diffs = R.date_differences(TITLES, {}, {"tt1262426": {"pvod_date": R.parse_date("1899-12-30"),
                                                         "est_date": None}})
    assert diffs == []


def test_reader_tolerates_hidden_display_columns(tmp_path):
    import openpyxl

    from us_hment_mmm.reference import review_workbook as W

    items = _items()
    path = W.write_workbook(items, TITLES, tmp_path)
    wb = openpyxl.load_workbook(path)
    ws = wb["3 · Dates"]
    ws.cell(2, 5).value = "Approved date (used now)"   # an older workbook's header
    _decide(ws, "WICKED", None, "Accept", "ok")
    wb.save(path)
    data, problems = W.read_workbook(path)
    assert problems == [] and len(data["review"]) == 1


def test_deciding_a_date_turns_down_the_other_sources():
    enrich = dict(ENRICH, tt1262426={"pvod_date": "2024-12-30", "est_date": None,
                                     "title_name": "WICKED"})
    chtt = {"tt1262426": {"pvod_date": "2024-12-20", "est_date": None}}
    dates = R.date_differences(TITLES, enrich, chtt)
    items = R.build_items([], TITLES, enrich, dates, [])
    nbc = next(i for i in items if i["key"] == "date|tt1262426|pvod_date|NBC")
    fin = next(i for i in items if i["key"] == "date|tt1262426|pvod_date|Finance")
    # Accept NBC on one row only: Finance's date is turned down automatically
    p = R.plan_from_workbook({"review": [_row(nbc, 3, "approve")]}, items, TITLES, ALIASES,
                             enrich, chtt)
    got = {(c["key"], c["field"]): c["new"] for c in p.changes}
    assert got[("tt1262426", "pvod_date")] == "2024-12-30"
    assert got[("date|tt1262426|pvod_date|Finance", "reject")] == "2024-12-20"
    # Use my answer on both rows: both sources turned down once each, no duplicates
    p = R.plan_from_workbook({"review": [_row(nbc, 3, "use instead", use_instead="2024-12-25"),
                                         _row(fin, 4, "use instead", use_instead="2024-12-25")]},
                             items, TITLES, ALIASES, enrich, chtt)
    assert p.problems == []
    keys = [c["key"] for c in p.changes if c["field"] == "reject"]
    assert sorted(keys) == ["date|tt1262426|pvod_date|Finance", "date|tt1262426|pvod_date|NBC"]
    assert [c["new"] for c in p.changes if c["key"] == "tt1262426"] == ["2024-12-25"]
    # the next workbook would show nothing for this date
    rejected = {(c["key"], c["new"]) for c in p.changes if c["field"] == "reject"}
    after = dict(TITLES, tt1262426=dict(TITLES["tt1262426"], pvod_date="2024-12-25"))
    again = R.build_items([], after, enrich, R.date_differences(after, enrich, chtt), [], rejected)
    assert not any(i["section"] == 3 for i in again)


def test_amazon_check_wording():
    ev = {"first_revenue_week": "2024-12-22", "first_week_over_threshold": "2024-12-22",
          "first_threshold_week_avg_price": 21.76, "first_revenue_week_quality": "DELIVERED",
          "usable_as_evidence": True}
    assert R.amazon_check("2024-12-24", ev).endswith("date fits")
    assert "premium purchase" in R.amazon_check("2024-12-24", ev)
    assert "42 days after" in R.amazon_check("2025-02-02", ev)
    assert "days before" in R.amazon_check("2024-11-01", ev)
    assert R.amazon_check("2024-12-24", None) == ""
    assert "no evidence" in R.amazon_check("2024-12-24", dict(ev, usable_as_evidence=False))
    partial = dict(ev, first_revenue_week="2025-05-04", first_week_over_threshold="2025-06-15",
                   first_revenue_week_quality="PARTIAL", first_threshold_week_avg_price=24.99)
    text_ = R.amazon_check("2025-05-06", partial)          # Sneaks: compare with first week
    assert "2025-05-04" in text_ and "partial week" in text_ and text_.endswith("date fits")
    assert "missing" in R.amazon_check("2024-12-24", ev, {"2024-12-15"})


def test_genre_items_and_decisions():
    enrich = {"tt1262426": {"master_genre_1": "Musical", "master_genre_2": "", "title_name": "W"}}
    genres = R.genre_differences(TITLES, enrich)
    assert [(g["field"], g["new"]) for g in genres] == [("nbc_genre_1", "MUSICAL")]
    items = R.build_items([], TITLES, enrich, [], [], genres=genres)
    g = next(i for i in items if i["section"] == 5)
    assert g["key"] == "genre|tt1262426|nbc_genre_1" and g["show"]["now"] == "(none)"
    p = R.plan_from_workbook({"review": [_row(g, 3, "approve")]}, items, TITLES, ALIASES, enrich)
    assert {(c["key"], c["field"]): c["new"] for c in p.changes} == {
        ("tt1262426", "nbc_genre_1"): "MUSICAL"}
    p = R.plan_from_workbook({"change_films": [{"row": 3, "film": "tt1262426",
                                                "field": "nbc_genre_2", "new_value": "drama",
                                                "reason": "x"}]}, [], TITLES, ALIASES, enrich)
    assert p.changes[0]["new"] == "DRAMA"


def test_dates_tab_shows_amazon_check(tmp_path):
    import openpyxl

    from us_hment_mmm.reference import review_workbook as W

    dates = [{"imdb_id": "tt1262426", "title_name": "WICKED", "in_mmm": None,
              "field": "pvod_date", "current": "2024-12-31", "new": "2024-12-30",
              "source": "NBC"}]
    amazon = {"tt1262426": {"first_revenue_week": "2024-12-29",
                            "first_week_over_threshold": "2024-12-29",
                            "first_threshold_week_avg_price": 6.5,
                            "first_revenue_week_quality": "DELIVERED",
                            "usable_as_evidence": True}}
    items = R.build_items([], TITLES, ENRICH, dates, [], amazon=amazon)
    path = W.write_workbook(items, TITLES, tmp_path, ALIASES)
    wb = openpyxl.load_workbook(path)
    ws = wb["3 · Dates"]
    header = [c.value for c in ws[2]]
    cell = ws.cell(3, header.index("Amazon check") + 1).value
    assert "2024-12-29" in cell and "rental" in cell and cell.endswith("date fits")
    films = wb["Films now"]
    fh = [c.value for c in films[2]]
    rows = {films.cell(r, 1).value: r for r in range(3, films.max_row + 1)}
    codes = films.cell(rows["tt12921446"], fh.index("Media product codes") + 1).value
    assert codes == "UP_HE:SSS"
    assert "5 · NBC genre" in wb.sheetnames and "6 · Distributor" in wb.sheetnames


def test_film_links_lists_product_ids_and_codes():
    from us_hment_mmm.reference import review_workbook as W

    aliases = dict(ALIASES)
    aliases[("revenue_product_id", None, "123")] = {"imdb_id": "tt12921446", "category": "film"}
    aliases[("media_product_code", "UP_HE", "DFT")] = {"imdb_id": None, "category": "non_film"}
    links = W.film_links(aliases)
    assert links["tt12921446"] == {"_pids": ["123"], "_codes": ["UP_HE:SSS"]}


def test_amazon_tables_unreadable_is_a_note_not_an_error():
    from google.api_core.exceptions import GoogleAPIError

    class NoAmazon(_FakeClient):
        def query(self, sql, job_config=None):
            if "UPNA_client_he_amazon_mmt_dev" in sql:
                raise GoogleAPIError("Access Denied")
            return super().query(sql, job_config)

    g = R.gather(NoAmazon())
    assert "Amazon check left out" in g["amazon_note"]
    assert g["items"]


def test_product_code_rows_are_labelled_by_code(tmp_path):
    import openpyxl

    from us_hment_mmm.reference import review_workbook as W

    path = W.write_workbook(_items(), TITLES, tmp_path)
    wb = openpyxl.load_workbook(path)
    _decide(wb["1 · Product codes"], "Focus", "SEN", "Accept", "ok")
    wb.save(path)
    data, _ = W.read_workbook(path)
    assert data["review"][0]["label"] == "Focus SEN"


def test_view_sql_mentions_every_field():
    from us_hment_mmm.reference import views as V

    sql = V.as_of_sql()
    for f in V.COMPARE_FIELDS:
        assert f" AS {f}" in sql
    assert "changed_at <= as_of" in sql
    assert "revenue_product_ids" in V.detail_sql()
    assert "IS DISTINCT FROM" in V.check_sql()
    assert "ADD COLUMN IF NOT EXISTS nbc_genre_1" in V.upgrade_sql()


def test_standard_distributor_names():
    assert R.standard_distributor("Bleecker St.") == "BLEECKER STREET"
    assert R.standard_distributor("WELL GO USA, INC.") == "WELL GO USA"
    assert R.standard_distributor("GO") == "WELL GO USA"
    assert R.standard_distributor("OpenRoad Briarcliff") == "OPEN ROAD/BRIARCLIFF"
    assert R.standard_distributor("MMTP") == "MOMENTUM"
    assert R.standard_distributor("Neon") == "NEON"
    assert R.standard_distributor("") is None


def test_distributor_items_rename_and_fill():
    titles = dict(TITLES)
    titles["tt1262426"] = dict(titles["tt1262426"], distributor="UNIVERSAL PICTURES")
    titles["tt12921446"] = dict(titles["tt12921446"], distributor=None)
    enrich = {"tt12921446": {"distributor": "Bleecker St.", "title_name": "X"}}
    diffs = R.distributor_differences(titles, enrich)
    assert {(d["imdb_id"], d["current"], d["new"]) for d in diffs} == {
        ("tt1262426", "UNIVERSAL PICTURES", "UNIVERSAL"), ("tt12921446", None, "BLEECKER STREET")}
    items = R.build_items([], titles, enrich, [], [], distributors=diffs)
    it = next(i for i in items if i["key"] == "dist|tt1262426")
    p = R.plan_from_workbook({"review": [_row(it, 3, "approve")]}, items, titles, ALIASES, enrich)
    assert p.changes[0]["new"] == "UNIVERSAL"
    # a typed distributor is stored in its standard spelling
    p = R.plan_from_workbook({"change_films": [{"row": 3, "film": "tt1262426",
                                                "field": "distributor", "new_value": "bleecker st",
                                                "reason": "x"}]}, [], titles, ALIASES, enrich)
    assert p.changes[0]["new"] == "BLEECKER STREET"
