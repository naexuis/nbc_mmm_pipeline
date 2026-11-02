"""
Build the core revenue tables (us_hment_mmm_core) from the newest raw US revenue delivery.

  revenue_film_daily     table: royalty and units per film, per sale day, per retailer.
                         Every sale day is kept; `revenue_window` says where the day falls:
                           before_pvod   sold before the film's PVOD date (pre-sales)
                           premium       PVOD date up to the day before the EST date
                           after_est     on or after the EST date
                           no_pvod_date  the film has no PVOD date yet
  revenue_premium_daily  view: royalty and units per film per day in the premium window
                         (PVOD to EST - 1). Pre-sales are added to the PVOD day. This is
                         the revenue the MMM uses.

Rules (decision log): US rows only; "Amazon Sale" rows are left out (Amazon = "Amazon Royalty");
revenue links to a film through its revenue product IDs (title_alias, product_id1); films
and dates come from the title table.

Run (from /mnt/UniversalPicturesMMM):
    python -m us_hment_mmm.revenue.build_core            # check only, nothing is written
    python -m us_hment_mmm.revenue.build_core --write    # build the table and the view
"""

from __future__ import annotations

import argparse

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client

ROYALTY = "SAFE_CAST(REPLACE(REPLACE(TRIM(royalty_total_usd), '$', ''), ',', '') AS NUMERIC)"
UNITS = "SAFE_CAST(REPLACE(TRIM(units_sold), ',', '') AS NUMERIC)"


def _raw(name: str) -> str:
    return config.table_id(config.DATASET_RAW, name)


def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _quoted(values) -> str:
    return ", ".join("'" + v.replace("'", "\\'") + "'" for v in values)


def _sources_cte() -> str:
    """Newest delivery, US rows, kept data sources, plus the film each product ID belongs to."""
    raw = _raw(config.REVENUE_RAW_TABLE)
    exclude = _quoted(config.REVENUE_EXCLUDE_SOURCES) or "''"
    return f"""
delivery AS (SELECT MAX(_delivery_date) AS d FROM `{raw}`),
sales AS (
  SELECT TRIM(product_id1) AS product_id,
         SAFE.PARSE_DATE('%Y-%m-%d', TRIM(transaction_date)) AS transaction_date,
         IFNULL(NULLIF(TRIM(licensee_grouping_name), ''), '(blank)') AS retailer,
         {ROYALTY} AS royalty_usd, {UNITS} AS units_sold, _delivery_date
  FROM `{raw}`
  WHERE _delivery_date = (SELECT d FROM delivery)
    AND TRIM(country_cd) = '{config.REVENUE_COUNTRY}'
    AND IFNULL(TRIM(data_source), '') NOT IN ({exclude})
),
links AS (
  SELECT DISTINCT TRIM(alias_value) AS product_id, imdb_id
  FROM `{_core(config.TITLE_ALIAS_TABLE)}`
  WHERE alias_type = 'revenue_product_id' AND category = 'film' AND imdb_id IS NOT NULL
)"""


def table_sql() -> str:
    """CREATE statement for revenue_film_daily."""
    return f"""
CREATE OR REPLACE TABLE `{_core(config.REVENUE_CORE_TABLE)}`
CLUSTER BY imdb_id AS
WITH {_sources_cte()}
SELECT l.imdb_id, s.transaction_date, s.retailer,
       CASE WHEN t.pvod_date IS NULL THEN 'no_pvod_date'
            WHEN s.transaction_date < t.pvod_date THEN 'before_pvod'
            WHEN t.est_date IS NULL OR s.transaction_date < t.est_date THEN 'premium'
            ELSE 'after_est' END AS revenue_window,
       SUM(s.royalty_usd) AS royalty_usd,
       SUM(s.units_sold) AS units_sold,
       ANY_VALUE(s._delivery_date) AS delivery_date,
       CURRENT_TIMESTAMP() AS built_at
FROM sales s
JOIN links l USING (product_id)
JOIN `{_core(config.TITLE_TABLE)}` t USING (imdb_id)
GROUP BY l.imdb_id, s.transaction_date, s.retailer, revenue_window
"""


def view_sql() -> str:
    """CREATE statement for revenue_premium_daily."""
    return f"""
CREATE OR REPLACE VIEW `{_core(config.REVENUE_PREMIUM_VIEW)}` AS
SELECT r.imdb_id,
       GREATEST(r.transaction_date, t.pvod_date) AS revenue_date,
       SUM(r.royalty_usd) AS royalty_usd,
       SUM(r.units_sold) AS units_sold,
       SUM(IF(r.revenue_window = 'before_pvod', r.royalty_usd, 0)) AS presale_royalty_usd
FROM `{_core(config.REVENUE_CORE_TABLE)}` r
JOIN `{_core(config.TITLE_TABLE)}` t USING (imdb_id)
WHERE r.revenue_window IN ('before_pvod', 'premium')
GROUP BY r.imdb_id, revenue_date
"""


