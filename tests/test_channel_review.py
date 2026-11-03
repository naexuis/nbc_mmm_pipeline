"""Channel review: suggestions, workbook round trip and saving, with a stand-in BigQuery client."""

from datetime import date
from types import SimpleNamespace

from openpyxl import load_workbook

from us_hment_mmm.channels import review as CR
from us_hment_mmm.channels import suggest as S


def _training(n=40):
    rows = []
    for i in range(n):
        if i % 2:
            rows.append({"key": f"P{i:06d}A", "site": "ROKU", "name": f"P{i:06d}A_ZUE_X_OTT ROKU HOME {i}",
                         "mediaplan_label": "CTVOTT_ROKU", "mmm_label": "CTVOTT_ROKU",
                         "film": f"tt{i % 5}", "client": "ZUE"})
        else:
            rows.append({"key": f"P{i:06d}B", "site": "FACEBOOK", "name": f"P{i:06d}B_ZUE_X_SOCIAL FB {i}",
                         "mediaplan_label": "SOCIAL_FACEBOOK", "mmm_label": "SOCIAL_FACEBOOK",
                         "film": f"tt{i % 5}", "client": "ZUE"})
    return rows


class _Rows(list):
    def to_dataframe(self):
        raise NotImplementedError


class _Row(dict):
    def items(self):
        return super().items()


class FakeClient:
    def __init__(self):
        self.tables = {"channel_label": [], "title_history": [], "channel_model_runs": []}
        self.placements = [
            {"key_type": "prisma_placement", "key": "P1111111", "client": "ZUE", "imdb_id": "tt1",
             "site": "ROKU", "name": "P1111111_ZUE_X_OTT ROKU HOME NEW", "buy_type": "Video",
             "package_type": "Standalone", "first_day": date(2025, 1, 1),
             "last_day": date(2025, 1, 31), "spend": 1000.0, "film": "FILM A"},
            {"key_type": "prisma_placement", "key": "P2222222", "client": "ZFF", "imdb_id": "tt2",
             "site": "NEWSITE", "name": "P2222222_ZFF_Y_SOMETHING", "buy_type": "Display",
             "package_type": "Standalone", "first_day": date(2025, 2, 1),
             "last_day": date(2025, 2, 28), "spend": 500.0, "film": "FILM B"}]
        self.tv = [{"key_type": "tv_network", "key": "STATION WNBC-TV", "media": "SPOT TV",
                    "sources": "SpotPak", "clients": "ZFF", "spend": 300.0}]
        self.tv_films = [{"key": "STATION WNBC-TV", "film": "FILM B", "first_day": date(2025, 2, 1),
                          "last_day": date(2025, 2, 10), "spend": 200.0},
                         {"key": "STATION WNBC-TV", "film": "FILM A", "first_day": date(2025, 1, 5),
                          "last_day": date(2025, 1, 9), "spend": 100.0}]

    def query(self, sql):
        if "media_prisma_daily` WHERE link = 'film'" in sql:
            rows = self.placements
        elif "title_name AS film" in sql:
            rows = self.tv_films
        elif "media_tv_daily` m" in sql:
            rows = self.tv
        elif "l.detail AS name" in sql:
            rows = _training()
        elif "COUNT(*) AS n FROM" in sql:
            rows = [{"mmm_label": "CTVOTT_ROKU", "n": 20}]
        elif "channel_model_runs" in sql:
            rows = self.tables["channel_model_runs"]
        elif "SELECT * FROM" in sql:
            rows = self.tables["channel_label"]
        else:
            rows = []
        return SimpleNamespace(result=lambda: _Rows(_Row(r) for r in rows))

    def load_table_from_json(self, payload, table_id, job_config=None):
        name = table_id.split(".")[-1]
        if str(job_config.write_disposition).endswith("WRITE_TRUNCATE"):
            self.tables[name] = list(payload)
        else:
            self.tables[name] = self.tables.get(name, []) + list(payload)
        return SimpleNamespace(result=lambda: None)

    def get_table(self, table_id):
        return SimpleNamespace(num_rows=len(self.tables[table_id.split(".")[-1]]))


