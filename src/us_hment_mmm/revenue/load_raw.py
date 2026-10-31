"""
Load a US revenue (POS) delivery from GCS into BigQuery (raw layer).

Source : gs://nbcu-marcus-hilliard/hment_rev_usa/<YYYYMMDD>/*.csv  (exported by hand from NBC GCP)
Target : <project>.us_hment_mmm_raw.revenue_pos_usa                (one partition per delivery)

The work is done by the shared engine in us_hment_mmm.sharded_csv; see it for the checks.

Usage (Domino terminal or Domino Job):
    python -m us_hment_mmm.revenue.load_raw --dry-run          # checks only, changes nothing
    python -m us_hment_mmm.revenue.load_raw                    # load the newest delivery
    python -m us_hment_mmm.revenue.load_raw --delivery 20261008
"""

from __future__ import annotations

from us_hment_mmm import config, sharded_csv
from us_hment_mmm.sharded_csv import (  # noqa: F401  (re-exported for tests and notebooks)
    Delivery,
    SourceFile,
    check_delivery,
    group_deliveries,
    parse_header,
    pick_delivery,
    sanitize_column,
)

SPEC = config.REVENUE_SPEC


def run(delivery: str = "latest", dry_run: bool = True, force: bool = False,
        allow_smaller: bool = False, show_columns: bool = False) -> dict:
    """Load the newest (or a given) revenue delivery. Defaults to a dry run."""
    return sharded_csv.run(SPEC, delivery=delivery, dry_run=dry_run, force=force,
                           allow_smaller=allow_smaller, show_columns=show_columns)


def raw_table_summary():
    return sharded_csv.raw_table_summary(SPEC)


def recent_loads(n: int = 5):
    return sharded_csv.recent_loads(SPEC, n)


def show_raw_table_summary():
    return sharded_csv.show_raw_table_summary(SPEC)


def show_recent_loads(n: int = 5):
    return sharded_csv.show_recent_loads(SPEC, n)


if __name__ == "__main__":
    sharded_csv.cli(SPEC, "Load a US revenue delivery into BigQuery.")
