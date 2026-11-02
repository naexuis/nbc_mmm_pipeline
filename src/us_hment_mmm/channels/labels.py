"""
Channel label table (us_hment_mmm_core.channel_label) and the media_daily view.

  channel_label   one row per labelled item:
                    key_type 'prisma_placement' -> key = Prisma placement number
                    key_type 'tv_network'       -> key = NetPak network code / SpotPak station
                  with the media-plan label and the MMM label (e.g. CTVOTT_AMAZON_FIRETV;
                  its parent channel is the start of the label, e.g. CTVOTT).
                  After the seed, labels change only through the channel review, and every
                  change is kept in title_history (table_name = 'channel_label').
  media_daily     view: Prisma and TV / audio / MVPD spend together, with each row's labels
                  and parent channel ('NEEDS A CHANNEL' when there is no label yet).

Seed (run once, from /mnt/UniversalPicturesMMM):
    python -m us_hment_mmm.channels.labels            # check only, nothing is written
    python -m us_hment_mmm.channels.labels --write    # create the table, history and view
Refresh only the view (safe to repeat):
    python -m us_hment_mmm.channels.labels --view
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client
from us_hment_mmm.reference import titles as T
from us_hment_mmm.reference.review import current_user, media_snapshot_expr

SEED_DIR = Path(__file__).resolve().parents[3] / "reference_seed"
PLACEMENT_FILE = "channel_labels_seed_20251112.csv"
NETWORK_FILE = "nspack_network_dict.json"
# From the old MVPD notebook (added there in memory only): ZUV "ONE N" is FreeWheel.
NETWORK_EXTRA = {"ONE": {"network": "FREEWHEEL", "mapping": "MVPD_FREEWHEEL"}}

LABEL_SCHEMA = [
    bigquery.SchemaField("key_type", "STRING", mode="REQUIRED"),   # prisma_placement | tv_network
    bigquery.SchemaField("key", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING"),            # site, or network name
    bigquery.SchemaField("detail", "STRING"),          # placement name, or media name
    bigquery.SchemaField("mediaplan_label", "STRING"),
    bigquery.SchemaField("mmm_label", "STRING"),
    bigquery.SchemaField("source", "STRING"),          # where the label came from
    bigquery.SchemaField("reason", "STRING"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_by", "STRING"),
]
LABEL_FIELDS = ("mediaplan_label", "mmm_label")
PARENT_RE = re.compile(r"^(" + "|".join(sorted(config.CHANNEL_PARENTS, key=len, reverse=True))
                       + r")(?:_|$)")


# =============================================================================
# Pure helpers (tested without Google Cloud)
# =============================================================================

def parent_of(label: str | None) -> str | None:
    m = PARENT_RE.match(label or "")
    return m.group(1) if m else None


def norm(s) -> str:
    return re.sub(r"[^0-9A-Z]+", " ", str(s or "").upper()).strip()


def network_label(code: str, media_name: str, d: dict) -> tuple[str | None, str | None, str]:
    """
    (network name, media-plan label, note) for a NetPak network code or SpotPak station.
    Exact code first; "STATION…" counts as radio only on radio spots; then the first 4
    letters. A label must fit the media: no AUDIO label on TV, only AUDIO on radio.
    """
    code = (code or "").strip()
    radio = (media_name or "").strip().upper() in config.CHANNEL_RADIO_MEDIA
    hit = d.get(code)
    if hit is None and radio and "STATION" in code.upper():
        hit = d.get("STATION")
    if hit is None:
        hit = d.get(code[:4].replace(" ", ""))
    if hit is None:
        return None, None, "not in the network dictionary"
    network, label = hit.get("network"), hit.get("mapping")
    if not label or label == "UNKNOWN":
        return network, None, "in the network dictionary without a label"
    is_audio = parent_of(label) == "AUDIO"
    if radio != is_audio:
        return network, None, (f"dictionary label {label} does not fit "
                               f"{'radio' if radio else 'TV'} spots")
    return network, label, "network dictionary"


def mmm_label_for(mediaplan_label: str) -> str:
    return config.CHANNEL_MMM_OVERRIDES.get(mediaplan_label, mediaplan_label)


def placement_rows(seed_rows: list[dict], by_name: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Label rows from the placements file; placements found by name + site when the number
    is missing. Returns (rows, not found)."""
    out, missing, seen = [], [], set()
    for r in seed_rows:
        number = (r.get("placement_number") or "").strip()
        how = "20251112 placements file"
        if not number:
            number = by_name.get(norm(r.get("placement_name")) + "|" + norm(r.get("site")), "")
            how = "20251112 placements file (matched by placement name + site)"
        if not number:
            missing.append(r)
            continue
        if number in seen:
            continue
        seen.add(number)
        out.append({"key_type": "prisma_placement", "key": number, "name": r.get("site"),
                    "detail": r.get("placement_name"), "mediaplan_label": r.get("mediaplan_label"),
                    "mmm_label": r.get("mmm_label"), "source": "seed",
                    "reason": f"Seeded from {how}, labelled {r.get('labelled_on') or ''}".strip()})
    return out, missing