def test_how_sure_groups():
    model = S.train(_training())
    items = S.suggest(model, [{"site": "ROKU", "name": "P9_ZUE_X_OTT ROKU HOME", "client": "ZUE"},
                              {"site": "ROKU", "name": "P9_ZFE_X_OTT ROKU HOME", "client": "ZFE"},
                              {"site": "BRANDNEW", "name": "P8_ZUE_X_SOCIAL FB", "client": "ZUE"}])
    assert items[0]["suggested_mmm"] == "CTVOTT_ROKU" and items[0]["group"] == "likely"
    assert items[1]["group"] == "closer"          # client has no confirmed labels yet
    assert items[2]["group"] == "new_site"


def test_site_with_several_labels_is_closer_look():
    rows = _training() + [{"key": "PX", "site": "ROKU", "name": "PX_ZUE_PROGRAMMATIC ROKU",
                           "mediaplan_label": "PROGRAMMATIC_ROKU", "mmm_label": "PROGRAMMATIC_ROKU",
                           "film": "tt9", "client": "ZUE"}]
    items = S.suggest(S.train(rows), [{"site": "ROKU", "name": "P1_OTT ROKU HOME", "client": "ZUE"}])
    assert items[0]["group"] == "closer"


def test_evaluate_reports_accuracy():
    q = S.evaluate(_training(), lambda x: x.split("_")[0])
    assert q["training_rows"] == 40 and q["exact"] > 0.9


def test_tv_suggestions():
    assert CR.tv_suggestion("STATION WNBC-TV", None, "SPOT TV") == "SPOTTV_LOCALTV"
    assert CR.tv_suggestion("FOXXN", "FOX NEWS", "CABLE") == "CABLE_FOXNEWS"
    assert CR.tv_suggestion("SIRIUS SATELLITE RAD", None, "NETWORK RADIO") == "AUDIO_SIRIUSSATELLITERAD"


def test_label_checks():
    assert CR.clean_label(" ctvott amazon-firetv ") == "CTVOTT_AMAZON_FIRETV"
    assert CR.label_problem("CTVOTT_ROKU") is None
    assert "known parent" in CR.label_problem("WRONG_THING")


def test_round_trip(tmp_path):
    client = FakeClient()
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    assert r["status"] == "ISSUED" and r["listed"]["tv"] == 1
    tv = load_workbook(tmp_path / "issued" / r["workbook"])["2 · TV and radio"]
    header = [c.value for c in tv[2]]
    films = [tv.cell(row=i, column=header.index("Film") + 1).value for i in (3, 4)]
    assert films == ["FILM B", "FILM A"]
    assert tv.cell(row=3, column=header.index("First spot") + 1).value == "2025-02-01"
    assert r["listed"]["likely"] + r["listed"]["closer"] + r["listed"]["new_site"] == 2
    assert client.tables["channel_model_runs"][0]["event"] == "issued"
    wb_path = tmp_path / "issued" / r["workbook"]
    wb = load_workbook(wb_path)
    # fill: accept the first likely row, type an answer on closer look, accept the TV row
    for tab in ("1 · Placements", "2 · TV and radio"):
        ws = wb[tab]
        header = [c.value for c in ws[2]]
        assert header.index("Your media-plan label (optional)") < header.index("Your MMM label")
        for row in ws.iter_rows(min_row=3):
            if row[0].value in (None, "Nothing to decide on this tab."):
                continue
            dec = header.index("Decision")
            if tab == "1 · Placements" and row[header.index("How sure")].value == "New site":
                row[dec].value = "Use my answer"
                row[header.index("Your MMM label")].value = "display video newsite"
            else:
                row[dec].value = "Accept"
    returned = tmp_path / "returned" / r["workbook"]
    wb.save(returned)

    r = CR.run(dry_run=True, root=tmp_path, client=client)
    assert r["status"] == "READY", r["problems"]
    labels = {c["key"]: c["mmm_label"] for c in r["changes"]}
    assert labels["STATION WNBC-TV"] == "SPOTTV_LOCALTV"
    assert labels["P2222222"] == "DISPLAY_VIDEO_NEWSITE"
    assert [c["new_label"] for c in r["changes"] if c["key"] == "P2222222"] == [True]
    assert client.tables["channel_label"] == []

    r = CR.run(dry_run=False, root=tmp_path, client=client)
    assert r["status"] == "APPLIED"
    assert len(client.tables["channel_label"]) == len(labels)
    assert all(h["table_name"] == "channel_label" for h in client.tables["title_history"])
    assert client.tables["channel_model_runs"][-1]["event"] == "saved"
    assert not list((tmp_path / "returned").glob("*.xlsx"))


