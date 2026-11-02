"""
Build core TV / audio / MVPD media (us_hment_mmm_core) from the newest Mediaocean snapshot.

  media_tv_daily   table: spend per day for each client, product code, network or station,
                   and source (NetPak = national TV, SpotPak = local TV and radio), with the
                   film it belongs to (title_alias, by business group and product code).

Rules (decision log):
  - clients ZUE, ZUV, ZFF, ZFE;
  - NetPak: date = Air_Date, cost = Actual_Cost_Net;
  - SpotPak: date = the affidavit date (when the station says the spot aired), otherwise
    Spot_Date; cost = 0.85 x Gross_Cost;
  - one row in the source = one spot on one day (cost is not spread); zero and negative
    amounts are kept;
  - spend whose product code is not mapped yet is kept, with link = 'unmapped'.
Spend dated after the snapshot is booked but not yet run: spend_status = 'scheduled'
(otherwise 'actual'); the model uses 'actual' only.
Channel labels are added in a later step (the network / station code is kept for that).

Run (from /mnt/UniversalPicturesMMM):
    python -m us_hment_mmm.media.build_tv            # check only, nothing is written
    python -m us_hment_mmm.media.build_tv --write    # build the table
"""

from __future__ import annotations

import argparse

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client
from us_hment_mmm.media.build_core import _groups_case, _money, _nice, _quoted
from us_hment_mmm.reference.review import media_snapshot_expr

DAY = "SAFE.PARSE_DATE('%d-%b-%Y', SUBSTR(TRIM({c}), 1, 11))"


def _raw(name: str) -> str:
    return config.table_id(config.DATASET_RAW, name)


def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _money_col(col: str) -> str:
    return f"SAFE_CAST(REPLACE(TRIM({col}), ',', '') AS NUMERIC)"


def _spots_cte() -> str:
    """Every spot row from both sources with its date, cost and reason left out (NULL = kept)."""
    clients = _quoted(config.MEDIA_TV_CLIENTS)
    factor = config.MEDIA_SPOTPAK_NET_FACTOR
    return f"""
spots AS (
  SELECT 'NetPak' AS source, TRIM(Client_Code) AS business_code, TRIM(Product_Code) AS product_code,
         TRIM(Network_Code) AS channel_code, TRIM(Media_Name) AS media_name,
         {DAY.format(c='Air_Date')} AS spend_date, CAST(NULL AS BOOL) AS dates_differ,
         {_money_col('Actual_Cost_Net')} AS cost
  FROM `{_raw('mediaocean_netpak')}` WHERE _snapshot_date = {media_snapshot_expr('mediaocean_netpak')}
  UNION ALL
  SELECT 'SpotPak', TRIM(Client_Code), TRIM(Product_Code), TRIM(Station_Name), TRIM(Media_Name),
         COALESCE({DAY.format(c='Affidavit_Date_Time')}, {DAY.format(c='Spot_Date')}),
         {DAY.format(c='Affidavit_Date_Time')} != {DAY.format(c='Spot_Date')},
         NUMERIC '{factor}' * {_money_col('Gross_Cost')}
  FROM `{_raw('mediaocean_spotpak')}` WHERE _snapshot_date = {media_snapshot_expr('mediaocean_spotpak')}
),
tagged AS (
  SELECT *, CASE WHEN business_code NOT IN ({clients}) THEN 'other client'
                 WHEN spend_date IS NULL THEN 'no readable date' END AS left_out
  FROM spots
),
kept AS (SELECT * FROM tagged WHERE left_out IS NULL),
links AS (
  SELECT business_group, TRIM(alias_value) AS product_code,
         ANY_VALUE(category) AS category, ANY_VALUE(imdb_id) AS imdb_id
  FROM `{_core(config.TITLE_ALIAS_TABLE)}`
  WHERE alias_type = 'media_product_code' GROUP BY 1, 2
)"""


