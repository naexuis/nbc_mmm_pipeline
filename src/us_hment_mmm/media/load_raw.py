"""
Load the newest complete media snapshot (Prisma + Mediaocean) into BigQuery (raw layer).

Source : gs://nbcuni-media-ocean/nbcuni-media-ocean/<folder>/*.txt.zip  (+ one manifest per delivery)
Target : <project>.us_hment_mmm_raw.<table>   one partition per snapshot date, e.g.
         prisma_campaigns, prisma_placements, prisma_advanced_placement_details,
         prisma_placement_buys, mediaocean_netpak, mediaocean_spotpak

How the snapshot is chosen
  Each folder writes a manifest per delivery listing its files. A date is COMPLETE when, for
  every required folder (nbc-zue, nbc-zff, nbc-zfe, nbc-netpak-spotpak), the manifest for that
  date lists every required file type and every listed file exists. The newest complete date
  is loaded; all tables come from that one date. (Each file is full history, so only the
  newest complete snapshot is needed.)

How each file is loaded
  BigQuery cannot read .zip, so each zip is downloaded to temporary disk in Domino, unzipped
  and converted from UTF-16 to UTF-8 gzip line by line (never held in memory), then loaded.
  The number of data lines in the file must equal the number of rows BigQuery loaded.

Usage:
    python -m us_hment_mmm.media.load_raw --dry-run     # choose snapshot + checks only
    python -m us_hment_mmm.media.load_raw               # load the newest complete snapshot
    python -m us_hment_mmm.media.load_raw --snapshot 20261030
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from us_hment_mmm import config, registry
from us_hment_mmm.display import table_or_note
from us_hment_mmm.gcp import bq_client, gcs_client

SNAPSHOT_SOURCE = "media_snapshot"
META_COLUMNS = ("_snapshot_date", "_source_folder", "_source_file", "_loaded_at")


# =============================================================================
# Pure helpers (no Google Cloud calls; covered by unit tests)
# =============================================================================

@dataclass
class MediaFile:
    folder: str
    name: str          # file name only, e.g. 463_PrismaCampaigns_3_0_20261030102042.txt.zip
    size: int
    generation: int


def required_types(folder: str) -> dict[str, str]:
    """File types (-> raw table) that a folder must deliver."""
    if folder == config.MEDIA_MEDIAOCEAN_FOLDER:
        return dict(config.MEDIA_MEDIAOCEAN_TYPES)
    return dict(config.MEDIA_PRISMA_TYPES)


def required_folders() -> list[str]:
    return [*config.MEDIA_PRISMA_FOLDERS, config.MEDIA_MEDIAOCEAN_FOLDER]


def file_type(name: str) -> str | None:
    """Match a file name to a known type. Longer type names are checked first, so
    'PrismaPlacementBuys' is not mistaken for 'PrismaPlacements'."""
    known = [*config.MEDIA_PRISMA_TYPES, *config.MEDIA_MEDIAOCEAN_TYPES]
    for t in sorted(known, key=len, reverse=True):
        if re.search(rf"_{t}_", name):
            return t
    return None


def is_manifest(name: str) -> bool:
    return "_Manifest_" in name and name.lower().endswith(".txt")


def name_date(name: str) -> str | None:
    """YYYYMMDD from the timestamp in a file name (14–17 digits)."""
    m = re.search(r"_(\d{14,17})\.txt", name)
    return m.group(1)[:8] if m else None


def parse_manifest(raw: bytes) -> list[str]:
    """File names listed in a manifest (UTF-16 or UTF-8, one name per line)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def folder_check(folder: str, listed: list[str] | None, present: set[str]) -> tuple[bool, str]:
    """Is a folder's delivery complete for one date? Returns (complete, reason)."""
    if listed is None:
        return False, "no manifest"
    missing_types = [t for t in required_types(folder)
                     if not any(file_type(n) == t for n in listed)]
    if missing_types:
        return False, f"manifest lacks {', '.join(missing_types)}"
    absent = [n for n in listed if file_type(n) in required_types(folder) and n not in present]
    if absent:
        return False, f"listed but not in bucket: {', '.join(absent)}"
    return True, "complete"


