"""
Load registry: one row per load attempt, so every table can be traced back to
the exact files it came from.

Table: <project>.us_hment_mmm_raw.load_registry
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from google.cloud import bigquery

from us_hment_mmm import config

REGISTRY_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED"),      # e.g. revenue_usa
    bigquery.SchemaField("delivery", "STRING"),                     # e.g. 20261008
    bigquery.SchemaField("gcs_prefix", "STRING"),
    bigquery.SchemaField("file_count", "INT64"),
    bigquery.SchemaField("total_bytes", "INT64"),
    bigquery.SchemaField("fingerprint", "STRING"),                  # identity of the file set
    bigquery.SchemaField("target_table", "STRING"),
    bigquery.SchemaField("rows_loaded", "INT64"),
    bigquery.SchemaField("status", "STRING", mode="REQUIRED"),      # SUCCESS / FAILED
    bigquery.SchemaField("message", "STRING"),
    bigquery.SchemaField("details_json", "STRING"),
    bigquery.SchemaField("started_at", "TIMESTAMP"),
    bigquery.SchemaField("finished_at", "TIMESTAMP"),
]


def registry_table_id() -> str:
    return config.table_id(config.DATASET_RAW, config.LOAD_REGISTRY_TABLE)


def ensure_registry(client: bigquery.Client) -> None:
    """Create the registry table if it does not exist yet."""
    table = bigquery.Table(registry_table_id(), schema=REGISTRY_SCHEMA)
    client.create_table(table, exists_ok=True)


def already_loaded(client: bigquery.Client, source: str, fingerprint: str) -> bool:
    """True if this exact file set was already loaded successfully."""
    sql = f"""
        SELECT COUNT(*) AS n
        FROM `{registry_table_id()}`
        WHERE source = @source AND fingerprint = @fp AND status = 'SUCCESS'
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("source", "STRING", source),
            bigquery.ScalarQueryParameter("fp", "STRING", fingerprint),
        ]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    return rows[0].n > 0


def record(client: bigquery.Client, *, run_id: str, source: str, status: str,
           started_at: datetime, delivery: str | None = None, gcs_prefix: str | None = None,
           file_count: int | None = None, total_bytes: int | None = None,
           fingerprint: str | None = None, target_table: str | None = None,
           rows_loaded: int | None = None, message: str | None = None,
           details: dict | None = None) -> None:
    """Append one row to the registry (uses DML so the row is immediately queryable)."""
    sql = f"""
        INSERT INTO `{registry_table_id()}`
        (run_id, source, delivery, gcs_prefix, file_count, total_bytes, fingerprint,
         target_table, rows_loaded, status, message, details_json, started_at, finished_at)
        VALUES (@run_id, @source, @delivery, @gcs_prefix, @file_count, @total_bytes,
                @fingerprint, @target_table, @rows_loaded, @status, @message, @details,
                @started_at, @finished_at)
    """
    p = bigquery.ScalarQueryParameter
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            p("run_id", "STRING", run_id),
            p("source", "STRING", source),
            p("delivery", "STRING", delivery),
            p("gcs_prefix", "STRING", gcs_prefix),
            p("file_count", "INT64", file_count),
            p("total_bytes", "INT64", total_bytes),
            p("fingerprint", "STRING", fingerprint),
            p("target_table", "STRING", target_table),
            p("rows_loaded", "INT64", rows_loaded),
            p("status", "STRING", status),
            p("message", "STRING", message),
            p("details", "STRING", json.dumps(details, default=str) if details else None),
            p("started_at", "TIMESTAMP", started_at),
            p("finished_at", "TIMESTAMP", datetime.now(timezone.utc)),
        ]
    )
    client.query(sql, job_config=cfg).result()
