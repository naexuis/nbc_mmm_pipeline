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
    assert "data_source" in sql and "'Amazon Sale'" in sql
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
    assert "s.transaction_date >= t.pvod_date AND s.transaction_date >= t.est_date" in b.summary_sql()