def choose_snapshot(status: dict[str, dict[str, tuple[bool, str]]], requested: str = "latest"
                    ) -> str | None:
    """
    status[date][folder] = (complete, reason). Return the newest date on which every
    required folder is complete (or the requested date, if it is complete).
    """
    folders = required_folders()
    complete = sorted(d for d, s in status.items()
                      if all(s.get(f, (False, ""))[0] for f in folders))
    if requested != "latest":
        return requested if requested in complete else None
    return complete[-1] if complete else None


def clean_column(name: str) -> str:
    """Keep the source spelling; only replace characters BigQuery does not allow."""
    clean = re.sub(r"[^0-9a-zA-Z_]", "_", name.strip())
    return f"c_{clean}" if not clean or clean[0].isdigit() else clean


def snapshot_fingerprint(files: list[MediaFile]) -> str:
    parts = sorted(f"{f.folder}/{f.name}|{f.size}|{f.generation}" for f in files)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def transcode_zip_to_gzip(zip_path: str, gz_path: str) -> tuple[list[str], int]:
    """
    Unzip the single text file inside zip_path, convert UTF-16 (or UTF-8) to UTF-8, and
    write it gzip-compressed to gz_path, line by line. Returns (header columns, data lines).
    Blank lines are dropped (BigQuery would skip them too) so the count stays comparable.
    Only "\\n" ends a row; a stray carriage return inside a value is replaced by a space so
    it cannot split a row.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise ValueError(f"Expected 1 file inside {os.path.basename(zip_path)}, "
                             f"found {len(members)}")
        with zf.open(members[0]) as raw:
            head = raw.peek(2)[:2] if hasattr(raw, "peek") else b""
        encoding = "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        header: list[str] = []
        rows = 0
        with zf.open(members[0]) as raw, \
                io.TextIOWrapper(raw, encoding=encoding, newline="\n") as src, \
                gzip.open(gz_path, "wt", encoding="utf-8", newline="", compresslevel=3) as dst:
            for i, line in enumerate(src):
                body = line.rstrip("\r\n").replace("\r", " ")
                if i == 0:
                    header = body.split("\t")
                elif not body.strip():
                    continue
                else:
                    rows += 1
                dst.write(body + "\n")
    return header, rows


# =============================================================================
# Google Cloud steps
# =============================================================================

def _list_folder(folder: str) -> list[MediaFile]:
    prefix = f"{config.MEDIA_ROOT_PREFIX}{folder}/"
    out = []
    for b in gcs_client().list_blobs(config.MEDIA_BUCKET, prefix=prefix):
        name = b.name[len(prefix):]
        if name and "/" not in name:
            out.append(MediaFile(folder, name, b.size or 0, b.generation or 0))
    return out


def _blob(folder: str, name: str):
    return gcs_client().bucket(config.MEDIA_BUCKET).blob(
        f"{config.MEDIA_ROOT_PREFIX}{folder}/{name}")


def _scan() -> tuple[dict, dict]:
    """
    Read every required folder and its manifests.
    Returns (status[date][folder] = (complete, reason), files[date][folder] = [MediaFile]).
    """
    status: dict[str, dict[str, tuple[bool, str]]] = {}
    chosen_files: dict[str, dict[str, list[MediaFile]]] = {}
    for folder in required_folders():
        files = _list_folder(folder)
        by_name = {f.name: f for f in files}
        manifests: dict[str, MediaFile] = {}
        for f in files:
            d = name_date(f.name)
            if is_manifest(f.name) and d and (d not in manifests or f.name > manifests[d].name):
                manifests[d] = f            # newest manifest of the day wins
        dates = {name_date(f.name) for f in files if name_date(f.name)}
        for d in dates:
            listed = None
            if d in manifests:
                listed = parse_manifest(_blob(folder, manifests[d].name).download_as_bytes())
            ok, reason = folder_check(folder, listed, set(by_name))
            status.setdefault(d, {})[folder] = (ok, reason)
            if ok:
                chosen_files.setdefault(d, {})[folder] = [
                    by_name[n] for n in listed if file_type(n) in required_types(folder)]
    return status, chosen_files


def _registry_exists(client: bigquery.Client) -> bool:
    try:
        client.get_table(registry.registry_table_id())
        return True
    except NotFound:
        return False


def _load_one_file(client: bigquery.Client, f: MediaFile, stage_id: str, workdir: str
                   ) -> tuple[list[str], int]:
    """Download, unzip + convert, load into a stage table. Returns (columns, rows)."""
    zip_path = os.path.join(workdir, f.name)
    gz_path = zip_path + ".utf8.gz"
    try:
        _blob(f.folder, f.name).download_to_filename(zip_path)
        header, line_rows = transcode_zip_to_gzip(zip_path, gz_path)
        os.remove(zip_path)
        columns = [clean_column(c) for c in header]
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            field_delimiter="\t",
            quote_character="",          # source is plain tab-separated text, no quoting
            skip_leading_rows=1,
            encoding="UTF-8",
            max_bad_records=0,
            schema=[bigquery.SchemaField(c, "STRING") for c in columns],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        with open(gz_path, "rb") as fh:
            job = client.load_table_from_file(fh, stage_id, job_config=job_config)
            job.result()
        loaded = int(job.output_rows)
        if loaded != line_rows:
            raise RuntimeError(f"{f.folder}/{f.name}: file has {line_rows:,} data lines but "
                               f"BigQuery loaded {loaded:,} rows")
        return columns, loaded
    finally:
        for p in (zip_path, gz_path):
            if os.path.exists(p):
                os.remove(p)


def _publish(client: bigquery.Client, table: str, columns: list[str], snap: date,
             stages: list[tuple[str, MediaFile]]) -> int:
    """Replace one snapshot date in a raw table from its stage tables (one per folder)."""
    target_id = config.table_id(config.DATASET_RAW, table)
    col_list = ", ".join(f"`{c}`" for c in columns)
    first_stage = stages[0][0]
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{target_id}`
        PARTITION BY _snapshot_date
        OPTIONS(description="Raw {table}: exact source text, one partition per snapshot date. "
                            "Loaded by us_hment_mmm.media.load_raw.")
        AS SELECT DATE '2000-01-01' AS _snapshot_date, '' AS _source_folder,
                  '' AS _source_file, CURRENT_TIMESTAMP() AS _loaded_at, {col_list}
           FROM `{first_stage}` WHERE FALSE
    """).result()

    existing = [f.name for f in client.get_table(target_id).schema if f.name not in META_COLUMNS]
    if existing != columns:
        added = sorted(set(columns) - set(existing))
        removed = sorted(set(existing) - set(columns))
        raise RuntimeError(f"{table}: column layout changed. New: {added or 'none'}; "
                           f"missing: {removed or 'none'}. Review before loading.")

    selects = "\nUNION ALL\n".join(
        f"SELECT @d, '{f.folder}', '{f.name}', CURRENT_TIMESTAMP(), {col_list} FROM `{sid}`"
        for sid, f in stages)
    params = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("d", "DATE", snap)])
    client.query(f"""
        BEGIN TRANSACTION;
        DELETE FROM `{target_id}` WHERE _snapshot_date = @d;
        INSERT INTO `{target_id}` (_snapshot_date, _source_folder, _source_file, _loaded_at,
                                   {col_list})
        {selects};
        COMMIT TRANSACTION;
    """, job_config=params).result()
    rows = list(client.query(
        f"SELECT COUNT(*) AS n FROM `{target_id}` WHERE _snapshot_date = @d",
        job_config=params).result())
    return int(rows[0].n)


