"""
Title reference tables (core layer) and their one-time seeding from the old JSON dictionaries.

Tables in us_hment_mmm_core:
  title          one row per film, keyed on IMDb ID: canonical title, distributor, theatrical
                 release, PVOD (PEW) and EST dates, and the In MMM flag with its reason.
  title_alias    maps source keys to films:
                   - media_product_code : (business group, 3-letter product code) -> film or non-film
                   - revenue_product_id : NBC product_id1 -> film
  title_history  every value ever set: table, key, field, old -> new, reason, who, when.

These tables are never overwritten by data loads. After seeding, every change goes through
the review notebook, which writes title_history at the same time.

Seeding (run once):
    python -m us_hment_mmm.reference.titles --seed --dry-run     # report only
    python -m us_hment_mmm.reference.titles --seed               # create and fill the tables
"""

from __future__ import annotations

import argparse
import getpass
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client

SEED_DIR = Path(__file__).resolve().parents[3] / "reference_seed"
PLACEHOLDER_IMDB = "tt00000000"
EMPTY_VALUES = {None, "", "XXXX", "1900-01-01", "UNKNOWN", "None"}

TITLE_SCHEMA = [
    bigquery.SchemaField("imdb_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("title_name", "STRING"),
    bigquery.SchemaField("distributor", "STRING"),
    bigquery.SchemaField("release_date", "DATE"),
    bigquery.SchemaField("pvod_date", "DATE"),
    bigquery.SchemaField("est_date", "DATE"),
    bigquery.SchemaField("nbc_genre_1", "STRING"),   # NBC master genre (from enrichment, reviewed)
    bigquery.SchemaField("nbc_genre_2", "STRING"),
    bigquery.SchemaField("imdb_genres", "STRING"),   # IMDb genres, sorted, comma separated
    bigquery.SchemaField("imdb_genres_source", "STRING"),  # where imdb_genres came from
    bigquery.SchemaField("in_mmm", "BOOL"),
    bigquery.SchemaField("in_mmm_reason", "STRING"),
    bigquery.SchemaField("review_note", "STRING"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_by", "STRING"),
]
ALIAS_SCHEMA = [
    bigquery.SchemaField("alias_type", "STRING", mode="REQUIRED"),   # media_product_code | revenue_product_id
    bigquery.SchemaField("business_group", "STRING"),                # UP_HE | FOCUS (codes only)
    bigquery.SchemaField("alias_value", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("category", "STRING", mode="REQUIRED"),     # film | non_film
    bigquery.SchemaField("imdb_id", "STRING"),
    bigquery.SchemaField("label", "STRING"),
    bigquery.SchemaField("reason", "STRING"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_by", "STRING"),
]
HISTORY_SCHEMA = [
    bigquery.SchemaField("change_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("table_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("record_key", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("field", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("old_value", "STRING"),
    bigquery.SchemaField("new_value", "STRING"),
    bigquery.SchemaField("reason", "STRING"),
    bigquery.SchemaField("changed_by", "STRING"),
    bigquery.SchemaField("changed_at", "TIMESTAMP"),
]
TITLE_FIELDS = ["title_name", "distributor", "release_date", "pvod_date", "est_date",
                "in_mmm", "in_mmm_reason", "review_note"]


# =============================================================================
# Pure builders (unit tested)
# =============================================================================

def clean(v):
    """Turn the dictionaries' placeholder values into None."""
    return None if v in EMPTY_VALUES else v


def build_titles(title_map: dict, overrides: dict) -> tuple[list[dict], list[str]]:
    """
    One row per real film (IMDb ID) from title_mapping_data_dict, plus the seed overrides.
    Returns (titles, notes). Where several names share an IMDb ID, the most complete entry
    wins (names starting with 'UNTITLED' lose) and a review note is recorded.
    """
    by_imdb: dict[str, list[tuple[str, dict]]] = {}
    for name, v in title_map.items():
        imdb = v.get("imdb_id")
        if imdb and imdb != PLACEHOLDER_IMDB:
            by_imdb.setdefault(imdb, []).append((name, v))

    titles, notes = [], []
    for imdb, entries in sorted(by_imdb.items()):
        def score(e):
            name, v = e
            filled = sum(clean(v.get(k)) is not None for k in
                         ("release_date", "pew_date", "est_date", "distributor", "product_id"))
            return (not name.startswith("UNTITLED"), filled, len(name))
        entries.sort(key=score, reverse=True)
        name, v = entries[0]
        row = {
            "imdb_id": imdb,
            "title_name": name,
            "distributor": clean(v.get("distributor")),
            "release_date": clean(v.get("release_date")),
            "pvod_date": clean(v.get("pew_date")),
            "est_date": clean(v.get("est_date")),
            "in_mmm": None,
            "in_mmm_reason": None,
            "review_note": None,
        }
        if len(entries) > 1:
            others = ", ".join(n for n, _ in entries[1:])
            row["review_note"] = f"IMDb ID also used for: {others}. Check which name is right."
            notes.append(f"{imdb}: kept '{name}', also listed as {others}")
        titles.append(row)

    index = {t["imdb_id"]: t for t in titles}
    for field in ("title_name", "pvod_date", "est_date", "release_date", "in_mmm"):
        for imdb, o in overrides.get(field, {}).items():
            if imdb not in index:
                notes.append(f"override for unknown IMDb ID {imdb} ({field}) ignored")
                continue
            index[imdb][field] = o["value"]
            if field == "in_mmm":
                index[imdb]["in_mmm_reason"] = o["reason"]
    return titles, notes


def resolve_code(code: str, dds: dict, exceptions: dict, title_map: dict) -> dict:
    """Follow the old chain: product code -> name -> corrected name -> title entry."""
    name = dds.get(code)
    if name is None:
        return {"status": "unmapped", "label": None, "imdb_id": None,
                "why": "code not in dds_inv_dictionary"}
    fixed = exceptions.get(name, name)
    entry = title_map.get(fixed)
    if entry is None:
        return {"status": "unmapped", "label": fixed, "imdb_id": None,
                "why": f"name '{fixed}' not in title_mapping_data_dict"}
    if entry.get("imdb_id") in (None, PLACEHOLDER_IMDB):
        return {"status": "non_film", "label": fixed, "imdb_id": None, "why": ""}
    return {"status": "film", "label": fixed, "imdb_id": entry["imdb_id"], "why": ""}


def build_code_aliases(observed: list[tuple[str, str, str]], dds: dict, exceptions: dict,
                       title_map: dict) -> tuple[list[dict], list[dict]]:
    """
    observed = [(business_group, product_code, sample_name)] seen in the media data.
    Returns (alias rows, unmapped list). Only codes actually used by US HE businesses are kept.
    """
    aliases, unmapped = [], []
    for group, code, sample in sorted(set(observed)):
        r = resolve_code(code, dds, exceptions, title_map)
        if r["status"] == "unmapped":
            unmapped.append({"business_group": group, "product_code": code,
                             "sample_name": sample, "why": r["why"]})
            continue
        aliases.append({"alias_type": "media_product_code", "business_group": group,
                        "alias_value": code, "category": r["status"], "imdb_id": r["imdb_id"],
                        "label": r["label"], "reason": "Seeded from dds_inv_dictionary.json"})
    return aliases, unmapped


def build_product_id_aliases(title_map: dict, known_imdb: set[str]) -> list[dict]:
    """Revenue product_id1 -> film, from the product_id field of title_mapping_data_dict."""
    rows, seen = [], set()
    for name, v in sorted(title_map.items()):
        pid, imdb = clean(v.get("product_id")), v.get("imdb_id")
        if pid and imdb in known_imdb and pid not in seen:
            seen.add(pid)
            rows.append({"alias_type": "revenue_product_id", "business_group": None,
                         "alias_value": str(pid), "category": "film", "imdb_id": imdb,
                         "label": name, "reason": "Seeded from title_mapping_data_dict.json"})
    return rows


def history_rows(table: str, rows: list[dict], key_fields: list[str], fields: list[str],
                 reason_for, who: str, when: datetime) -> list[dict]:
    """One history row per non-empty value (old value empty, since this is the seed)."""
    out = []
    for r in rows:
        key = "|".join(str(r.get(k) or "") for k in key_fields)
        for f in fields:
            if r.get(f) is not None:
                out.append({"change_id": uuid.uuid4().hex, "table_name": table, "record_key": key,
                            "field": f, "old_value": None, "new_value": str(r[f]),
                            "reason": reason_for(r, f), "changed_by": who, "changed_at": when})
    return out


# =============================================================================
# Google Cloud steps
# =============================================================================

def _tid(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _observed_media_codes(client: bigquery.Client) -> list[tuple[str, str, str]]:
    """(business group, product code, sample product name) from the newest raw snapshots."""
    groups = config.MEDIA_BUSINESS_GROUPS
    codes = ", ".join(f"'{c}'" for c in groups)
    raw = lambda t: config.table_id(config.DATASET_RAW, t)  # noqa: E731
    sql = f"""
        SELECT AdvertiserCode AS biz, ProductCode AS code, ANY_VALUE(ProductName) AS name
        FROM `{raw('prisma_placements')}`
        WHERE _snapshot_date = (SELECT MAX(_snapshot_date) FROM `{raw('prisma_placements')}`)
          AND AdvertiserCode IN ({codes}) AND ProductCode IS NOT NULL
        GROUP BY 1, 2
        UNION DISTINCT
        SELECT Client_Code, Product_Code, ANY_VALUE(Product_Name)
        FROM `{raw('mediaocean_netpak')}`
        WHERE _snapshot_date = (SELECT MAX(_snapshot_date) FROM `{raw('mediaocean_netpak')}`)
          AND Client_Code IN ({codes}) AND Product_Code IS NOT NULL
        GROUP BY 1, 2
        UNION DISTINCT
        SELECT Client_Code, Product_Code, ANY_VALUE(Product_Name)
        FROM `{raw('mediaocean_spotpak')}`
        WHERE _snapshot_date = (SELECT MAX(_snapshot_date) FROM `{raw('mediaocean_spotpak')}`)
          AND Client_Code IN ({codes}) AND Product_Code IS NOT NULL
        GROUP BY 1, 2
    """
    best: dict[tuple[str, str], str] = {}
    for r in client.query(sql).result():
        biz, code = (r.biz or "").strip(), (r.code or "").strip()
        if biz in groups and code:
            best.setdefault((groups[biz], code), r.name)
    return [(g, c, n) for (g, c), n in best.items()]


def _table_has_rows(client: bigquery.Client, name: str) -> bool:
    try:
        t = client.get_table(_tid(name))
    except NotFound:
        return False
    return (t.num_rows or 0) > 0


def _write(client: bigquery.Client, name: str, schema, rows: list[dict]) -> None:
    """Replace the table with these rows (JSON load job, explicit schema, row count checked)."""
    names = [f.name for f in schema]
    payload = [{k: (r.get(k).isoformat() if isinstance(r.get(k), datetime) else r.get(k))
                for k in names} for r in rows]
    job = client.load_table_from_json(payload, _tid(name), job_config=bigquery.LoadJobConfig(
        schema=schema, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE))
    job.result()
    loaded = client.get_table(_tid(name)).num_rows
    if loaded != len(rows):
        raise RuntimeError(f"{name}: expected {len(rows):,} rows but the table has {loaded:,}")


def seed(dry_run: bool = True, force: bool = False, seed_dir: Path = SEED_DIR) -> dict:
    """Create and fill title, title_alias and title_history from the JSON dictionaries."""
    load = lambda f: json.loads((seed_dir / f).read_text())  # noqa: E731
    title_map, dds = load("title_mapping_data_dict.json"), load("dds_inv_dictionary.json")
    exceptions, overrides = load("exception_dict.json"), load("seed_overrides.json")

    print("=" * 78)
    print(f"TITLE SEED  ({'DRY RUN - nothing will change' if dry_run else 'WRITE'})")
    print("=" * 78)
    titles, notes = build_titles(title_map, overrides)
    known = {t["imdb_id"] for t in titles}
    client = bq_client()
    observed = _observed_media_codes(client)
    code_aliases, unmapped = build_code_aliases(observed, dds, exceptions, title_map)
    pid_aliases = build_product_id_aliases(title_map, known)
    # A code pointing at a film that has no title row (e.g. a drafting campaign of a
    # non-film entry) is still a valid alias; only report it.
    orphan = [a for a in code_aliases if a["imdb_id"] and a["imdb_id"] not in known]

    in_true = sum(1 for t in titles if t["in_mmm"] is True)
    in_false = sum(1 for t in titles if t["in_mmm"] is False)
    print(f"Films (title rows):                 {len(titles):,}")
    print(f"  In MMM = yes / no / not reviewed: {in_true} / {in_false} / "
          f"{len(titles) - in_true - in_false}")
    print(f"  missing PVOD date:                {sum(t['pvod_date'] is None for t in titles):,}")
    print(f"Product codes seen in US HE media:  {len(observed):,}")
    print(f"  mapped to a film:                 {sum(a['category'] == 'film' for a in code_aliases):,}")
    print(f"  mapped as non-film campaign:      {sum(a['category'] == 'non_film' for a in code_aliases):,}")
    print(f"  NOT mapped (go to review):        {len(unmapped):,}")
    print(f"Revenue product IDs mapped:         {len(pid_aliases):,}")
    if notes:
        print("\nNotes for review:")
        for n in notes:
            print(f"  - {n}")
    if orphan:
        print(f"\n{len(orphan)} code(s) point to an IMDb ID with no title row: "
              + ", ".join(f"{a['business_group']}/{a['alias_value']}" for a in orphan[:10]))
    if unmapped:
        print("\nProduct codes not mapped yet (first 25):")
        for u in unmapped[:25]:
            print(f"  {u['business_group']:<6} {u['product_code']:<6} {str(u['sample_name'])[:40]:<40} "
                  f"({u['why']})")

    result = {"titles": len(titles), "code_aliases": len(code_aliases),
              "product_id_aliases": len(pid_aliases), "unmapped_codes": unmapped, "notes": notes}
    already = _table_has_rows(client, config.TITLE_TABLE)
    if already and not force:
        print("\nThe title table already has rows. Seeding is a one-time step and was skipped.")
        result["status"] = "ALREADY_SEEDED"
        return result
    if dry_run:
        print("\nDry run result: READY TO SEED")
        result["status"] = "READY"
        return result

    who = getpass.getuser()
    now = datetime.now(timezone.utc)
    for t in titles:
        t.update(updated_at=now, updated_by=who)
    aliases = code_aliases + pid_aliases
    for a in aliases:
        a.update(updated_at=now, updated_by=who)

    def title_reason(r, f):
        for field in ("title_name", "pvod_date", "est_date", "release_date"):
            if f == field and r["imdb_id"] in overrides.get(field, {}):
                return overrides[field][r["imdb_id"]]["reason"]
        if f in ("in_mmm", "in_mmm_reason"):
            return r.get("in_mmm_reason")
        return "Seeded from title_mapping_data_dict.json"

    history = (history_rows(config.TITLE_TABLE, titles, ["imdb_id"], TITLE_FIELDS,
                            title_reason, who, now)
               + history_rows(config.TITLE_ALIAS_TABLE, aliases,
                              ["alias_type", "business_group", "alias_value"],
                              ["category", "imdb_id", "label"], lambda r, f: r["reason"], who, now))
    _write(client, config.TITLE_TABLE, TITLE_SCHEMA, titles)
    _write(client, config.TITLE_ALIAS_TABLE, ALIAS_SCHEMA, aliases)
    _write(client, config.TITLE_HISTORY_TABLE, HISTORY_SCHEMA, history)
    print(f"\nWrote {len(titles):,} titles, {len(aliases):,} aliases, "
          f"{len(history):,} history rows to {config.DATASET_CORE}.")
    result["status"] = "SEEDED"
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Title reference tables.")
    ap.add_argument("--seed", action="store_true", help="Seed the tables from reference_seed/")
    ap.add_argument("--dry-run", action="store_true", help="Report only; change nothing")
    ap.add_argument("--force", action="store_true", help="Re-seed even if tables have rows")
    args = ap.parse_args()
    if not args.seed:
        ap.error("nothing to do; use --seed")
    seed(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
