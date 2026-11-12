"""
BigQuery views for the Looker Studio dashboard (us_hment_mmm_core.dash_*). Read-only views:
they always show the newest core tables and channel labels, so nothing needs refreshing.

Every view applies the same rules, so Looker Studio only has to draw charts:
  - titles released on or after config.MMM_FIRST_RELEASE_DATE
  - Home Entertainment media only (clients ZUE, ZUV), spend that has run, linked to a film
  - model channels from config.MODEL_CHANNELS; fees and removed placements left out;
    unlabelled spend shows as "No channel yet"
  - premium revenue = the MMM's premium window (revenue_premium_daily), US royalty

Views
  dash_title         one row per title: dates, distributor, In MMM, media, premium revenue
  dash_media_daily   title x day x model channel x source: Home Ent media spend
  dash_revenue_daily title x day: premium revenue (US royalty), with days since PVOD
  dash_comp_mix      title x model channel (every labelled channel, $0 included): spend, the
                     title's total and its labelled total (spend with a channel), so for any
                     set of comps share = SUM(spend) / (SUM(labelled_total) / channels shown).
                     "No channel yet" is left out of the mix; no_channel_spend and
                     channel_check (a line for every film) say how much is still unlabelled.
  dash_recon         title: ours (US + Canada premium royalty) vs Finance's CHTT premium revenue,
                     gap, status and date notes; same query and rules as QA 08 Step 9
  dash_revenue_sources  TABLE (it reads the raw revenue tables, so it is saved, not a view):
                     film x country x data source x product ID x product name, royalty in the
                     same window and links as Ours, so US + Canada = Ours for every film;
                     std_chtt repeats the film's CHTT figure on every row (use MAX)

Usage (from /mnt/UniversalPicturesMMM):
    python -m us_hment_mmm.dashboard.views            # check only: BigQuery validates the SQL
    python -m us_hment_mmm.dashboard.views --write    # create or replace the views
"""

from __future__ import annotations

import argparse

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client


def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def view_name(short: str) -> str:
    return _core(config.DASHBOARD_VIEW_PREFIX + short)


def _channel_sql(col: str) -> str:
    whens = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in config.MODEL_CHANNELS.items())
    return (f"CASE WHEN {col} = '{config.CHANNEL_NEEDS_LABEL}' THEN '{config.MODEL_CHANNEL_NONE}' "
            f"ELSE CASE {col} {whens} END END")


def model_channels() -> list[str]:
    seen = []
    for v in list(config.MODEL_CHANNELS.values()) + [config.MODEL_CHANNEL_NONE]:
        if v not in seen:
            seen.append(v)
    return seen


def _titles_sql() -> str:
    return f"""
      SELECT imdb_id, INITCAP(title_name) AS title, title_name, release_date, pvod_date, est_date,
             INITCAP(distributor) AS distributor,
             CASE in_mmm WHEN TRUE THEN 'Yes' WHEN FALSE THEN 'No' ELSE 'Not reviewed' END AS in_mmm,
             IF(us_premium_window IS FALSE, 'No', 'Yes') AS us_premium_window
      FROM `{_core(config.TITLE_TABLE)}`
      WHERE release_date >= DATE '{config.MMM_FIRST_RELEASE_DATE}'"""


def media_daily_sql() -> str:
    media = _core(config.MEDIA_DAILY_VIEW)
    return f"""
WITH t AS ({_titles_sql()}),
m AS (
  SELECT d.imdb_id, d.spend_date, d.media_source, d.business_code, d.spend_usd,
         {_channel_sql('d.channel_parent')} AS model_channel
  FROM `{media}` d
  WHERE d.link = 'film' AND d.spend_status = 'actual'
    AND d.media_owner = '{config.HOME_ENT_OWNER}'
    AND d.channel_parent != '{config.CHANNEL_REMOVED}')
SELECT t.imdb_id, t.title, t.distributor, t.release_date, t.pvod_date, t.est_date, t.in_mmm,
       m.spend_date AS date, m.media_source AS source, m.business_code AS client,
       m.model_channel, SUM(m.spend_usd) AS media_spend
FROM m JOIN t USING (imdb_id)
WHERE m.model_channel IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11"""


def revenue_daily_sql() -> str:
    return f"""
WITH t AS ({_titles_sql()})
SELECT t.imdb_id, t.title, t.distributor, t.release_date, t.pvod_date, t.est_date, t.in_mmm,
       p.revenue_date AS date, DATE_DIFF(p.revenue_date, t.pvod_date, DAY) AS days_since_pvod,
       SUM(p.royalty_usd) AS premium_revenue
FROM `{_core(config.REVENUE_PREMIUM_VIEW)}` p JOIN t USING (imdb_id)
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9"""