def _apply_retention(client: bigquery.Client, table: str) -> None:
    target_id = config.table_id(config.DATASET_RAW, table)
    client.query(f"""
        DELETE FROM `{target_id}`
        WHERE _snapshot_date < (
            SELECT MIN(d) FROM (
                SELECT DISTINCT _snapshot_date AS d FROM `{target_id}`
                ORDER BY d DESC LIMIT {int(config.MEDIA_KEEP_SNAPSHOTS)}))
    """).result()


def raw_media_summary():
    """Rows per raw media table, snapshot date and folder, as a DataFrame."""
    import pandas as pd

    client = bq_client()
    parts = []
    for table in [*config.MEDIA_PRISMA_TYPES.values(), *config.MEDIA_MEDIAOCEAN_TYPES.values()]:
        tid = config.table_id(config.DATASET_RAW, table)
        try:
            client.get_table(tid)
        except NotFound:
            continue
        parts.append(f"SELECT '{table}' AS table_name, _snapshot_date, _source_folder, "
                     f"COUNT(*) AS rows_ FROM `{tid}` GROUP BY 2, 3")
    if not parts:
        return pd.DataFrame()
    sql = "\nUNION ALL\n".join(parts) + "\nORDER BY table_name, _snapshot_date DESC, _source_folder"
    return client.query(sql).to_dataframe()


