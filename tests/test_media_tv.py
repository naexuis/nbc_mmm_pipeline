"""Unit tests for the core TV / audio / MVPD build (SQL text and report; no Google Cloud)."""

from types import SimpleNamespace as R

from us_hment_mmm.media import build_tv as b


def test_sql_applies_the_agreed_rules():
    sql = b.table_sql()
    for text in ("Actual_Cost_Net", "NUMERIC '0.85' * ", "Gross_Cost",
                 "COALESCE(SAFE.PARSE_DATE('%d-%b-%Y', SUBSTR(TRIM(Affidavit_Date_Time)",
                 "'ZUE', 'ZUV', 'ZFF', 'ZFE'", "alias_type = 'media_product_code'", "AS spend_status"):
        assert text in sql, text


def test_sections_and_totals():
    rows = [R(part="snapshot", item="2026-10-30", n=0, spend=0),
            R(part="range", item="2018-01-01 to 2026-10-29", n=10, spend=300.0),
            R(part="kept", item="NetPak|film", n=6, spend=200.0),
            R(part="kept", item="SpotPak|unmapped", n=4, spend=100.0),
            R(part="client", item="NetPak|ZUE", n=6, spend=200.0),
            R(part="dates", item="SpotPak: affidavit date differs", n=2, spend=10.0)]
    sec, total = b.sections(rows)
    assert total == 300.0
    names = [r[0] for r in sec["tables"][0]["rows"]]
    assert names[0] == "NetPak: linked to a film" and names[-1] == "Total kept"
    assert any("affidavit" in n for n in sec["notes"])
