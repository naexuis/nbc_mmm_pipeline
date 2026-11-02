"""
Build core media (us_hment_mmm_core) from the newest complete media snapshot.

  media_prisma_daily   table: Prisma spend (BuyAmountBillable) per placement per day,
                       with the film it belongs to (title_alias, by business group and
                       product code). One row per placement and day.

Rules (decision log, same as the old notebooks 103/104):
  - advertisers ZUE, ZFF, ZFE; campaigns with an APPROVED budget only;
  - leave out deleted buys and placements, Fee buys, Child placements and the Ad Ops
    test campaigns; credits (negative amounts) are kept;
  - each buy month is clipped to the placement's flight dates and spread evenly over those
    days. A placement with no flight dates takes them from its first and last buy month.
    A buy month that falls outside the flight is spread over the whole month (flagged).
  - spend whose product code is not mapped yet is kept, with link = 'unmapped'.
Spend dated after the snapshot is booked but not yet run: spend_status = 'scheduled'
(otherwise 'actual'); the model uses 'actual' only.
Channel labels are added in a later step.

Run (from /mnt/UniversalPicturesMMM):
    python -m us_hment_mmm.media.build_core            # check only, nothing is written
    python -m us_hment_mmm.media.build_core --write    # build the table
"""

from __future__ import annotations

import argparse

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client
from us_hment_mmm.reference.review import media_snapshot_expr

AMOUNT = "SAFE_CAST(REPLACE(TRIM(b.BuyAmountBillable), ',', '') AS NUMERIC)"
FLIGHT = "SAFE.PARSE_DATE('%d-%b-%Y', TRIM({c}))"
MONTH = "SAFE.PARSE_DATE('%d-%b-%Y', CONCAT('01-', INITCAP(TRIM(b.BuyMonth))))"


def _raw(name: str) -> str:
    return config.table_id(config.DATASET_RAW, name)


def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _quoted(values) -> str:
    return ", ".join("'" + v.replace("'", "\\'") + "'" for v in values)


def _snap(table: str) -> str:
    return f"_snapshot_date = {media_snapshot_expr(table)}"


def _groups_case(col: str) -> str:
    whens = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in config.MEDIA_BUSINESS_GROUPS.items())
    return f"CASE {col} {whens} END"


def _buys_cte() -> str:
    """Every buy row with the reason it is left out (NULL = kept)."""
    adv = _quoted(config.MEDIA_PRISMA_ADVERTISERS)
    tests = _quoted(config.MEDIA_TEST_CAMPAIGNS)
    return f"""
p AS (
  SELECT TRIM(PlacementNumber) AS placement_number, TRIM(PlacementName) AS placement_name,
         TRIM(Site) AS site, TRIM(ProductCode) AS product_code, TRIM(EstimateCode) AS estimate_code,
         TRIM(PackageType) AS package_type, TRIM(IsDeleted) AS is_deleted,
         {FLIGHT.format(c='PlacementStartDate')} AS flight_start,
         {FLIGHT.format(c='PlacementEndDate')} AS flight_end
  FROM `{_raw('prisma_placements')}` WHERE {_snap('prisma_placements')}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY TRIM(PlacementNumber)
                             ORDER BY IF(TRIM(IsDeleted) = 'False', 0, 1)) = 1
),
c AS (
  SELECT TRIM(CampaignPublicId) AS campaign_id, ANY_VALUE(TRIM(CampaignName)) AS campaign_name,
         ANY_VALUE(TRIM(BudgetApprovalStatus)) AS approval
  FROM `{_raw('prisma_campaigns')}` WHERE {_snap('prisma_campaigns')} GROUP BY 1
),
all_buys AS (
  SELECT TRIM(b.AdvertiserCode) AS business_code, TRIM(b.CampaignPublicId) AS campaign_id,
         c.campaign_name, TRIM(b.PlacementNumber) AS placement_number,
         COALESCE(p.placement_name, TRIM(b.PlacementName)) AS placement_name, p.site,
         COALESCE(NULLIF(p.product_code, ''), TRIM(b.ProductCode)) AS product_code,
         COALESCE(NULLIF(p.estimate_code, ''), TRIM(b.EstimateCode)) AS estimate_code,
         TRIM(b.BuyType) AS buy_type, p.package_type, p.flight_start, p.flight_end,
         {MONTH} AS buy_month, {AMOUNT} AS amount,
         CASE WHEN TRIM(b.AdvertiserCode) NOT IN ({adv}) THEN 'other advertiser'
              WHEN TRIM(b.CampaignPublicId) IN ({tests}) THEN 'test campaign'
              WHEN TRIM(b.IsDeleted) != 'False' THEN 'deleted buy'
              WHEN TRIM(b.BuyType) = 'Fee' THEN 'fee buy'
              WHEN p.placement_number IS NULL THEN 'placement not found'
              WHEN p.is_deleted != 'False' THEN 'deleted placement'
              WHEN p.package_type = 'Child' THEN 'child placement'
              WHEN IFNULL(c.approval, '') != '{config.MEDIA_APPROVED_STATUS}'
                   THEN CONCAT('budget ', IFNULL(LOWER(c.approval), 'unknown'))
              WHEN {MONTH} IS NULL THEN 'unreadable buy month'
         END AS left_out
  FROM `{_raw('prisma_placement_buys')}` b
  LEFT JOIN p ON p.placement_number = TRIM(b.PlacementNumber)
  LEFT JOIN c ON c.campaign_id = TRIM(b.CampaignPublicId)
  WHERE {_snap('prisma_placement_buys')}
),
kept AS (SELECT * FROM all_buys WHERE left_out IS NULL),
flights AS (
  SELECT placement_number,
         MIN(buy_month) AS first_month, LAST_DAY(MAX(buy_month)) AS last_month_end
  FROM kept GROUP BY 1
),
segments AS (
  SELECT k.*,
         IF(k.flight_start IS NULL OR k.flight_end IS NULL
            OR k.flight_start > k.flight_end, 'flight from buy months', NULL) AS flight_note,
         IF(k.flight_start IS NULL OR k.flight_end IS NULL OR k.flight_start > k.flight_end,
            f.first_month, k.flight_start) AS fs,
         IF(k.flight_start IS NULL OR k.flight_end IS NULL OR k.flight_start > k.flight_end,
            f.last_month_end, k.flight_end) AS fe
  FROM kept k JOIN flights f USING (placement_number)
),
clipped AS (
  SELECT *,
         GREATEST(buy_month, fs) <= LEAST(LAST_DAY(buy_month), fe) AS inside,
         IF(GREATEST(buy_month, fs) <= LEAST(LAST_DAY(buy_month), fe),
            GREATEST(buy_month, fs), buy_month) AS seg_start,
         IF(GREATEST(buy_month, fs) <= LEAST(LAST_DAY(buy_month), fe),
            LEAST(LAST_DAY(buy_month), fe), LAST_DAY(buy_month)) AS seg_end,
         NULLIF(ARRAY_TO_STRING([flight_note,
                IF(GREATEST(buy_month, fs) <= LEAST(LAST_DAY(buy_month), fe), NULL,
                   'buy month outside flight')], '; '), '') AS row_note
  FROM segments
),
links AS (
  SELECT business_group, TRIM(alias_value) AS product_code,
         ANY_VALUE(category) AS category, ANY_VALUE(imdb_id) AS imdb_id
  FROM `{_core(config.TITLE_ALIAS_TABLE)}`
  WHERE alias_type = 'media_product_code' GROUP BY 1, 2
)"""


