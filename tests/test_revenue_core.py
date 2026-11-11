"""Unit tests for the core revenue build (SQL text and report wording; no Google Cloud)."""

from types import SimpleNamespace

from us_hment_mmm import config
from us_hment_mmm.revenue import build_core as b


def _summary(**kw):
    base = dict(delivery="2026-10-30", kept_royalty=3e9, bad_dates=0, film_royalty=2e9,
                no_pvod_royalty=0, before_pvod_royalty=1e8, premium_royalty=9e8,
                after_est_royalty=1e9, films_with_revenue=320, films_with_ids=324,
                films_no_pvod=0, films_no_est=0, no_pvod_in_mmm=0, no_pvod_not_in_mmm=0,
                no_pvod_unreviewed=0, no_pvod_before_2020=0, no_pvod_in_mmm_royalty=0, shared_ids=0, shared_examples=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_sql_applies_the_agreed_filters():
    sql = b.table_sql()
    assert "data_source" in sql and "'Amazon Sale'" not in sql      # every source is kept
    assert f"country_cd) = '{config.REVENUE_COUNTRY}'" in sql
    assert "alias_type = 'revenue_product_id'" in sql and "category = 'film'" in sql
    assert "MAX(_delivery_date)" in sql
    assert "'before_pvod'" in sql and "'premium'" in sql and "'after_est'" in sql


def test_view_puts_presales_on_pvod_day_and_stops_before_est():
    sql = b.view_sql()
    assert "GREATEST(r.transaction_date, t.pvod_date)" in sql
    assert "revenue_window IN ('before_pvod', 'premium')" in sql


def test_report_plain_when_nothing_to_note():
    text = "\n".join(b.report(_summary()))
    assert "NOTE" not in text
    assert "$1,000,000,000" in text  # pre-sales + premium


def test_report_notes_missing_dates_and_shared_ids():
    text = "\n".join(b.report(_summary(films_no_pvod=2, films_no_est=3, shared_ids=1,
                                       shared_examples="12345")))
    assert "2 film(s) have revenue but no PVOD date" in text
    assert "3 film(s) have a PVOD date but no EST date" in text
    assert "linked to more than one film (e.g. 12345)" in text


def test_after_est_does_not_count_films_without_pvod():
    sql = " ".join(b.summary_sql().split())
    assert "s.transaction_date >= t.pvod_date AND s.transaction_date >= t.est_date" in sql


def test_no_us_premium_window_is_never_premium():
    table = " ".join(b.table_sql().split()) if hasattr(b, "table_sql") else ""
    summary = " ".join(b.summary_sql().split())
    assert "t.us_premium_window IS NOT FALSE AND s.transaction_date < t.pvod_date" in summary
    assert "AS no_us_premium_royalty" in summary
    if table:
        assert "WHEN t.us_premium_window IS FALSE THEN 'no_us_premium'" in table


class _Client:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def query(self, sql):
        if sql.startswith("ALTER TABLE"):                 # ensure_title_columns
            self.altered = sql
            return SimpleNamespace(result=lambda: [])
        self.sql.append(sql)
        rows = self.rows.pop(0) if self.rows else []
        return SimpleNamespace(result=lambda: rows)


def test_run_check_only_writes_nothing():
    c = _Client([[_summary()]])
    r = b.run(dry_run=True, client=c)
    assert r["status"] == "CHECKED" and len(c.sql) == 1
    assert "ADD COLUMN IF NOT EXISTS us_premium_window BOOL" in c.altered


def test_run_refuses_to_write_shared_ids():
    c = _Client([[_summary(shared_ids=1, shared_examples="1")]])
    r = b.run(dry_run=False, client=c)
    assert r["status"] == "NOT_WRITTEN" and len(c.sql) == 1


def test_run_write_passes_when_totals_match():
    check = SimpleNamespace(table_royalty=2e9, view_royalty=1e9, table_rows=10, view_films=5)
    c = _Client([[_summary()], [], [], [check]])
    r = b.run(dry_run=False, client=c)
    assert r["status"] == "PASS" and any("CREATE OR REPLACE TABLE" in s for s in c.sql)


def test_amazon_overlap_stops_the_build():
    assert "'Amazon Royalty', 'Amazon Sale'" in b.summary_sql()
    s = _summary(overlap_days=3, overlap_examples="123 2026-01-02")
    assert any("both Amazon Royalty and Amazon Sale" in n for n in b._notes(s))
    client = SimpleNamespace(query=lambda sql: SimpleNamespace(result=lambda: [s]))
    assert b.run(dry_run=False, client=client)["status"] == "NOT_WRITTEN"