def title_sql() -> str:
    return f"""
WITH t AS ({_titles_sql()}),
m AS (SELECT imdb_id, SUM(media_spend) AS media_spend,
             SUM(IF(model_channel = '{config.MODEL_CHANNEL_NONE}', media_spend, 0)) AS no_channel_spend,
             MIN(date) AS first_media_date, MAX(date) AS last_media_date
      FROM ({media_daily_sql()}) GROUP BY 1),
r AS (SELECT imdb_id, SUM(premium_revenue) AS premium_revenue FROM ({revenue_daily_sql()}) GROUP BY 1)
SELECT t.imdb_id, t.title, t.distributor, t.release_date, t.pvod_date, t.est_date, t.in_mmm,
       t.us_premium_window, EXTRACT(YEAR FROM t.release_date) AS release_year,
       IFNULL(m.media_spend, 0) AS media_spend, IFNULL(m.no_channel_spend, 0) AS no_channel_spend,
       IFNULL(r.premium_revenue, 0) AS premium_revenue,
       SAFE_DIVIDE(r.premium_revenue, m.media_spend) AS revenue_per_media_dollar,
       m.first_media_date, m.last_media_date
FROM t LEFT JOIN m USING (imdb_id) LEFT JOIN r USING (imdb_id)
WHERE m.imdb_id IS NOT NULL OR r.imdb_id IS NOT NULL"""


def comp_mix_sql() -> str:
    none = config.MODEL_CHANNEL_NONE
    channels = ", ".join(f"'{c}'" for c in model_channels() if c != none)
    return f"""
WITH s AS (SELECT imdb_id, title, distributor, release_date, model_channel,
                  SUM(media_spend) AS spend
           FROM ({media_daily_sql()}) GROUP BY 1, 2, 3, 4, 5),
films AS (SELECT imdb_id, ANY_VALUE(title) AS title, ANY_VALUE(distributor) AS distributor,
                 ANY_VALUE(release_date) AS release_date, SUM(spend) AS title_total,
                 SUM(IF(model_channel = '{none}', spend, 0)) AS no_channel_spend
          FROM s GROUP BY 1),
grid AS (SELECT f.*, c AS model_channel FROM films f CROSS JOIN UNNEST([{channels}]) AS c)
SELECT g.imdb_id, g.title, g.distributor, g.release_date, g.model_channel,
       IFNULL(s.spend, 0) AS spend, g.title_total,
       g.title_total - g.no_channel_spend AS labelled_total,
       g.no_channel_spend,
       SAFE_DIVIDE(g.no_channel_spend, g.title_total) AS no_channel_share,
       IF(g.no_channel_spend > 0,
          FORMAT("$%'d of $%'d (%d%%) has no channel yet; the mix uses only its labelled spend",
                 CAST(ROUND(g.no_channel_spend) AS INT64),
                 CAST(ROUND(g.title_total) AS INT64),
                 CAST(ROUND(100 * SAFE_DIVIDE(g.no_channel_spend, g.title_total)) AS INT64)),
          "All Home Ent media has a channel") AS channel_check
FROM grid g LEFT JOIN s USING (imdb_id, model_channel)"""