def summary_sql() -> str:
    """One row of checks computed straight from raw (works before anything is written)."""
    title = _core(config.TITLE_TABLE)
    return f"""
WITH {_sources_cte()},
shared AS (SELECT product_id FROM links GROUP BY product_id HAVING COUNT(*) > 1),
films AS (SELECT DISTINCT imdb_id FROM links),
by_film AS (
  SELECT l.imdb_id,
         SUM(s.royalty_usd) AS royalty,
         SUM(IF(t.pvod_date IS NULL, s.royalty_usd, 0)) AS no_pvod,
         SUM(IF(s.transaction_date < t.pvod_date, s.royalty_usd, 0)) AS before_pvod,
         SUM(IF(s.transaction_date >= t.pvod_date
                AND (t.est_date IS NULL OR s.transaction_date < t.est_date), s.royalty_usd, 0)) AS premium,
         SUM(IF(s.transaction_date >= t.pvod_date AND s.transaction_date >= t.est_date,
                s.royalty_usd, 0)) AS after_est,
         ANY_VALUE(t.pvod_date IS NULL) AS has_no_pvod,
         ANY_VALUE(t.in_mmm) AS in_mmm,
         ANY_VALUE(t.release_date) AS release_date
  FROM sales s JOIN links l USING (product_id) JOIN `{title}` t USING (imdb_id)
  GROUP BY l.imdb_id
)
SELECT
  (SELECT d FROM delivery) AS delivery,
  (SELECT SUM(royalty_usd) FROM sales) AS kept_royalty,
  (SELECT COUNTIF(transaction_date IS NULL) FROM sales) AS bad_dates,
  (SELECT SUM(royalty) FROM by_film) AS film_royalty,
  (SELECT SUM(no_pvod) FROM by_film) AS no_pvod_royalty,
  (SELECT SUM(before_pvod) FROM by_film) AS before_pvod_royalty,
  (SELECT SUM(premium) FROM by_film) AS premium_royalty,
  (SELECT SUM(after_est) FROM by_film) AS after_est_royalty,
  (SELECT COUNT(*) FROM by_film) AS films_with_revenue,
  (SELECT COUNT(*) FROM films) AS films_with_ids,
  (SELECT COUNTIF(has_no_pvod) FROM by_film) AS films_no_pvod,
  (SELECT COUNTIF(has_no_pvod AND in_mmm IS TRUE) FROM by_film) AS no_pvod_in_mmm,
  (SELECT COUNTIF(has_no_pvod AND in_mmm IS FALSE) FROM by_film) AS no_pvod_not_in_mmm,
  (SELECT COUNTIF(has_no_pvod AND in_mmm IS NULL) FROM by_film) AS no_pvod_unreviewed,
  (SELECT COUNTIF(has_no_pvod AND release_date < '2020-03-01') FROM by_film) AS no_pvod_before_2020,
  (SELECT SUM(IF(has_no_pvod AND in_mmm IS TRUE, royalty, 0)) FROM by_film) AS no_pvod_in_mmm_royalty,
  (SELECT COUNT(*) FROM by_film b JOIN `{title}` t USING (imdb_id)
     WHERE t.pvod_date IS NOT NULL AND t.est_date IS NULL) AS films_no_est,
  (SELECT COUNT(*) FROM shared) AS shared_ids,
  (SELECT STRING_AGG(product_id, ', ' LIMIT 10) FROM shared) AS shared_examples
"""


def table_check_sql() -> str:
    """Totals of the written table and view, to compare with the summary."""
    return f"""
SELECT
  (SELECT SUM(royalty_usd) FROM `{_core(config.REVENUE_CORE_TABLE)}`) AS table_royalty,
  (SELECT SUM(royalty_usd) FROM `{_core(config.REVENUE_PREMIUM_VIEW)}`) AS view_royalty,
  (SELECT COUNT(*) FROM `{_core(config.REVENUE_CORE_TABLE)}`) AS table_rows,
  (SELECT COUNT(DISTINCT imdb_id) FROM `{_core(config.REVENUE_PREMIUM_VIEW)}`) AS view_films
"""


def summary(client=None):
    client = client or bq_client()
    return list(client.query(summary_sql()).result())[0]


def write(client=None):
    client = client or bq_client()
    client.query(table_sql()).result()
    client.query(view_sql()).result()
    return list(client.query(table_check_sql()).result())[0]


def _money(x) -> str:
    return f"${float(x or 0):,.0f}"


def _close(a, b) -> bool:
    return abs(float(a or 0) - float(b or 0)) < 1.0


