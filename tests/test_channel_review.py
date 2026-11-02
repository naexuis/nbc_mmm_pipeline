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
                         "film": f"tt{i % 5}"})
        else:
            rows.append({"key": f"P{i:06d}B", "site": "FACEBOOK", "name": f"P{i:06d}B_ZUE_X_SOCIAL FB {i}",
                         "mediaplan_label": "SOCIAL_FACEBOOK", "mmm_label": "SOCIAL_FACEBOOK",
                         "film": f"tt{i % 5}"})
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

    def query(self, sql):
        if "media_prisma_daily` WHERE link = 'film'" in sql:
            rows = self.placements
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


def test_suggest_groups_seen_sites_as_likely():
    model = S.train(_training())
    items = S.suggest(model, [{"site": "ROKU", "name": "P9_ZUE_X_OTT ROKU HOME"},
                              {"site": "BRANDNEW", "name": "P8_ZUE_X_SOCIAL FB"}])
    assert items[0]["suggested_mmm"] == "CTVOTT_ROKU" and items[0]["group"] == "likely"
    assert items[1]["group"] == "closer"


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
    assert r["listed"]["likely"] + r["listed"]["closer"] == 2
    assert client.tables["channel_model_runs"][0]["event"] == "issued"
    wb_path = tmp_path / "issued" / r["workbook"]
    wb = load_workbook(wb_path)
    # fill: accept the first likely row, type an answer on closer look, accept the TV row
    filled = {}
    for tab in ("1 · Likely right", "2 · Closer look", "3 · TV and radio"):
        ws = wb[tab]
        header = [c.value for c in ws[2]]
        for row in ws.iter_rows(min_row=3):
            if row[0].value in (None, "Nothing to decide on this tab."):
                continue
            dec = header.index("Decision")
            if tab == "2 · Closer look":
                row[dec].value = "Use my answer"
                row[header.index("Your MMM label")].value = "display video newsite"
            else:
                row[dec].value = "Accept"
            filled[tab] = True
    returned = tmp_path / "returned" / r["workbook"]
    wb.save(returned)

    r = CR.run(dry_run=True, root=tmp_path, client=client)
    assert r["status"] == "READY", r["problems"]
    labels = {c["key"]: c["mmm_label"] for c in r["changes"]}
    assert labels["STATION WNBC-TV"] == "SPOTTV_LOCALTV"
    assert labels.get("P2222222", "DISPLAY_VIDEO_NEWSITE") == "DISPLAY_VIDEO_NEWSITE"
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
    ws = wb["3 · TV and radio"]
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
