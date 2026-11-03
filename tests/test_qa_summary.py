"""QA summary (notebook 08): reads only, and every chart and table builds from the data."""

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

from us_hment_mmm import config  # noqa: E402
from us_hment_mmm.qa import summary as Q  # noqa: E402

FILMS = pd.DataFrame([
    {"imdb_id": "tt1", "title_name": "FILM A", "release_date": "2024-03-01",
     "distributor": "UNIVERSAL", "in_mmm": True, "pvod_date": "2024-04-01",
     "est_date": "2024-05-01", "nbc_genre_1": "DRAMA", "prisma": 1_000_000.0, "tv": 500_000.0,
     "no_channel": 100_000.0, "removed": 50_000.0, "clients": "ZFF, ZUE", "first_spend": None,
     "last_spend": None, "royalty_all": 3_000_000.0, "after_est": 1_000_000.0,
     "premium": 2_000_000.0},
    {"imdb_id": "tt2", "title_name": "FILM B", "release_date": "2025-06-01",
     "distributor": "FOCUS", "in_mmm": None, "pvod_date": None, "est_date": None,
     "nbc_genre_1": "COMEDY", "prisma": None, "tv": None, "no_channel": None, "removed": None,
     "clients": None, "first_spend": None, "last_spend": None, "royalty_all": 10_000.0,
     "after_est": 0.0, "premium": None},
])


class FakeClient:
    def __init__(self):
        self.sql = []

    def query(self, sql):
        self.sql.append(sql)
        if "COUNTIF(release_date" in sql:
            df = pd.DataFrame([{"films": 400, "mmm_era": 2, "in_mmm_yes": 1, "in_mmm_no": 0,
                                "in_mmm_unset": 1}])
        elif "media_snapshot" in sql:
            df = pd.DataFrame([{"media_snapshot": "2026-10-30", "revenue_delivery": "2026-10-30"}])
        elif "WITH m AS" in sql:
            df = FILMS
        elif "channel_parent, d.media_source" in sql:
            df = pd.DataFrame([{"channel_parent": "CTVOTT", "media_source": "Prisma", "spend": 9e5},
                               {"channel_parent": config.CHANNEL_NEEDS_LABEL,
                                "media_source": "Prisma", "spend": 1e5},
                               {"channel_parent": config.CHANNEL_REMOVED,
                                "media_source": "Prisma", "spend": 5e4}])
        elif "DATE_TRUNC(p.revenue_date" in sql:
            df = pd.DataFrame([{"month": m, "royalty": 2e5} for m in ("2022-12-01", "2024-03-01")])
        elif "DATE_TRUNC" in sql:
            df = pd.DataFrame([{"month": "2024-03-01", "media_source": s, "spend": 1e5}
                               for s in Q.SOURCES])
        elif "r.retailer" in sql:
            df = pd.DataFrame([{"retailer": f"R{i}", "royalty": 100.0 - i} for i in range(15)])
        else:
            df = pd.DataFrame([{"spend": 25_000.0}])
        df = df.copy()
        for c in df.columns:                       # BigQuery returns NUMERIC money as Decimal
            if df[c].dtype == float:
                df[c] = [None if pd.isna(v) else Decimal(str(v)) for v in df[c]]
        return SimpleNamespace(result=lambda: SimpleNamespace(to_dataframe=lambda: df))

    def load_table_from_json(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("the QA summary must not write")


def test_reads_only_and_uses_current_labels():
    client = FakeClient()
    Q.load(client)
    joined = "\n".join(client.sql)
    assert all(s.lstrip().upper().startswith(("SELECT", "WITH")) for s in client.sql)
    assert config.MEDIA_DAILY_VIEW in joined           # the view that carries today's labels
    assert f"DATE '{config.MMM_FIRST_RELEASE_DATE}'" in joined
    assert "spend_status = 'actual'" in joined


def test_headline_numbers():
    h = Q.headline(Q.load(FakeClient()))
    assert h["media"] == 1_500_000
    assert h["with_channel"] == 1_350_000                # minus no channel and removed
    assert h["films_with_media"] == 1 and h["films_with_premium"] == 1
    assert h["scheduled"] == 25_000
    assert "$1.5M" in Q.headline_markdown(h).data
    assert Q._m(-4) == "−$4"


def test_every_chart_and_table_builds():
    data = Q.load(FakeClient())
    for chart in (Q.chart_channels, Q.chart_channel_coverage,
                  Q.chart_retailers):
        fig = chart(data)
        assert fig.axes and fig._repr_png_()[:4] == b"\x89PNG"
    labels = [t.get_text() for t in Q.chart_channels(data).axes[0].get_yticklabels()]
    assert "No channel yet" in labels and "CTV / OTT" in labels
    assert not any("REMOVED" in x for x in labels)
    table = Q.film_table(data).data
    assert list(table["Title"]) == ["FILM A", "FILM B"]
    assert table.iloc[1]["PVOD"] == "" and table.iloc[1]["In MMM"] == "not reviewed"
    assert table.iloc[0]["Client"] == "ZFF, ZUE"


def test_notebook_calls_exist():
    nb = Path(__file__).parents[1] / "notebooks" / "08_QA_Summary.ipynb"
    src = nb.read_text()
    for name in ("load", "headline_markdown", "chart_channels", "chart_monthly",
                 "chart_spend_vs_revenue", "chart_channel_coverage", "chart_retailers",
                 "film_table"):
        assert f"qa.{name}(" in src and hasattr(Q, name)
    assert "outputs\": []" in src and sys


def test_scatter_is_interactive_one_dot_per_title():
    pytest.importorskip("plotly")
    fig = Q.chart_spend_vs_revenue(Q.load(FakeClient()))
    assert [t.name for t in fig.data] == ["Universal (1)"]   # coloured by distributor
    assert len(fig.data[0].x) == 1
    assert fig.data[0].customdata[0][0] == "Film A"          # hover shows the title


def test_monthly_is_interactive_and_starts_in_2023():
    pytest.importorskip("plotly")
    fig = Q.chart_monthly(Q.load(FakeClient()))
    assert [len(t.x) for t in fig.data] == [1, 1]            # December 2022 left out
    assert fig.layout.hovermode == "x unified"


def test_save_film_csv(tmp_path):
    msg = Q.save_film_csv(Q.load(FakeClient()), folder=tmp_path / "qa")
    files = list((tmp_path / "qa").glob("qa-titles-*.csv"))
    assert len(files) == 1 and files[0].name in msg.data
    saved = pd.read_csv(files[0])
    assert list(saved["Title"]) == ["FILM A", "FILM B"]
    assert saved.loc[0, "IMDb ID"] == "tt1" and saved.loc[0, "Media spend"] == 1_500_000