def recon_sql() -> str:
    """CHTT reconciliation, one row per title: QA 08's query (qa.summary.chtt_sql) with the same
    status rules as qa.summary.recon_frame, so the dashboard and QA 08 always agree."""
    from us_hment_mmm.qa import summary as Q

    def fin_date(col: str) -> str:
        d = f"SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR({col}, 1, 10))"
        return f"IF({d} >= DATE '2000-01-01', {d}, NULL)"   # CHTT placeholder dates (1899-12-30)

    return f"""
WITH r AS ({Q.chtt_sql()}),
c AS (
  SELECT r.*,
         CAST(IFNULL(r.us_premium, 0) AS FLOAT64) + CAST(IFNULL(r.can_premium, 0) AS FLOAT64) AS ours,
         CAST(IFNULL(r.finance, 0) AS FLOAT64) AS fin,
         CAST(IFNULL(r.us_fin_dates, 0) AS FLOAT64)
           + CAST(IFNULL(r.can_fin_dates, 0) AS FLOAT64) AS ours_fin,
         {fin_date('r.fin_pvod')} AS finance_pvod, {fin_date('r.fin_est')} AS finance_est
  FROM r),
s AS (
  SELECT c.*, SAFE_DIVIDE(ours - fin, NULLIF(fin, 0)) AS gap_pct,
         CASE WHEN fin = 0 THEN '{Q.RECON_NO_FINANCE}'
              WHEN ours = 0 THEN '{Q.RECON_NO_POS}'
              WHEN est_date IS NULL OR est_date > last_sale THEN '{Q.RECON_OPEN}'
              WHEN SAFE_DIVIDE(ours - fin, fin) BETWEEN {Q.RECON_UNDER} AND {Q.RECON_OVER}
                THEN '{Q.RECON_OK}'
              ELSE '{Q.RECON_CHECK}' END AS status
  FROM c)
SELECT imdb_id, INITCAP(title_name) AS title, release_date, INITCAP(distributor) AS distributor,
       CASE in_mmm WHEN TRUE THEN 'Yes' WHEN FALSE THEN 'No' ELSE 'Not reviewed' END AS in_mmm,
       IF(us_premium_window IS FALSE, 'No', 'Yes') AS us_premium_window,
       pvod_date, est_date, finance_pvod, finance_est,
       CAST(IFNULL(us_premium, 0) AS FLOAT64) AS us_premium,
       CAST(IFNULL(can_premium, 0) AS FLOAT64) AS can_premium,
       ours, fin AS finance, ours - fin AS gap, gap_pct,
       status, IF(status IN ('{Q.RECON_OK}', '{Q.RECON_CHECK}'), 'Yes', 'No') AS compared,
       ours_fin AS ours_with_finance_est,
       SAFE_DIVIDE(ours_fin - fin, NULLIF(fin, 0)) AS gap_pct_with_finance_est,
       ARRAY_TO_STRING(ARRAY(SELECT n FROM UNNEST([
         IF(us_premium_window IS FALSE, 'No US premium window: ours is Canada only', NULL),
         IF(finance_pvod IS NOT NULL AND (pvod_date IS NULL OR pvod_date != finance_pvod),
            FORMAT('Finance PVOD %t (ours %s)', finance_pvod, IFNULL(CAST(pvod_date AS STRING), 'blank')),
            NULL),
         IF(finance_est IS NOT NULL AND (est_date IS NULL OR est_date != finance_est),
            FORMAT('Finance EST %t (ours %s)', finance_est, IFNULL(CAST(est_date AS STRING), 'blank')),
            NULL)]) AS n WHERE n IS NOT NULL), '; ') AS date_note,
       IF(est_reason IS NULL, NULL,
          CONCAT(SUBSTR(est_reason, 1, 90), ' (', IFNULL(est_by, ''), ', ', IFNULL(est_on, ''), ')'))
         AS est_source,
       last_sale
FROM s"""


def revenue_sources_sql() -> str:
    """Revenue bridge, one row per film x country x data source x product ID x product name:
    royalty in the same window and with the same product ID links as "Ours" in dash_recon
    (US: pre-sales + PVOD to EST - 1, none for films with no US premium window;
    Canada: pre-sales + PVOD to EST - 1), so US + Canada adds up to Ours for every film."""
    raw_us = config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE)
    raw_can = config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE_CAN)
    exclude = ", ".join(f"'{x}'" for x in config.REVENUE_EXCLUDE_SOURCES) or "''"
    money = "SAFE_CAST(REPLACE(REPLACE(TRIM(CAST(royalty_total_usd AS STRING)), '$', ''), ',', '') AS FLOAT64)"

    def rows(table: str, country_cd: str, label: str) -> str:
        return f"""
  SELECT '{label}' AS country, TRIM(CAST(product_id1 AS STRING)) AS product_id,
         IFNULL(NULLIF(TRIM(data_source), ''), '(blank)') AS data_source,
         IFNULL(NULLIF(TRIM(reported_product_name), ''), '(blank)') AS product_name,
         SAFE.PARSE_DATE('%Y-%m-%d', TRIM(transaction_date)) AS day, {money} AS usd
  FROM `{table}`
  WHERE _delivery_date = (SELECT MAX(_delivery_date) FROM `{table}`)
    AND TRIM(country_cd) = '{country_cd}'
    AND IFNULL(TRIM(data_source), '') NOT IN ({exclude})"""

    return f"""
WITH links AS (
  SELECT DISTINCT TRIM(alias_value) AS product_id, imdb_id FROM `{_core(config.TITLE_ALIAS_TABLE)}`
  WHERE alias_type = 'revenue_product_id' AND category = 'film' AND imdb_id IS NOT NULL),
t AS (
  SELECT imdb_id, title_name, pvod_date, est_date, us_premium_window FROM `{_core(config.TITLE_TABLE)}`
  WHERE release_date >= DATE '{config.MMM_FIRST_RELEASE_DATE}' AND pvod_date IS NOT NULL),
s AS ({rows(raw_us, config.REVENUE_COUNTRY, 'US')}
  UNION ALL{rows(raw_can, 'CAN', 'Canada')}),
b AS (
  SELECT t.imdb_id, INITCAP(t.title_name) AS title, t.pvod_date, t.est_date, s.country,
         s.data_source, s.product_id, s.product_name,
         SUM(IF(s.day < t.pvod_date, s.usd, 0)) AS presale_royalty,
         SUM(IF(s.day >= t.pvod_date, s.usd, 0)) AS premium_window_royalty,
         SUM(s.usd) AS royalty
  FROM s JOIN links l USING (product_id) JOIN t USING (imdb_id)
  WHERE (t.est_date IS NULL OR s.day < t.est_date)
    AND (s.country = 'Canada' OR t.us_premium_window IS NOT FALSE)
  GROUP BY 1, 2, 3, 4, 5, 6, 7, 8)
SELECT b.*, r.finance AS std_chtt     -- the film's CHTT figure, the same on every row: use MAX
FROM b LEFT JOIN (SELECT imdb_id, finance FROM `{view_name('recon')}`) r USING (imdb_id)"""