def network_rows(codes: list[dict], d: dict) -> tuple[list[dict], list[dict]]:
    """Label rows for TV codes; returns (rows, codes left for review with their note)."""
    out, left = [], []
    by_code = defaultdict(lambda: {"spend": 0.0, "media": set()})
    for c in codes:
        e = by_code[c["code"]]
        e["spend"] += float(c.get("spend") or 0)
        e["media"].add(c.get("media_name") or "")
    for code, e in by_code.items():
        labels = {network_label(code, m, d) for m in e["media"]}
        good = {lab for _, lab, _ in labels if lab}
        if len(good) == 1 and all(lab for _, lab, _ in labels):
            network, label, _ = next(iter(labels))
            out.append({"key_type": "tv_network", "key": code, "name": network,
                        "detail": ", ".join(sorted(m for m in e["media"] if m)),
                        "mediaplan_label": label, "mmm_label": mmm_label_for(label),
                        "source": "seed", "reason": "Seeded from nspack_network_dict.json"})
        else:
            note = "; ".join(sorted({n for _, lab, n in labels if not lab})) or \
                "different labels for different media"
            left.append({"code": code, "network": next(iter(labels))[0], "spend": e["spend"],
                         "media": ", ".join(sorted(m for m in e["media"] if m)), "note": note})
    return out, sorted(left, key=lambda x: -abs(x["spend"]))


def history_rows(rows: list[dict], who: str, when: datetime) -> list[dict]:
    return [{"change_id": uuid.uuid4().hex, "table_name": config.CHANNEL_LABEL_TABLE,
             "record_key": f"{r['key_type']}|{r['key']}", "field": f, "old_value": None,
             "new_value": r[f], "reason": r["reason"], "changed_by": who, "changed_at": when}
            for r in rows for f in LABEL_FIELDS if r.get(f)]


# =============================================================================
# SQL
# =============================================================================

def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _parent_sql(col: str) -> str:
    alts = "|".join(sorted(config.CHANNEL_PARENTS, key=len, reverse=True))
    return (f"COALESCE(REGEXP_EXTRACT({col}, r'^({alts})(?:_|$)'), "
            f"'{config.CHANNEL_NEEDS_LABEL}')")


def view_sql() -> str:
    labels = _core(config.CHANNEL_LABEL_TABLE)
    return f"""
CREATE OR REPLACE VIEW `{_core(config.MEDIA_DAILY_VIEW)}` AS
SELECT 'Prisma' AS media_source, p.snapshot_date, p.business_code, p.business_group,
       p.product_code, p.imdb_id, p.link, p.spend_date, p.spend_usd, p.spend_status,
       p.cost_basis, 'prisma_placement' AS label_key_type, p.placement_number AS label_key,
       p.site AS media_partner, p.placement_name AS media_detail, p.campaign_name,
       CAST(NULL AS INT64) AS spots,
       l.mediaplan_label, l.mmm_label, {_parent_sql('l.mmm_label')} AS channel_parent
FROM `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` p
LEFT JOIN `{labels}` l ON l.key_type = 'prisma_placement' AND l.key = p.placement_number
UNION ALL
SELECT t.source, t.snapshot_date, t.business_code, t.business_group, t.product_code, t.imdb_id,
       t.link, t.spend_date, t.spend_usd, t.spend_status, t.cost_basis,
       'tv_network', t.channel_code, COALESCE(l.name, t.channel_code), t.media_name,
       CAST(NULL AS STRING), t.spots,
       l.mediaplan_label, l.mmm_label, {_parent_sql('l.mmm_label')}
FROM `{_core(config.MEDIA_TV_CORE_TABLE)}` t
LEFT JOIN `{labels}` l ON l.key_type = 'tv_network' AND l.key = t.channel_code
"""