def table_sql() -> str:
    """CREATE statement for media_prisma_daily."""
    return f"""
CREATE OR REPLACE TABLE `{_core(config.MEDIA_PRISMA_CORE_TABLE)}`
CLUSTER BY imdb_id, placement_number AS
WITH {_buys_cte()}
SELECT {media_snapshot_expr('prisma_placement_buys')} AS snapshot_date,
       x.business_code, {_groups_case('x.business_code')} AS business_group,
       x.product_code, IF(l.category = 'film', l.imdb_id, NULL) AS imdb_id,
       IFNULL(l.category, 'unmapped') AS link,
       x.campaign_id, ANY_VALUE(x.campaign_name) AS campaign_name,
       x.placement_number, ANY_VALUE(x.placement_name) AS placement_name,
       ANY_VALUE(x.site) AS site, ANY_VALUE(x.estimate_code) AS estimate_code,
       ANY_VALUE(x.buy_type) AS buy_type, ANY_VALUE(x.package_type) AS package_type,
       spend_date, SUM(x.amount / (DATE_DIFF(x.seg_end, x.seg_start, DAY) + 1)) AS spend_usd,
       'prisma BuyAmountBillable' AS cost_basis,
       IF(spend_date > {media_snapshot_expr('prisma_placement_buys')}, 'scheduled', 'actual')
         AS spend_status,
       STRING_AGG(DISTINCT x.row_note, '; ') AS date_note,
       CURRENT_TIMESTAMP() AS built_at
FROM clipped x CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(x.seg_start, x.seg_end)) AS spend_date
LEFT JOIN links l
  ON l.business_group = {_groups_case('x.business_code')} AND l.product_code = x.product_code
GROUP BY x.business_code, x.product_code, imdb_id, link, x.campaign_id, x.placement_number, spend_date
"""