def test_bad_label_stops(tmp_path):
    client = FakeClient()
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    wb = load_workbook(tmp_path / "issued" / r["workbook"])
    ws = wb["2 · TV and radio"]
    header = [c.value for c in ws[2]]
    ws.cell(row=3, column=header.index("Decision") + 1, value="Use my answer")
    ws.cell(row=3, column=header.index("Your MMM label") + 1, value="TELEVISION_THING")
    wb.save(tmp_path / "returned" / r["workbook"])
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    assert r["status"] == "PROBLEMS" and "known parent" in r["problems"][0]


def test_quality_text_first_run_and_drop(tmp_path):
    client = FakeClient()
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    text = CR.quality_markdown(r).data
    assert "Trained on **40**" in text and "⚠️" not in text
    r["runs"] = [{"event": "issued", "exact": 0.999, "workbook": "older.xlsx"}]
    r["quality"] = {**r["quality"], "exact": 0.5}
    assert "⚠️" in CR.quality_markdown(r).data


def test_site_names_compared_loosely():
    assert S.site_key("Peacock.com") == S.site_key("PEACOCK") == "PEACOCK"
    assert S.site_key("TIKTOK INC") == "TIKTOK"
    assert S.site_key("X CORP FKA TWITTER") == "TWITTER"
    assert S.site_key("SNAP INC FK SNAPCHAT") == "SNAPCHAT"
    assert S.site_key("YouTube Select") != S.site_key("YOUTUBE.COM")


def test_one_label_per_code():
    item = {"key_type": "tv_network", "key": "X N", "suggested_mmm": "CABLE_X",
            "suggested_plan": "CABLE_X", "network": "X", "media": "CABLE"}
    ids = {"tv_network|X N": item}
    same = [{"tab": "2 · TV and radio", "row": 3, "_id": "tv_network|X N", "decision": "Accept",
             "suggested_mmm": "CABLE_X"},
            {"tab": "2 · TV and radio", "row": 4, "_id": "tv_network|X N", "decision": "Accept",
             "suggested_mmm": "CABLE_X"}]
    changes, problems, _ = CR.decide(ids, same)
    assert len(changes) == 1 and not problems
    differ = same[:1] + [{"tab": "2 · TV and radio", "row": 4, "_id": "tv_network|X N",
                          "decision": "Use my answer", "your_mmm": "CABLE_Y"}]
    changes, problems, _ = CR.decide(ids, differ)
    assert "one code gets one label" in problems[0]


def test_summary_tab_counts(tmp_path):
    r = CR.run(dry_run=True, root=tmp_path, client=FakeClient())
    wb = load_workbook(tmp_path / "issued" / r["workbook"])
    assert wb.sheetnames[:3] == ["Summary", "How to fill this in", "1 · Placements"]
    ws = wb["Summary"]
    labels = {ws.cell(row=i, column=1).value: ws.cell(row=i, column=2).value for i in range(4, 11)}
    assert labels["Films on this workbook"] == 2
    assert labels["Placements still to review"] == "=B5-B6"
    head = next(i for i in range(1, 30) if ws.cell(row=i, column=1).value == "Film")
    films = [ws.cell(row=i, column=1).value for i in (head + 1, head + 2)]
    assert set(films) == {"FILM A", "FILM B"}
    assert ws.cell(row=head, column=8).value == "Credit"
    assert ws.cell(row=head, column=9).value == "Spend changed"
    assert str(ws.cell(row=head + 1, column=10).value).startswith("=COUNTIFS(")
    assert "Remove placement" in ws.cell(row=head + 1, column=10).value
    assert ws.cell(row=head, column=2).value == "Release date"
    assert "Waiting for a later workbook" not in labels      # everything is on this workbook


def test_remove_placement():
    item = {"key_type": "prisma_placement", "key": "P0", "suggested_mmm": "CTVOTT_SAMSUNG",
            "suggested_plan": "CTVOTT_SAMSUNG", "site": "SAMSUNG", "name": "x"}
    ids = {"prisma_placement|P0": item}
    row = {"tab": "1 · Placements", "row": 3, "_id": "prisma_placement|P0",
           "decision": "Remove placement"}
    changes, problems, _ = CR.decide(ids, [row])
    assert not changes and "needs a Reason" in problems[0]
    changes, problems, _ = CR.decide(ids, [{**row, "reason": "$0 added value"}], set())
    assert not problems and changes[0]["mmm_label"] == "REMOVED" and not changes[0]["new_label"]