def table_sql() -> str:
    """CREATE statement for media_tv_daily."""
    return f"""
CREATE OR REPLACE TABLE `{_core(config.MEDIA_TV_CORE_TABLE)}`
CLUSTER BY imdb_id, channel_code AS
WITH {_spots_cte()}
SELECT {media_snapshot_expr('mediaocean_netpak')} AS snapshot_date,
       k.source, k.business_code, {_groups_case('k.business_code')} AS business_group,
       k.product_code, IF(l.category = 'film', l.imdb_id, NULL) AS imdb_id,
       IFNULL(l.category, 'unmapped') AS link,
       k.channel_code, k.media_name, k.spend_date,
       SUM(k.cost) AS spend_usd, COUNT(*) AS spots,
       IF(k.source = 'NetPak', 'NetPak Actual_Cost_Net',
          'SpotPak {config.MEDIA_SPOTPAK_NET_FACTOR} x Gross_Cost') AS cost_basis,
       IF(k.spend_date > {media_snapshot_expr('mediaocean_netpak')}, 'scheduled', 'actual')
         AS spend_status,
       CURRENT_TIMESTAMP() AS built_at
FROM kept k
LEFT JOIN links l
  ON l.business_group = {_groups_case('k.business_code')} AND l.product_code = k.product_code
GROUP BY k.source, k.business_code, k.product_code, imdb_id, link, k.channel_code,
         k.media_name, k.spend_date
"""


def summary_sql() -> str:
    """Checks computed straight from raw (works before anything is written)."""
    return f"""
WITH {_spots_cte()}
SELECT 'kept' AS part, CONCAT(source, '|', IFNULL(l.category, 'unmapped')) AS item,
       COUNT(*) AS n, SUM(k.cost) AS spend
FROM kept k LEFT JOIN links l
  ON l.business_group = {_groups_case('k.business_code')} AND l.product_code = k.product_code
GROUP BY 2
UNION ALL
SELECT 'client', CONCAT(source, '|', business_code), COUNT(*), SUM(cost) FROM kept GROUP BY 2
UNION ALL
SELECT 'left out', left_out, COUNT(*), SUM(cost)
FROM tagged WHERE left_out = 'no readable date' GROUP BY 2
UNION ALL
SELECT 'dates', 'SpotPak: affidavit date differs from Spot_Date (affidavit used)', COUNT(*), SUM(cost)
FROM kept WHERE dates_differ
UNION ALL
SELECT 'range', CONCAT(CAST(MIN(spend_date) AS STRING), ' to ', CAST(MAX(spend_date) AS STRING)),
       COUNT(*), SUM(cost) FROM kept
UNION ALL
SELECT 'scheduled', 'after the snapshot date (booked, not yet aired)', COUNT(*), SUM(cost)
FROM kept WHERE spend_date > {media_snapshot_expr('mediaocean_netpak')}
UNION ALL
SELECT 'snapshot', CAST({media_snapshot_expr('mediaocean_netpak')} AS STRING), 0, 0
"""


def table_check_sql() -> str:
    return f"""
SELECT COUNT(*) AS n, SUM(spend_usd) AS spend, SUM(spots) AS spots,
       COUNT(DISTINCT channel_code) AS channels, COUNT(DISTINCT imdb_id) AS films
FROM `{_core(config.MEDIA_TV_CORE_TABLE)}`"""


LINKS = {"film": "linked to a film", "non_film": "non-film campaigns (kept, not added to a film)",
         "unmapped": "product code not mapped yet (map it in 05_Review_Titles)"}


