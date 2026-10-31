"""
Load the newest copy of each reference CSV (copied by hand from NBC GCP) into BigQuery.

Sources (see config.REFERENCE_SOURCES):
  enrichment_title : gs://nbcu-marcus-hilliard/enrichments_table_stage/<YYYYMMDD>_enrichments_title.csv
                     -> us_hment_mmm_raw.nbc_enrichment_title
  std_chtt         : gs://nbcu-marcus-hilliard/std_chtt_table_stage/he_ent_std_chtt_<YYYYMMDD>.csv
                     -> us_hment_mmm_raw.finance_std_chtt

For each source: pick the newest dated file, check its header has the required columns and
that it is not much smaller than the previous copy, have BigQuery read it straight from GCS,
store it as one partition per copy date, keep the newest N copies, and log the run.

Usage:
    python -m us_hment_mmm.reference.load_csv --dry-run              # all sources, check only
    python -m us_hment_mmm.reference.load_csv                        # load all sources
    python -m us_hment_mmm.reference.load_csv --source std_chtt
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from us_hment_mmm import config, registry
from us_hment_mmm.display import table_or_note
from us_hment_mmm.gcp import bq_client, gcs_client

META_COLUMNS = ("_delivery_date", "_source_file", "_loaded_at")
SMALLER_LIMIT = 0.9          # a copy under 90% of the previous copy's size is blocked


# =============================================================================
# Pure helpers (unit tested)
# =============================================================================

@dataclass
class RefFile:
    name: str
    date: str          # YYYYMMDD
    size: int
    generation: int


def dated_files(names_sizes: list[tuple[str, int, int]], pattern: str) -> list[RefFile]:
    """Keep only files matching the source's pattern, sorted oldest -> newest."""
    out = []
    for name, size, gen in names_sizes:
        m = re.match(pattern, name)
        if m:
            out.append(RefFile(name, m.group(1), size, gen))
    return sorted(out, key=lambda f: (f.date, f.name))


def pick(files: list[RefFile], requested: str = "latest") -> tuple[RefFile | None, RefFile | None]:
    """(chosen, previous). chosen is None when nothing matches."""
    if not files:
        return None, None
    if requested == "latest":
        idx = len(files) - 1
    else:
        matches = [i for i, f in enumerate(files) if f.date == requested]
        if not matches:
            return None, None
        idx = matches[-1]
    return files[idx], (files[idx - 1] if idx > 0 else None)


def parse_header(raw: bytes) -> list[str]:
    text = raw.decode("utf-8-sig", errors="replace")
    first = text.splitlines()[0] if text else ""
    return [c.strip() for c in next(csv.reader(io.StringIO(first)), [])]


def clean_column(name: str) -> str:
    clean = re.sub(r"[^0-9a-zA-Z_]", "_", name.strip()).lower()
    return f"c_{clean}" if not clean or clean[0].isdigit() else clean


def check(header: list[str], required: tuple[str, ...], chosen: RefFile,
          previous: RefFile | None, allow_smaller: bool = False
          ) -> tuple[list[str], list[str], list[str]]:
    """Returns (columns, problems, warnings)."""
    problems, warnings = [], []
    columns = [clean_column(c) for c in header]
    if not columns:
        problems.append("File has no header row.")
    dupes = sorted({c for c in columns if columns.count(c) > 1})
    if dupes:
        problems.append(f"Duplicate column names: {', '.join(dupes)}")
    missing = [c for c in required if c not in columns]
    if missing:
        problems.append(f"Required columns missing: {', '.join(missing)}")
    if previous is not None and chosen.size < SMALLER_LIMIT * previous.size:
        msg = (f"File is much smaller than the previous copy ({chosen.size:,} vs "
               f"{previous.size:,} bytes in {previous.name}); it may be incomplete.")
        (warnings if allow_smaller else problems).append(msg)
    return columns, problems, warnings


# =============================================================================
# Google Cloud steps
# =============================================================================

def _registry_exists(client: bigquery.Client) -> bool:
    try:
        client.get_table(registry.registry_table_id())
        return True
    except NotFound:
        return False