def coverage_sql() -> str:
    return f"""
SELECT media_source, business_code, channel_parent = '{config.CHANNEL_NEEDS_LABEL}' AS needs,
       COUNT(DISTINCT label_key) AS n_items, SUM(spend_usd) AS spend
FROM `{_core(config.MEDIA_DAILY_VIEW)}` GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""


# =============================================================================
# Google Cloud steps
# =============================================================================

def _read_inputs(client):
    raw = config.table_id(config.DATASET_RAW, "prisma_placements")
    by_name = {}
    sql = f"""
        SELECT TRIM(PlacementNumber) AS n, TRIM(PlacementName) AS name, TRIM(Site) AS site
        FROM `{raw}` WHERE _snapshot_date = {media_snapshot_expr('prisma_placements')}
          AND TRIM(AdvertiserCode) = 'ZUE' AND IFNULL(TRIM(PackageType), '') != 'Child'"""
    for r in client.query(sql).result():
        by_name.setdefault(norm(r.name) + "|" + norm(r.site), r.n)
    codes = [dict(r.items()) for r in client.query(f"""
        SELECT channel_code AS code, media_name, SUM(spend_usd) AS spend
        FROM `{_core(config.MEDIA_TV_CORE_TABLE)}` GROUP BY 1, 2""").result()]
    placements = {r.placement_number: (r.business_code, float(r.spend or 0))
                  for r in client.query(f"""
        SELECT placement_number, ANY_VALUE(business_code) AS business_code, SUM(spend_usd) AS spend
        FROM `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` GROUP BY 1""").result()}
    return by_name, codes, placements


def _money(x) -> str:
    return f"${float(x or 0):,.0f}"


def seed(dry_run: bool = True, client=None, seed_dir: Path = SEED_DIR) -> int:
    client = client or bq_client()
    table = _core(config.CHANNEL_LABEL_TABLE)
    try:
        existing = client.get_table(table).num_rows or 0
    except Exception:  # noqa: BLE001 - table does not exist yet
        existing = 0
    if existing:
        print(f"{config.DATASET_CORE}.{config.CHANNEL_LABEL_TABLE} already has {existing:,} rows. "
              "The seed runs only once; labels now change through the channel review.")
        print_coverage(client)
        return 0

    with open(seed_dir / PLACEMENT_FILE, newline="") as f:
        file_rows = list(csv.DictReader(f))
    d = {**json.loads((seed_dir / NETWORK_FILE).read_text()), **NETWORK_EXTRA}
    by_name, codes, placements = _read_inputs(client)
    p_rows, p_missing = placement_rows(file_rows, by_name)
    n_rows, n_left = network_rows(codes, d)

    labelled = {r["key"] for r in p_rows}
    need = defaultdict(lambda: [0, 0.0])
    have = defaultdict(lambda: [0, 0.0])
    for number, (biz, spend) in placements.items():
        e = have[biz] if number in labelled else need[biz]
        e[0] += 1
        e[1] += spend
    tv_have = sum(float(c.get("spend") or 0) for c in codes
                  if c["code"] in {r["key"] for r in n_rows})
    tv_left = sum(x["spend"] for x in n_left)

    print(f"CHANNEL LABEL SEED ({'check only, nothing is written' if dry_run else 'WRITE'})\n")
    print(f"Prisma placements from the file: {len(p_rows):,} labels "
          f"({sum('matched by' in r['reason'] for r in p_rows):,} found by placement name + site); "
          f"{len(p_missing):,} file rows not found in Prisma.")
    print("  Prisma spend in media_prisma_daily:")
    for biz in sorted(set(have) | set(need)):
        print(f"     {biz}: labelled {have[biz][0]:>6,} placements {_money(have[biz][1]):>15}   "
              f"needs a label {need[biz][0]:>6,} placements {_money(need[biz][1]):>15}")
    print(f"\nTV / audio / MVPD codes: {len(n_rows):,} labelled ({_money(tv_have)}); "
          f"{len(n_left):,} left for review ({_money(tv_left)}). Largest left for review:")
    for x in n_left[:25]:
        print(f"     {x['code'][:24]:<24} {str(x['network'] or '')[:24]:<24} {x['media'][:22]:<22} "
              f"{_money(x['spend']):>13}  {x['note']}")

    if dry_run:
        print("\nCheck only: nothing was written. To save, run again with --write.")
        return 0
    who, now = current_user(), datetime.now(timezone.utc)
    rows = [{**r, "updated_at": now, "updated_by": who} for r in p_rows + n_rows]
    T._write(client, config.CHANNEL_LABEL_TABLE, LABEL_SCHEMA, rows)
    hist = history_rows(rows, who, now)
    payload = [{**h, "changed_at": h["changed_at"].isoformat()} for h in hist]
    client.load_table_from_json(payload, _core(config.TITLE_HISTORY_TABLE),
                                job_config=bigquery.LoadJobConfig(
                                    schema=T.HISTORY_SCHEMA,
                                    write_disposition=bigquery.WriteDisposition.WRITE_APPEND)).result()
    client.query(view_sql()).result()
    print(f"\nWrote {config.DATASET_CORE}.{config.CHANNEL_LABEL_TABLE} ({len(rows):,} rows), "
          f"{len(hist):,} history rows, and view {config.DATASET_CORE}.{config.MEDIA_DAILY_VIEW}.")
    print_coverage(client)
    return len(rows)


def print_coverage(client) -> None:
    print("\nSpend by media source and client (from the view):")
    for r in client.query(coverage_sql()).result():
        state = "needs a channel" if r.needs else "labelled"
        print(f"     {r.media_source:<8} {r.business_code:<4} {state:<16} {r.n_items:>7,} items "
              f"{_money(r.spend):>16}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed channel labels and build the media_daily view.")
    ap.add_argument("--write", action="store_true", help="Create the label table and view")
    ap.add_argument("--view", action="store_true", help="Only refresh the media_daily view")
    args = ap.parse_args()
    if args.view:
        bq_client().query(view_sql()).result()
        print(f"Refreshed view {config.DATASET_CORE}.{config.MEDIA_DAILY_VIEW}.")
        return
    seed(dry_run=not args.write)


if __name__ == "__main__":
    main()