def sections(rows) -> tuple[dict, float]:
    """What the notebook shows (facts, tables, notes); also returns the kept total."""
    by = {}
    for r in rows:
        by.setdefault(r.part, []).append(r)
    rng = by.get("range", [None])[0]
    snap = by.get("snapshot", [None])[0]
    kept_total = float(rng.spend or 0) if rng else 0.0
    kept = []
    for r in sorted(by.get("kept", []), key=lambda r: (r.item.split("|")[0], -float(r.spend or 0))):
        src, link = r.item.split("|")
        kept.append((f"{src}: {LINKS.get(link, link)}", r.spend, r.n))
    kept.append(("Total kept", kept_total, rng.n if rng else 0))
    clients = [(r.item.replace("|", ": "), r.spend, r.n)
               for r in sorted(by.get("client", []), key=lambda r: r.item)]
    cols = ["Item", "Spend", "Spots"]
    tables = [{"title": "Spend kept", "columns": cols, "money": ["Spend"], "counts": ["Spots"],
               "rows": kept, "bold_last": True},
              {"title": "By source and client", "columns": ["Source: client", "Spend", "Spots"],
               "money": ["Spend"], "counts": ["Spots"], "rows": clients}]
    notes = [f"{_nice(r.item)}: {_money(r.spend)} ({r.n:,} spots) left out."
             for r in by.get("left out", []) if r.n]
    notes += [f"{r.item}: {r.n:,} spots ({_money(r.spend)})."
              for r in by.get("dates", []) if r.n]
    for r in by.get("scheduled", []):
        if r.n:
            notes.append(f"{_money(r.spend)} of the spend kept ({r.n:,} spots) is dated after the "
                         "snapshot: booked but not yet aired. It is marked spend_status = "
                         "'scheduled' and the model uses only 'actual' spend.")
    facts = (f"Mediaocean snapshot **{snap.item if snap else '?'}** · clients "
             f"{', '.join(config.MEDIA_TV_CLIENTS)} · spend dates "
             f"{rng.item if rng else '?'} · SpotPak cost = {config.MEDIA_SPOTPAK_NET_FACTOR} x gross")
    return {"facts": facts, "tables": tables, "notes": notes}, kept_total


def report(rows) -> tuple[list[str], float]:
    sec, kept_total = sections(rows)
    lines = [sec["facts"].replace("**", "")]
    for t in sec["tables"]:
        lines.append(f"  {t['title']}:")
        for name, spend, n in t["rows"]:
            lines.append(f"     {name:<62} {_money(spend):>16}  ({n:,} spots)")
    return lines + ["NOTE: " + n for n in sec["notes"]], kept_total


def run(dry_run: bool = True, client=None) -> dict:
    """Check (and with dry_run=False, build). Returns what to show and the outcome."""
    client = client or bq_client()
    rows = list(client.query(summary_sql()).result())
    lines, kept_total = report(rows)
    sec, _ = sections(rows)
    if dry_run:
        return {"lines": lines, **sec, "status": "CHECKED",
                "message": "Nothing was written. To save, set DRY_RUN = False and run again."}
    client.query(table_sql()).result()
    c = list(client.query(table_check_sql()).result())[0]
    ok = abs(float(c.spend or 0) - kept_total) < 1.0
    wrote = (f"Wrote `{config.DATASET_CORE}.{config.MEDIA_TV_CORE_TABLE}`: {c.n:,} rows, "
             f"{c.spots:,} spots, {c.channels:,} networks/stations, {c.films} films, "
             f"{_money(c.spend)}.")
    sec["facts"] += "  \n" + wrote
    return {"lines": lines + ["", wrote.replace("`", "")], **sec,
            "status": "PASS" if ok else "MISMATCH",
            "message": "Table total matches the spend kept." if ok else
                       "Table total differs from the spend kept. Save this notebook and send "
                       "it to the pipeline owner."}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build core TV / audio / MVPD media.")
    ap.add_argument("--write", action="store_true", help="Build the table (default: check only)")
    args = ap.parse_args()
    r = run(dry_run=not args.write)
    print("\n".join(r["lines"]))
    print(f"\n{r['status']}: {r['message']}")


if __name__ == "__main__":
    main()