def _load(client: bigquery.Client, src: dict, f: RefFile, columns: list[str]) -> int:
    target_id = config.table_id(config.DATASET_RAW, src["table"])
    stage_id = config.table_id(config.DATASET_RAW, f"_stage_{src['table']}")
    uri = f"gs://{src['bucket']}/{src['prefix']}{f.name}"
    job = client.load_table_from_uri(uri, stage_id, job_config=bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV, skip_leading_rows=1,
        allow_quoted_newlines=True, encoding="UTF-8", max_bad_records=0,
        schema=[bigquery.SchemaField(c, "STRING") for c in columns],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE))
    job.result()
    staged = int(job.output_rows)

    col_list = ", ".join(f"`{c}`" for c in columns)
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{target_id}`
        PARTITION BY _delivery_date
        OPTIONS(description="{src['description']}. Raw text columns, one partition per copy. "
                            "Loaded by us_hment_mmm.reference.load_csv.")
        AS SELECT DATE '2000-01-01' AS _delivery_date, '' AS _source_file,
                  CURRENT_TIMESTAMP() AS _loaded_at, {col_list}
           FROM `{stage_id}` WHERE FALSE
    """).result()
    existing = [s.name for s in client.get_table(target_id).schema if s.name not in META_COLUMNS]
    if existing != columns:
        added = sorted(set(columns) - set(existing))
        removed = sorted(set(existing) - set(columns))
        raise RuntimeError(f"{src['table']}: column layout changed. New: {added or 'none'}; "
                           f"missing: {removed or 'none'}. Review before loading.")

    d = datetime.strptime(f.date, "%Y%m%d").date()
    params = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("d", "DATE", d),
        bigquery.ScalarQueryParameter("f", "STRING", f.name)])
    client.query(f"""
        BEGIN TRANSACTION;
        DELETE FROM `{target_id}` WHERE _delivery_date = @d;
        INSERT INTO `{target_id}` (_delivery_date, _source_file, _loaded_at, {col_list})
        SELECT @d, @f, CURRENT_TIMESTAMP(), {col_list} FROM `{stage_id}`;
        COMMIT TRANSACTION;
    """, job_config=params).result()
    published = int(list(client.query(
        f"SELECT COUNT(*) AS n FROM `{target_id}` WHERE _delivery_date = @d",
        job_config=params).result())[0].n)
    if published != staged:
        raise RuntimeError(f"Row count mismatch: file {staged:,} vs table {published:,}")

    client.query(f"""
        DELETE FROM `{target_id}`
        WHERE _delivery_date < (
            SELECT MIN(x) FROM (SELECT DISTINCT _delivery_date AS x FROM `{target_id}`
                                ORDER BY x DESC LIMIT {int(src['keep'])}))
    """).result()
    client.delete_table(stage_id, not_found_ok=True)
    return published


def run_source(name: str, dry_run: bool = True, delivery: str = "latest", force: bool = False,
               allow_smaller: bool = False) -> dict:
    src = config.REFERENCE_SOURCES[name]
    started = datetime.now(timezone.utc)
    print(f"\n--- {name}  ({src['description']})")
    blobs = gcs_client().list_blobs(src["bucket"], prefix=src["prefix"])
    files = dated_files([(b.name[len(src["prefix"]):], b.size or 0, b.generation or 0)
                         for b in blobs], src["pattern"])
    for f in files:
        print(f"  found {f.name:<45} {f.size / 1e6:7.2f} MB")
    chosen, previous = pick(files, delivery)
    result = {"source": name, "file": chosen.name if chosen else None, "problems": [],
              "warnings": [], "status": "CHECKED"}
    if chosen is None:
        result["problems"] = [f"No file for delivery '{delivery}' in gs://{src['bucket']}/"
                              f"{src['prefix']}"]
        result["status"] = "WOULD_BLOCK" if dry_run else "BLOCKED"
        print(f"  PROBLEM: {result['problems'][0]}")
        return result

    raw = gcs_client().bucket(src["bucket"]).blob(src["prefix"] + chosen.name).download_as_bytes(
        start=0, end=65535)
    columns, problems, warnings = check(parse_header(raw), src["required"], chosen, previous,
                                        allow_smaller)
    result.update(problems=problems, warnings=warnings)
    print(f"  selected {chosen.name}  ({len(columns)} columns)")
    for w in warnings:
        print(f"  WARNING: {w}")
    for p in problems:
        print(f"  PROBLEM: {p}")

    client = bq_client()
    fingerprint = f"{chosen.name}|{chosen.size}|{chosen.generation}"
    loaded = _registry_exists(client) and registry.already_loaded(client, name, fingerprint)
    if dry_run:
        result["status"] = ("WOULD_BLOCK" if problems else
                            "WOULD_SKIP" if loaded and not force else "READY")
        print(f"  dry run: {result['status']}")
        return result
    if loaded and not force:
        result["status"] = "SKIPPED"
        print("  already loaded; skipped")
        return result

    registry.ensure_registry(client)
    base = dict(run_id=f"{started:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}", source=name,
                started_at=started, delivery=chosen.date,
                gcs_prefix=f"gs://{src['bucket']}/{src['prefix']}", file_count=1,
                total_bytes=chosen.size, fingerprint=fingerprint,
                target_table=config.table_id(config.DATASET_RAW, src["table"]))
    if problems:
        registry.record(client, status="FAILED", message="; ".join(problems), **base)
        result["status"] = "BLOCKED"
        print("  blocked; nothing changed")
        return result
    try:
        rows = _load(client, src, chosen, columns)
    except Exception as exc:
        registry.record(client, status="FAILED", message=str(exc)[:1000], **base)
        raise
    registry.record(client, status="SUCCESS", rows_loaded=rows, message="Loaded", **base)
    result.update(status="SUCCESS", rows_loaded=rows)
    print(f"  loaded {rows:,} rows (counts match) -> {src['table']}")
    return result


def run(source: str = "all", dry_run: bool = True, force: bool = False,
        allow_smaller: bool = False) -> list[dict]:
    """Load one or all reference sources. Defaults to a dry run."""
    print("=" * 78)
    print(f"REFERENCE LOAD  ({'DRY RUN - nothing will change' if dry_run else 'LOAD'})")
    print("=" * 78)
    names = list(config.REFERENCE_SOURCES) if source == "all" else [source]
    return [run_source(n, dry_run=dry_run, force=force, allow_smaller=allow_smaller)
            for n in names]


def reference_summary():
    """Copies currently held in each raw reference table, as a DataFrame."""
    import pandas as pd

    client = bq_client()
    parts = []
    for name, src in config.REFERENCE_SOURCES.items():
        tid = config.table_id(config.DATASET_RAW, src["table"])
        try:
            client.get_table(tid)
        except NotFound:
            continue
        parts.append(f"SELECT '{name}' AS source, _delivery_date, ANY_VALUE(_source_file) AS file, "
                     f"COUNT(*) AS rows_ FROM `{tid}` GROUP BY 2")
    if not parts:
        return pd.DataFrame()
    return client.query(" UNION ALL ".join(parts) + " ORDER BY source, _delivery_date DESC"
                        ).to_dataframe()


def show_reference_summary():
    return table_or_note(reference_summary(),
                  "No reference files loaded yet. This is normal before the first load.",
                  counts=["rows_"],
                  labels={"source": "Source", "_delivery_date": "Copy date", "file": "File",
                          "rows_": "Rows"})


def main() -> None:
    ap = argparse.ArgumentParser(description="Load reference CSVs (enrichment, Standard CHTT).")
    ap.add_argument("--source", default="all", choices=["all", *config.REFERENCE_SOURCES])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--allow-smaller", action="store_true")
    args = ap.parse_args()
    results = run(args.source, dry_run=args.dry_run, force=args.force,
                  allow_smaller=args.allow_smaller)
    if any(r["status"] in ("BLOCKED", "WOULD_BLOCK") for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