def _notes(s) -> list[str]:
    notes = []
    if s.films_no_pvod:
        notes.append(f"{s.films_no_pvod} film(s) have revenue but no PVOD date. Their revenue is "
                     "kept in the table but not used by the MMM until a date is added in "
                     f"05_Review_Titles. In MMM = yes: {s.no_pvod_in_mmm} "
                     f"({_money(s.no_pvod_in_mmm_royalty)}); In MMM = no: {s.no_pvod_not_in_mmm}; "
                     f"not reviewed: {s.no_pvod_unreviewed}; released before March 2020 "
                     f"(before PVOD existed): {s.no_pvod_before_2020}.")
    if s.films_no_est:
        notes.append(f"{s.films_no_est} film(s) have a PVOD date but no EST date, so their premium "
                     "window runs to the newest sale day. Add the EST date in 05_Review_Titles.")
    if s.bad_dates:
        notes.append(f"{s.bad_dates:,} sale row(s) have an unreadable transaction date.")
    if s.shared_ids:
        notes.append(f"{s.shared_ids} revenue product ID(s) are linked to more than one film "
                     f"(e.g. {s.shared_examples}). Fix them in 05_Review_Titles before writing; "
                     "otherwise that revenue would be counted twice.")
    return notes


def sections(s) -> dict:
    """What the notebook shows: one line of facts, a table, and notes."""
    used = float(s.before_pvod_royalty or 0) + float(s.premium_royalty or 0)
    rows = [
        ("Royalty kept from the delivery", s.kept_royalty),
        ("Linked to a film", s.film_royalty),
        ("  ↳ sold before PVOD (pre-sales)", s.before_pvod_royalty),
        ("  ↳ premium window (PVOD to EST − 1)", s.premium_royalty),
        ("  ↳ on or after EST", s.after_est_royalty),
        ("  ↳ film has no PVOD date", s.no_pvod_royalty),
        ("Used by the MMM (pre-sales + premium)", used),
    ]
    return {
        "facts": (f"Revenue delivery **{s.delivery}** · US rows only · left out: "
                  f"{', '.join(config.REVENUE_EXCLUDE_SOURCES)} · {s.films_with_ids} films have "
                  f"revenue product IDs ({s.films_with_revenue} with revenue in this delivery)"),
        "tables": [{"title": "Royalty", "columns": ["Item", "Royalty"], "money": ["Royalty"],
                    "rows": rows, "bold_last": True}],
        "notes": _notes(s),
    }


def report(s) -> list[str]:
    """Plain-text version of sections() for the command line."""
    sec = sections(s)
    lines = [sec["facts"].replace("**", "")]
    for name, amount in sec["tables"][0]["rows"]:
        lines.append(f"  {name.replace('↳ ', ''):<44} {_money(amount):>16}")
    return lines + ([""] + ["NOTE: " + n for n in sec["notes"]] if sec["notes"] else [])


def run(dry_run: bool = True, client=None) -> dict:
    """Check (and with dry_run=False, build). Returns lines to show and the outcome."""
    client = client or bq_client()
    s = summary(client)
    lines = report(s)
    sec = sections(s)
    if dry_run:
        return {"lines": lines, **sec, "status": "CHECKED",
                "message": "Nothing was written. To save, set DRY_RUN = False and run again."}
    if s.shared_ids:
        return {"lines": lines, **sec, "status": "NOT_WRITTEN",
                "message": "Not written: some revenue product IDs are linked to more than one "
                           "film (see the notes below). Fix them in 05_Review_Titles, then run again."}
    c = write(client)
    used = float(s.before_pvod_royalty or 0) + float(s.premium_royalty or 0)
    ok = _close(c.table_royalty, s.film_royalty) and _close(c.view_royalty, used)
    wrote = (f"Wrote `{config.DATASET_CORE}.{config.REVENUE_CORE_TABLE}` ({c.table_rows:,} rows, "
             f"{_money(c.table_royalty)}) and view `{config.DATASET_CORE}."
             f"{config.REVENUE_PREMIUM_VIEW}` ({c.view_films} films, {_money(c.view_royalty)}).")
    lines = lines + ["", wrote.replace("`", "")]
    sec["facts"] += "  \n" + wrote
    return {"lines": lines, **sec, "status": "PASS" if ok else "MISMATCH",
            "message": "Table and view totals match the check." if ok else
                       "Table or view totals differ from the check. Save this notebook and "
                       "send it to the pipeline owner."}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the core revenue table and view.")
    ap.add_argument("--write", action="store_true", help="Build the table and view (default: check only)")
    args = ap.parse_args()
    r = run(dry_run=not args.write)
    print("\n".join(r["lines"]))
    print(f"\n{r['status']}: {r['message']}")


if __name__ == "__main__":
    main()
