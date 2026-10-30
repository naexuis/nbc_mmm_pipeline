"""
Load a US revenue delivery from GCS into BigQuery (raw layer).

Source : gs://<REVENUE_BUCKET>/hment_rev_usa/<YYYYMMDD>/*.csv   (exported by hand from NBC GCP)
Target : <project>.us_hment_mmm_raw.revenue_pos_usa             (one partition per delivery)

What it does
  1. Finds the delivery folder to load (newest by default).
  2. Checks the delivery before touching BigQuery:
       - every file has the same header, and the required columns are present
       - the delivery is not smaller than the previous one (each delivery is full history,
         so a smaller one usually means files are missing)
  3. Has BigQuery read the CSVs straight from GCS (nothing large is downloaded into Domino).
  4. Replaces that delivery's partition, checks row counts match, keeps the newest
     REVENUE_KEEP_DELIVERIES deliveries, and writes one row to the load registry.

Usage (Domino terminal or Domino Job):
    python -m us_hment_mmm.revenue.load_raw --dry-run          # checks only, changes nothing
    python -m us_hment_mmm.revenue.load_raw                    # load the newest delivery
    python -m us_hment_mmm.revenue.load_raw --delivery 20261008
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from us_hment_mmm import config, registry
from us_hment_mmm.display import styled
from us_hment_mmm.gcp import bq_client, gcs_client

SOURCE_NAME = "revenue_usa"
STAGE_TABLE = "_stage_revenue_pos_usa"
META_COLUMNS = ("_delivery_date", "_loaded_at")
HEADER_BYTES = 65536


# =============================================================================
# Pure helpers (no Google Cloud calls; covered by unit tests)
# =============================================================================

@dataclass
class SourceFile:
    name: str
    size: int
    generation: int


@dataclass
class Delivery:
    folder: str                       # YYYYMMDD
    files: list[SourceFile] = field(default_factory=list)  # .csv files only
    other_files: list[str] = field(default_factory=list)   # anything else in the folder

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def delivery_date(self) -> date:
        return datetime.strptime(self.folder, "%Y%m%d").date()

    def fingerprint(self) -> str:
        """Identity of the exact file set; changes if any file is added, removed, or replaced."""
        parts = sorted(f"{f.name}|{f.size}|{f.generation}" for f in self.files)
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def group_deliveries(prefix: str, blobs: list[SourceFile]) -> dict[str, Delivery]:
    """Group objects under prefix into dated delivery folders (YYYYMMDD/...)."""
    out: dict[str, Delivery] = {}
    for b in blobs:
        rest = b.name[len(prefix):]
        if "/" not in rest or b.name.endswith("/"):
            continue
        folder = rest.split("/", 1)[0]
        if not re.fullmatch(r"\d{8}", folder):
            continue
        d = out.setdefault(folder, Delivery(folder))
        if b.name.lower().endswith(".csv"):
            d.files.append(b)
        else:
            d.other_files.append(b.name)
    return out


def pick_delivery(deliveries: dict[str, Delivery], requested: str) -> tuple[Delivery, Delivery | None]:
    """Return (chosen delivery, the delivery just before it or None)."""
    if not deliveries:
        raise ValueError("No dated delivery folders found.")
    folders = sorted(deliveries)
    chosen = folders[-1] if requested == "latest" else requested
    if chosen not in deliveries:
        raise ValueError(f"Delivery {chosen} not found. Available: {', '.join(folders)}")
    idx = folders.index(chosen)
    previous = deliveries[folders[idx - 1]] if idx > 0 else None
    return deliveries[chosen], previous


def parse_header(raw: bytes) -> list[str]:
    """Column names from the first line of a CSV file (handles quoted names)."""
    text = raw.decode("utf-8-sig", errors="replace")
    first_line = text.splitlines()[0] if text else ""
    return next(csv.reader(io.StringIO(first_line)), [])


def sanitize_column(name: str) -> str:
    """Make a column name safe for BigQuery: letters, digits, underscores; not starting with digit."""
    clean = re.sub(r"[^0-9a-zA-Z_]", "_", name.strip())
    if not clean or clean[0].isdigit():
        clean = f"c_{clean}"
    return clean.lower()


def check_delivery(current: Delivery, previous: Delivery | None, headers: dict[str, list[str]],
                   required: tuple[str, ...], allow_smaller: bool = False
                   ) -> tuple[list[str], list[str], list[str]]:
    """
    Validate a delivery. Returns (columns, problems, warnings).
    Problems block the load; warnings are reported only.
    """
    problems: list[str] = []
    warnings: list[str] = []

    if not current.files:
        problems.append("No .csv files in the delivery folder.")
        return [], problems, warnings

    distinct = {tuple(h) for h in headers.values()}
    if len(distinct) > 1:
        problems.append(f"Files do not share one header ({len(distinct)} different headers).")
    raw_cols = headers[current.files[0].name] if current.files[0].name in headers else []
    columns = [sanitize_column(c) for c in raw_cols]

    dupes = sorted({c for c in columns if columns.count(c) > 1})
    if dupes:
        problems.append(f"Duplicate column names after cleaning: {', '.join(dupes)}")
    missing = [c for c in required if c not in columns]
    if missing:
        problems.append(f"Required columns missing: {', '.join(missing)}")

    if previous is not None:
        if current.total_bytes < previous.total_bytes:
            msg = (f"Delivery is smaller than the previous one ({current.total_bytes:,} bytes vs "
                   f"{previous.total_bytes:,} in {previous.folder}); files may be missing.")
            (warnings if allow_smaller else problems).append(msg)
        if len(current.files) < len(previous.files):
            warnings.append(f"Fewer files than previous delivery ({len(current.files)} vs "
                            f"{len(previous.files)}).")
    if current.other_files:
        warnings.append(f"{len(current.other_files)} non-CSV file(s) ignored.")

    return columns, problems, warnings


# =============================================================================
# Google Cloud steps
# =============================================================================

def _list_source_files() -> list[SourceFile]:
    blobs = gcs_client().list_blobs(config.REVENUE_BUCKET, prefix=config.REVENUE_US_PREFIX)
    return [SourceFile(b.name, b.size or 0, b.generation or 0) for b in blobs]


def _read_headers(delivery: Delivery) -> dict[str, list[str]]:
    bucket = gcs_client().bucket(config.REVENUE_BUCKET)
    return {
        f.name: parse_header(bucket.blob(f.name).download_as_bytes(start=0, end=HEADER_BYTES - 1))
        for f in delivery.files
    }


def _registry_exists(client: bigquery.Client) -> bool:
    try:
        client.get_table(registry.registry_table_id())
        return True
    except NotFound:
        return False


def _load_stage(client: bigquery.Client, delivery: Delivery, columns: list[str]) -> int:
    """BigQuery reads the CSVs directly from GCS into a temporary all-text stage table."""
    uri = f"gs://{config.REVENUE_BUCKET}/{config.REVENUE_US_PREFIX}{delivery.folder}/*.csv"
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        allow_quoted_newlines=True,
        encoding="UTF-8",
        max_bad_records=0,
        schema=[bigquery.SchemaField(c, "STRING") for c in columns],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    stage_id = config.table_id(config.DATASET_RAW, STAGE_TABLE)
    print(f"  Loading {uri}\n  -> {stage_id} (server-side; this can take several minutes)")
    job = client.load_table_from_uri(uri, stage_id, job_config=job_config)
    job.result()
    return int(job.output_rows)


def _publish(client: bigquery.Client, delivery: Delivery, columns: list[str]) -> int:
    """Replace this delivery's partition in the raw table from the stage table."""
    stage_id = config.table_id(config.DATASET_RAW, STAGE_TABLE)
    target_id = config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE)
    col_list = ", ".join(f"`{c}`" for c in columns)

    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{target_id}`
        PARTITION BY _delivery_date
        OPTIONS(description="US HE digital POS revenue, raw text columns, one partition per "
                            "delivery. Loaded by us_hment_mmm.revenue.load_raw.")
        AS SELECT DATE '2000-01-01' AS _delivery_date, CURRENT_TIMESTAMP() AS _loaded_at,
                  {col_list}
           FROM `{stage_id}` WHERE FALSE
    """).result()

    existing = [f.name for f in client.get_table(target_id).schema if f.name not in META_COLUMNS]
    if existing != columns:
        added = sorted(set(columns) - set(existing))
        removed = sorted(set(existing) - set(columns))
        raise RuntimeError(
            "Column layout differs from the existing raw table. "
            f"New: {added or 'none'}; missing: {removed or 'none'}. "
            "Review the source change before loading."
        )

    params = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("d", "DATE", delivery.delivery_date)])
    client.query(f"""
        BEGIN TRANSACTION;
        DELETE FROM `{target_id}` WHERE _delivery_date = @d;
        INSERT INTO `{target_id}` (_delivery_date, _loaded_at, {col_list})
        SELECT @d, CURRENT_TIMESTAMP(), {col_list} FROM `{stage_id}`;
        COMMIT TRANSACTION;
    """, job_config=params).result()

    rows = list(client.query(
        f"SELECT COUNT(*) AS n FROM `{target_id}` WHERE _delivery_date = @d",
        job_config=params).result())
    return int(rows[0].n)


def _apply_retention(client: bigquery.Client) -> None:
    target_id = config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE)
    client.query(f"""
        DELETE FROM `{target_id}`
        WHERE _delivery_date < (
            SELECT MIN(d) FROM (
                SELECT DISTINCT _delivery_date AS d FROM `{target_id}`
                ORDER BY d DESC LIMIT {int(config.REVENUE_KEEP_DELIVERIES)}))
    """).result()


def _summary(client: bigquery.Client) -> list[tuple]:
    target_id = config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE)
    sql = f"""
        SELECT _delivery_date, COUNT(*) AS rows_, MIN(transaction_date) AS first_txn,
               MAX(transaction_date) AS last_txn,
               ROUND(SUM(SAFE_CAST(royalty_total_usd AS FLOAT64)), 2) AS royalty_usd
        FROM `{target_id}` GROUP BY 1 ORDER BY 1
    """
    return [tuple(r.values()) for r in client.query(sql).result()]


def raw_table_summary():
    """Deliveries currently in the raw revenue table, as a DataFrame (for notebooks)."""
    import pandas as pd

    cols = ["delivery", "rows", "first_transaction", "last_transaction", "royalty_usd"]
    return pd.DataFrame(_summary(bq_client()), columns=cols)


def recent_loads(n: int = 5):
    """The last n revenue load attempts from the load registry, as a DataFrame."""
    import pandas as pd

    client = bq_client()
    if not _registry_exists(client):
        return pd.DataFrame()
    sql = f"""
        SELECT finished_at, delivery, status, rows_loaded, file_count,
               ROUND(total_bytes / 1e9, 2) AS size_gb, message
        FROM `{registry.registry_table_id()}`
        WHERE source = @source ORDER BY finished_at DESC LIMIT {int(n)}
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("source", "STRING", SOURCE_NAME)])
    return client.query(sql, job_config=cfg).to_dataframe()


def show_raw_table_summary():
    """Readable version of raw_table_summary() for notebooks."""
    return styled(raw_table_summary(), counts=["rows"], money=["royalty_usd"],
                  labels={"rows": "Rows", "royalty_usd": "Royalty (USD)", "delivery": "Delivery",
                          "first_transaction": "First sale", "last_transaction": "Last sale"})


def show_recent_loads(n: int = 5):
    """Readable version of recent_loads() for notebooks."""
    return styled(recent_loads(n), counts=["rows_loaded", "file_count"], times=["finished_at"],
                  decimals={"size_gb": 2},
                  labels={"finished_at": "Finished (Central)", "delivery": "Delivery",
                          "status": "Status", "rows_loaded": "Rows", "file_count": "Files",
                          "size_gb": "Size (GB)", "message": "Message"})


# =============================================================================
# Entry point
# =============================================================================

def run(delivery: str = "latest", dry_run: bool = True, force: bool = False,
        allow_smaller: bool = False, show_columns: bool = False) -> dict:
    """Run the revenue load. Defaults to a dry run so nothing changes by accident."""
    started = datetime.now(timezone.utc)
    run_id = f"{started:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    prefix = config.REVENUE_US_PREFIX

    print("=" * 78)
    print(f"REVENUE LOAD  ({'DRY RUN - nothing will change' if dry_run else 'LOAD'})")
    print("=" * 78)

    deliveries = group_deliveries(prefix, _list_source_files())
    print("Delivery folders found:")
    for folder in sorted(deliveries):
        d = deliveries[folder]
        print(f"  {folder}: {len(d.files):>4} csv files, {d.total_bytes / 1e9:7.2f} GB")

    chosen, previous = pick_delivery(deliveries, delivery)
    print(f"\nSelected delivery: {chosen.folder}"
          f"   (previous: {previous.folder if previous else 'none'})")

    print("Reading the header of every file (first 64 KB each)...")
    headers = _read_headers(chosen)
    columns, problems, warnings = check_delivery(
        chosen, previous, headers, config.REVENUE_REQUIRED_COLUMNS, allow_smaller)

    if problems or show_columns:
        print(f"\nColumns ({len(columns)}):")
        for i in range(0, len(columns), 4):
            print("  " + ", ".join(columns[i:i + 4]))
    else:
        print(f"Columns: {len(columns)}, all required columns present, same header in every file.")
    for w in warnings:
        print(f"WARNING: {w}")
    for p in problems:
        print(f"PROBLEM: {p}")

    client = bq_client()
    fingerprint = chosen.fingerprint()
    loaded_before = _registry_exists(client) and registry.already_loaded(
        client, SOURCE_NAME, fingerprint)
    if loaded_before:
        print("\nThis exact file set was already loaded successfully.")

    result = {"delivery": chosen.folder, "columns": columns, "problems": problems,
              "warnings": warnings, "already_loaded": loaded_before, "status": "CHECKED"}

    if dry_run:
        if problems:
            result["status"], verdict = "WOULD_BLOCK", "WOULD BE BLOCKED"
        elif loaded_before and not force:
            result["status"], verdict = "WOULD_SKIP", "WOULD SKIP (already loaded)"
        else:
            result["status"], verdict = "READY", "READY TO LOAD"
        print(f"\nDry run result: {verdict}")
        return result

    if loaded_before and not force:
        print("Skipping. Use --force to reload anyway.")
        result["status"] = "SKIPPED"
        return result

    registry.ensure_registry(client)
    base = dict(run_id=run_id, source=SOURCE_NAME, started_at=started, delivery=chosen.folder,
                gcs_prefix=f"gs://{config.REVENUE_BUCKET}/{prefix}{chosen.folder}/",
                file_count=len(chosen.files), total_bytes=chosen.total_bytes,
                fingerprint=fingerprint,
                target_table=config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE))

    if problems:
        registry.record(client, status="FAILED", message="; ".join(problems),
                        details={"warnings": warnings}, **base)
        print("\nLoad blocked by the problems above. Nothing was changed.")
        result["status"] = "BLOCKED"
        return result

    try:
        print("\nStep 1/3  Load files into the stage table")
        staged = _load_stage(client, chosen, columns)
        print(f"  Rows read from files: {staged:,}")

        print("Step 2/3  Replace this delivery in the raw table")
        published = _publish(client, chosen, columns)
        print(f"  Rows in raw table for {chosen.folder}: {published:,}")
        if published != staged:
            raise RuntimeError(f"Row count mismatch: staged {staged:,} vs published {published:,}")

        print(f"Step 3/3  Keep newest {config.REVENUE_KEEP_DELIVERIES} deliveries; tidy up")
        _apply_retention(client)
        client.delete_table(config.table_id(config.DATASET_RAW, STAGE_TABLE), not_found_ok=True)

        registry.record(client, status="SUCCESS", rows_loaded=published,
                        message="Loaded", details={"warnings": warnings}, **base)
    except Exception as exc:
        registry.record(client, status="FAILED", message=str(exc)[:1000], **base)
        raise

    print("\nRaw revenue table now holds:")
    print(f"  {'delivery':<12}{'rows':>15}  {'first txn':<12}{'last txn':<12}{'royalty USD':>18}")
    for d, n, first, last, roy in _summary(client):
        print(f"  {str(d):<12}{n:>15,}  {str(first):<12}{str(last):<12}{float(roy or 0):>18,.2f}")

    result.update(status="SUCCESS", rows_loaded=published)
    print("\nDone.")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Load a US revenue delivery into BigQuery.")
    ap.add_argument("--delivery", default="latest", help="YYYYMMDD folder, or 'latest'")
    ap.add_argument("--dry-run", action="store_true", help="Check only; change nothing")
    ap.add_argument("--force", action="store_true", help="Reload even if already loaded")
    ap.add_argument("--allow-smaller", action="store_true",
                    help="Allow a delivery smaller than the previous one")
    ap.add_argument("--show-columns", action="store_true", help="Print the full column list")
    args = ap.parse_args()
    result = run(delivery=args.delivery, dry_run=args.dry_run, force=args.force,
                 allow_smaller=args.allow_smaller, show_columns=args.show_columns)
    if result["status"] == "BLOCKED" or (args.dry_run and result["problems"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
