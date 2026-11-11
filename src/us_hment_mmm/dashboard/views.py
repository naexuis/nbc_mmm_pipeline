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


VIEWS = {"title": title_sql, "media_daily": media_daily_sql,
         "revenue_daily": revenue_daily_sql, "comp_mix": comp_mix_sql}


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