def test_view_shows_removed():
    from us_hment_mmm.channels import labels as L
    assert "'REMOVED', 'REMOVED'" in L.view_sql()


def test_removed_placement_comes_back_when_spend_changes(tmp_path):
    client = FakeClient()
    client.placements[1].update(was_decided=True, was_spend=0.0, current_mmm="REMOVED")
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    assert r["listed"]["changed"] == 1
    ws = load_workbook(tmp_path / "issued" / r["workbook"])["1 · Placements"]
    header = [c.value for c in ws[2]]
    sure = [ws.cell(row=i, column=header.index("How sure") + 1).value for i in (3, 4)]
    assert "Removed, spend changed (was $0)" in sure


def test_remove_saves_the_spend(tmp_path):
    client = FakeClient()
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    wb = load_workbook(tmp_path / "issued" / r["workbook"])
    ws = wb["1 · Placements"]
    header = [c.value for c in ws[2]]
    for row in ws.iter_rows(min_row=3):
        if row[header.index("Film")].value == "FILM B":
            row[header.index("Decision")].value = "Remove placement"
            row[header.index("Reason (optional)")].value = "test"
    wb.save(tmp_path / "returned" / r["workbook"])
    r = CR.run(dry_run=False, root=tmp_path, client=client)
    assert r["status"] == "APPLIED"
    saved = {x["key"]: x for x in client.tables["channel_label"]}
    assert saved["P2222222"]["mmm_label"] == "REMOVED" and saved["P2222222"]["decided_spend"] == 500.0


def test_labelled_placement_comes_back_with_its_label(tmp_path):
    client = FakeClient()
    client.placements[0].update(was_decided=True, was_spend=900.0, current_mmm="CTVOTT_ROKU",
                                current_plan="CTVOTT_ROKU")
    r = CR.run(dry_run=True, root=tmp_path, client=client)
    ws = load_workbook(tmp_path / "issued" / r["workbook"])["1 · Placements"]
    header = [c.value for c in ws[2]]
    row = next(row for row in ws.iter_rows(min_row=3, values_only=True)
               if row[header.index("Film")] == "FILM A")
    assert row[header.index("How sure")] == "Spend changed (was $900)"
    assert row[header.index("Suggested MMM label")] == "CTVOTT_ROKU"


def test_baseline_fills_only_missing(capsys):
    client = FakeClient()
    client.tables["channel_label"] = [
        {"key_type": "prisma_placement", "key": "P1111111", "mmm_label": "CTVOTT_ROKU",
         "decided_spend": None},
        {"key_type": "prisma_placement", "key": "P9", "mmm_label": "X", "decided_spend": 5.0}]
    client.query_spend = True
    assert CR.baseline(dry_run=True, client=client) >= 0


def test_credits_take_the_film_and_site_label():
    placements = [
        {"key": "P1", "imdb_id": "tt1", "site": "FACEBOOK.COM", "package_type": "Standalone",
         "suggested_mmm": "SOCIAL_FACEBOOK", "suggested_plan": "SOCIAL_FACEBOOK", "group": "closer"},
        {"key": "P2", "imdb_id": "tt1", "site": "Facebook", "package_type": "CreditPlacement",
         "suggested_mmm": "CTVOTT_ROKU", "suggested_plan": "CTVOTT_ROKU", "group": "closer"},
        {"key": "P3", "imdb_id": "tt2", "site": "YOUTUBE.COM", "package_type": "CreditPlacement",
         "suggested_mmm": "CTVOTT_AMAZON_PRIME_VIDEO", "group": "closer"}]
    training = [{"film": "tt2", "site": "YOUTUBE.COM", "mmm_label": "CTVOTT_YOUTUBE",
                 "mediaplan_label": "VIDEO_YOUTUBE"}]
    CR.suggest_credits(placements, training)
    assert placements[1]["group"] == "credit" and placements[1]["suggested_mmm"] == "SOCIAL_FACEBOOK"
    assert placements[2]["suggested_mmm"] == "CTVOTT_YOUTUBE"
    assert placements[2]["suggested_plan"] == "VIDEO_YOUTUBE"
    assert placements[0]["group"] == "closer"
