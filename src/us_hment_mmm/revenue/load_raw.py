"""
Load a revenue (POS) delivery from GCS into BigQuery (raw layer).

US     : gs://nbcu-marcus-hilliard/hment_rev_usa/<YYYYMMDD>/*.csv -> us_hment_mmm_raw.revenue_pos_usa
Canada : gs://nbcu-marcus-hilliard/hment_rev_can/<YYYYMMDD>/*.csv -> us_hment_mmm_raw.revenue_pos_can
(exported by hand from NBC GCP; one partition per delivery). Canada is never used by the MMM;
it is loaded only to reconcile with Finance's CHTT, which is US + Canada.

The work is done by the shared engine in us_hment_mmm.sharded_csv; see it for the checks.

Usage (Domino terminal or Domino Job):
    python -m us_hment_mmm.revenue.load_raw --dry-run          # checks only, changes nothing
    python -m us_hment_mmm.revenue.load_raw                    # load the newest delivery
    python -m us_hment_mmm.revenue.load_raw --delivery 20261008
    python -m us_hment_mmm.revenue.load_raw_can --dry-run      # the same for Canada
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
SPECS = {"USA": config.REVENUE_SPEC, "CAN": config.REVENUE_CAN_SPEC}


def _spec(country: str) -> dict:
    key = str(country or "USA").strip().upper()
    if key not in SPECS:
        raise ValueError(f"COUNTRY must be \"USA\" or \"CAN\", not {country!r}")
    return SPECS[key]


def run(delivery: str = "latest", dry_run: bool = True, force: bool = False,
        allow_smaller: bool = False, show_columns: bool = False, country: str = "USA") -> dict:
    """Load the newest (or a given) revenue delivery. Defaults to a dry run."""
    return sharded_csv.run(_spec(country), delivery=delivery, dry_run=dry_run, force=force,
                           allow_smaller=allow_smaller, show_columns=show_columns)


def raw_table_summary(country: str = "USA"):
    return sharded_csv.raw_table_summary(_spec(country))


def recent_loads(n: int = 5, country: str = "USA"):
    return sharded_csv.recent_loads(_spec(country), n)


def show_raw_table_summary(country: str = "USA"):
    return sharded_csv.show_raw_table_summary(_spec(country))


def show_recent_loads(n: int = 5, country: str = "USA"):
    return sharded_csv.show_recent_loads(_spec(country), n)


if __name__ == "__main__":
    sharded_csv.cli(SPEC, "Load a US revenue delivery into BigQuery.")