def summary_sql() -> str:
    """Checks computed straight from raw (works before anything is written)."""
    return f"""
WITH {_buys_cte()}
SELECT 'left out' AS part, left_out AS item, COUNT(*) AS n, SUM(amount) AS spend
FROM all_buys WHERE left_out IS NOT NULL AND left_out != 'other advertiser' GROUP BY 2
UNION ALL
SELECT 'kept', IFNULL(l.category, 'unmapped'), COUNT(*), SUM(k.amount)
FROM kept k LEFT JOIN links l
  ON l.business_group = {_groups_case('k.business_code')} AND l.product_code = k.product_code
GROUP BY 2
UNION ALL
SELECT 'dates', 'flight taken from buy months', COUNT(*), SUM(amount)
FROM clipped WHERE flight_note IS NOT NULL
UNION ALL
SELECT 'dates', 'buy month outside flight (spread over the month)', COUNT(*), SUM(amount)
FROM clipped WHERE NOT inside
UNION ALL
SELECT 'scheduled', 'after the snapshot date (booked, not yet run)', COUNT(DISTINCT x.placement_number),
       SUM(x.amount / (DATE_DIFF(x.seg_end, x.seg_start, DAY) + 1))
FROM clipped x CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(x.seg_start, x.seg_end)) AS d
WHERE d > {media_snapshot_expr('prisma_placement_buys')}
UNION ALL
SELECT 'snapshot', CAST({media_snapshot_expr('prisma_placement_buys')} AS STRING), COUNT(*), SUM(amount)
FROM kept
"""


def table_check_sql() -> str:
    return f"""
SELECT COUNT(*) AS n, SUM(spend_usd) AS spend, COUNT(DISTINCT placement_number) AS placements,
       COUNT(DISTINCT imdb_id) AS films
FROM `{_core(config.MEDIA_PRISMA_CORE_TABLE)}`"""


def _money(x) -> str:
    return f"${float(x or 0):,.0f}"


KEPT_LABELS = {"film": "Linked to a film", "non_film": "Non-film campaigns (kept, not added to a film)",
               "unmapped": "Product code not mapped yet (map it in 05_Review_Titles)"}


def _nice(item: str) -> str:
    text = item.replace("_", " ")
    return text[:1].upper() + text[1:]


def sections(rows) -> tuple[dict, float]:
    """What the notebook shows (facts, tables, notes); also returns the kept total."""
    by = {}
    for r in rows:
        by.setdefault(r.part, []).append(r)
    snap = by.get("snapshot", [None])[0]
    kept_total = float(snap.spend or 0) if snap else 0.0
    kept = [(KEPT_LABELS.get(r.item, r.item), r.spend, r.n)
            for r in sorted(by.get("kept", []), key=lambda r: -float(r.spend or 0))]
    kept.append(("Total kept", kept_total, snap.n if snap else 0))
    left = [(_nice(r.item), r.spend, r.n)
            for r in sorted(by.get("left out", []), key=lambda r: -abs(float(r.spend or 0)))]
    cols = ["Item", "Spend", "Buy rows"]
    tables = [{"title": "Spend kept", "columns": cols, "money": ["Spend"], "counts": ["Buy rows"],
               "rows": kept, "bold_last": True},
              {"title": "Left out", "columns": cols, "money": ["Spend"], "counts": ["Buy rows"],
               "rows": left}]
    notes = [f"{_nice(r.item)}: {_money(r.spend)} ({r.n:,} buy rows). The spend "
             "is kept; these rows are marked in the column date_note."
             for r in by.get("dates", []) if r.n]
    for r in by.get("scheduled", []):
        if r.n:
            notes.append(f"{_money(r.spend)} of the spend kept is dated after the snapshot "
                         f"({r.n:,} placements): booked but not yet run. It is marked "
                         "spend_status = 'scheduled' and the model uses only 'actual' spend.")
    facts = (f"Prisma snapshot **{snap.item if snap else '?'}** · advertisers "
             f"{', '.join(config.MEDIA_PRISMA_ADVERTISERS)} · approved campaign budgets only")
    return {"facts": facts, "tables": tables, "notes": notes}, kept_total


def report(rows) -> tuple[list[str], float]:
    """Plain-text version of sections() for the command line; also returns the kept total."""
    sec, kept_total = sections(rows)
    lines = [sec["facts"].replace("**", "")]
    for t in sec["tables"]:
        lines.append(f"  {t['title']}:")
        for name, spend, n in t["rows"]:
            lines.append(f"     {name:<50} {_money(spend):>16}  ({n:,} buy rows)")
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
    wrote = (f"Wrote `{config.DATASET_CORE}.{config.MEDIA_PRISMA_CORE_TABLE}`: {c.n:,} rows, "
             f"{c.placements:,} placements, {c.films} films, {_money(c.spend)}.")
    sec["facts"] += "  \n" + wrote
    return {"lines": lines + ["", wrote.replace("`", "")], **sec,
            "status": "PASS" if ok else "MISMATCH",
            "message": "Table total matches the spend kept." if ok else
                       "Table total differs from the spend kept. Save this notebook and send "
                       "it to the pipeline owner."}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build core media (Prisma).")
    ap.add_argument("--write", action="store_true", help="Build the table (default: check only)")
    args = ap.parse_args()
    r = run(dry_run=not args.write)
    print("\n".join(r["lines"]))
    print(f"\n{r['status']}: {r['message']}")


if __name__ == "__main__":
    main()
