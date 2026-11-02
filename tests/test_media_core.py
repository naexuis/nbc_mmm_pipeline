"""Unit tests for the core media build (SQL text and report wording; no Google Cloud)."""

from types import SimpleNamespace as R

from us_hment_mmm.media import build_core as b


def test_sql_applies_the_agreed_rules():
    sql = b.table_sql()
    for text in ("'CPZLLW'", "'Fee'", "'Child'", "'APPROVED'", "IsDeleted) != 'False'",
                 "GENERATE_DATE_ARRAY(x.seg_start, x.seg_end)", "alias_type = 'media_product_code'",
                 "INITCAP(TRIM(b.BuyMonth))", "'%d-%b-%Y'", "AS spend_status"):
        assert text in sql, text


def test_report_lists_kept_and_left_out():
    rows = [R(part="snapshot", item="2026-10-30", n=31608, spend=625e6),
            R(part="kept", item="film", n=30000, spend=600e6),
            R(part="kept", item="unmapped", n=1608, spend=25e6),
            R(part="left out", item="budget not_submitted", n=561, spend=12.5e6),
            R(part="dates", item="buy month outside flight (spread over the month)", n=0, spend=0)]
    lines, total = b.report(rows)
    text = "\n".join(lines)
    assert total == 625e6
    assert "Linked to a film" in text and "Product code not mapped yet" in text
    assert "Budget not submitted" in text
    assert "NOTE" not in text


def test_run_write_passes_when_totals_match():
    from types import SimpleNamespace as N
    summary = [R(part="snapshot", item="2026-10-30", n=2, spend=100.0)]
    check = N(n=4, spend=100.0, placements=1, films=1)
    queue = [summary, [], [check]]
    sql = []

    class C:
        def query(self, s):
            sql.append(s)
            rows = queue.pop(0)
            return N(result=lambda: rows)

    r = b.run(dry_run=False, client=C())
    assert r["status"] == "PASS" and "CREATE OR REPLACE TABLE" in sql[1]