def revenue_sources_check_sql() -> str:
    """Films where US + Canada in dash_revenue_sources differs from Ours in dash_recon by > $1."""
    return f"""
SELECT COUNT(*) AS films, COUNTIF(ABS(IFNULL(b.total, 0) - r.ours) > 1) AS differ
FROM `{view_name('recon')}` r
LEFT JOIN (SELECT imdb_id, SUM(royalty) AS total FROM `{view_name('revenue_sources')}`
           GROUP BY 1) b USING (imdb_id)"""


VIEWS = {"title": title_sql, "media_daily": media_daily_sql,
         "revenue_daily": revenue_daily_sql, "comp_mix": comp_mix_sql, "recon": recon_sql}
TABLES = {"revenue_sources": revenue_sources_sql}     # read the raw tables: saved as tables


def run(write: bool = False, client=None) -> list[str]:
    from google.cloud import bigquery

    client = client or bq_client()
    lines = []
    for short, make in VIEWS.items():
        sql = make()
        job = client.query(f"SELECT * FROM ({sql}) LIMIT 1",
                           job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        lines.append(f"OK    {view_name(short)}  (BigQuery checked the SQL; "
                     f"{len(job.schema)} columns)")
        if write:
            client.query(f"CREATE OR REPLACE VIEW `{view_name(short)}` AS {sql}").result()
            lines[-1] = f"SAVED {view_name(short)}  ({len(job.schema)} columns)"
    for short, make in TABLES.items():
        sql = make()
        job = client.query(f"SELECT * FROM ({sql}) LIMIT 1",
                           job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        lines.append(f"OK    {view_name(short)}  (BigQuery checked the SQL; "
                     f"{len(job.schema)} columns; table, rebuilt on save)")
        if write:
            client.query(f"CREATE OR REPLACE TABLE `{view_name(short)}` CLUSTER BY imdb_id "
                         f"AS {sql}").result()
            chk = list(client.query(revenue_sources_check_sql()).result())[0]
            same = ("US + Canada = Ours on the reconciliation page for all "
                    f"{chk['films']} films" if chk["differ"] == 0 else
                    f"⚠️ US + Canada differs from Ours for {chk['differ']} of {chk['films']} films")
            lines[-1] = f"SAVED {view_name(short)}  ({len(job.schema)} columns; {same})"
    lines.append("Saved." if write else "Check only: nothing was written. To save, add --write.")
    return lines


def markdown(lines: list[str], dry_run: bool):
    """Notebook message for run()'s lines."""
    from IPython.display import Markdown

    head = ("🔎 **CHECK ONLY — nothing was written.** BigQuery checked every view."
            if dry_run else "✅ **SAVED** — the dashboard views were re-created.")
    body = "\n".join(f"- `{ln.split()[1].split('.')[-1]}` {ln.split('(', 1)[1].rstrip(')')}"
                     for ln in lines if ln.startswith(("OK", "SAVED")))
    return Markdown(f"{head}\n\n{body}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Create the dashboard views (dash_*).")
    ap.add_argument("--write", action="store_true", help="create or replace the views")
    print("\n".join(run(write=ap.parse_args().write)))


if __name__ == "__main__":
    main()