def show_raw_media_summary():
    return table_or_note(raw_media_summary(),
                  "No media loaded yet. This is normal before the first load.", counts=["rows_"],
                  labels={"table_name": "Table", "_snapshot_date": "Snapshot",
                          "_source_folder": "Folder", "rows_": "Rows"})


# =============================================================================
# Entry point
# =============================================================================

def run(snapshot: str = "latest", dry_run: bool = True, force: bool = False) -> dict:
    """Load the newest complete media snapshot. Defaults to a dry run."""
    started = datetime.now(timezone.utc)
    run_id = f"{started:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    folders = required_folders()

    print("=" * 78)
    print(f"MEDIA LOAD  ({'DRY RUN - nothing will change' if dry_run else 'LOAD'})")
    print("=" * 78)
    print("Reading folders and manifests...")
    status, files = _scan()

    print("\nDelivery status by date (newest first):")
    print(f"  {'date':<10}  " + "  ".join(f"{f:<20}" for f in folders))
    for d in sorted(status, reverse=True)[:6]:
        cells = []
        for f in folders:
            ok, reason = status[d].get(f, (False, "nothing delivered"))
            cells.append(f"{'OK' if ok else 'MISSING':<20}")
        print(f"  {d:<10}  " + "  ".join(cells))

    chosen = choose_snapshot(status, snapshot)
    result = {"snapshot": chosen, "status": "CHECKED", "problems": [], "tables": {}}
    if chosen is None:
        why = []
        if snapshot != "latest" and snapshot in status:
            why = [f"{f}: {status[snapshot].get(f, (False, 'nothing delivered'))[1]}"
                   for f in folders if not status[snapshot].get(f, (False, ""))[0]]
        result["problems"] = why or ["No date has every required file from every folder."]
        result["status"] = "WOULD_BLOCK" if dry_run else "BLOCKED"
        for p in result["problems"]:
            print(f"PROBLEM: {p}")
        return result

    snap_files = [mf for f in folders for mf in files[chosen][f]]
    print(f"\nSelected snapshot: {chosen}   ({len(snap_files)} files)")
    for mf in snap_files:
        print(f"  {mf.folder:<20} {mf.name:<62} {mf.size / 1e6:8.1f} MB (zipped)")

    client = bq_client()
    fingerprint = snapshot_fingerprint(snap_files)
    loaded_before = _registry_exists(client) and registry.already_loaded(
        client, SNAPSHOT_SOURCE, fingerprint)
    result["already_loaded"] = loaded_before
    if loaded_before:
        print("\nThis exact snapshot was already loaded successfully.")

    if dry_run:
        if loaded_before and not force:
            result["status"] = "WOULD_SKIP"
            print("\nDry run result: WOULD SKIP (already loaded)")
        else:
            result["status"] = "READY"
            print("\nDry run result: READY TO LOAD")
        return result
    if loaded_before and not force:
        result["status"] = "SKIPPED"
        print("Skipping (already loaded).")
        return result

    registry.ensure_registry(client)
    snap_date = datetime.strptime(chosen, "%Y%m%d").date()
    all_types = {**config.MEDIA_PRISMA_TYPES, **config.MEDIA_MEDIAOCEAN_TYPES}
    total_rows = 0
    with tempfile.TemporaryDirectory(prefix="media_load_") as workdir:
        for ftype, table in all_types.items():
            type_files = [mf for mf in snap_files if file_type(mf.name) == ftype]
            t_started = datetime.now(timezone.utc)
            base = dict(run_id=run_id, source=table, started_at=t_started, delivery=chosen,
                        gcs_prefix=f"gs://{config.MEDIA_BUCKET}/{config.MEDIA_ROOT_PREFIX}",
                        file_count=len(type_files),
                        total_bytes=sum(mf.size for mf in type_files),
                        fingerprint=snapshot_fingerprint(type_files),
                        target_table=config.table_id(config.DATASET_RAW, table))
            stages: list[tuple[str, MediaFile]] = []
            try:
                print(f"\n{table}")
                columns: list[str] | None = None
                staged_rows = 0
                for i, mf in enumerate(type_files):
                    stage_id = config.table_id(config.DATASET_RAW, f"_stage_{table}_{i}")
                    cols, n = _load_one_file(client, mf, stage_id, workdir)
                    if columns is None:
                        columns = cols
                    elif cols != columns:
                        raise RuntimeError(f"{table}: {mf.folder} has a different header than "
                                           f"{type_files[0].folder}")
                    stages.append((stage_id, mf))
                    staged_rows += n
                    print(f"  {mf.folder:<20} {n:>12,} rows read and loaded (counts match)")
                published = _publish(client, table, columns, snap_date, stages)
                if published != staged_rows:
                    raise RuntimeError(f"{table}: staged {staged_rows:,} rows but "
                                       f"published {published:,}")
                _apply_retention(client, table)
                print(f"  -> {table}: {published:,} rows for {chosen}")
                registry.record(client, status="SUCCESS", rows_loaded=published,
                                message="Loaded", **base)
                result["tables"][table] = published
                total_rows += published
            except Exception as exc:
                registry.record(client, status="FAILED", message=str(exc)[:1000], **base)
                result["status"] = "FAILED"
                raise
            finally:
                for sid, _ in stages:
                    client.delete_table(sid, not_found_ok=True)

    registry.record(client, run_id=run_id, source=SNAPSHOT_SOURCE, status="SUCCESS",
                    started_at=started, delivery=chosen, file_count=len(snap_files),
                    total_bytes=sum(mf.size for mf in snap_files), fingerprint=fingerprint,
                    rows_loaded=total_rows, message="All media tables loaded")
    result.update(status="SUCCESS", rows_loaded=total_rows)
    print(f"\nDone. Snapshot {chosen} loaded: {total_rows:,} rows across "
          f"{len(result['tables'])} tables.")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Load the newest complete media snapshot.")
    ap.add_argument("--snapshot", default="latest", help="YYYYMMDD, or 'latest'")
    ap.add_argument("--dry-run", action="store_true", help="Check only; change nothing")
    ap.add_argument("--force", action="store_true", help="Reload even if already loaded")
    args = ap.parse_args()
    result = run(snapshot=args.snapshot, dry_run=args.dry_run, force=args.force)
    if result["status"] in ("BLOCKED", "WOULD_BLOCK"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
