"""
Load a Canada revenue (POS) delivery: the same as load_raw, for gs://.../hment_rev_can/.

Usage:
    python -m us_hment_mmm.revenue.load_raw_can --dry-run
    python -m us_hment_mmm.revenue.load_raw_can
"""

from us_hment_mmm import config, sharded_csv

if __name__ == "__main__":
    sharded_csv.cli(config.REVENUE_CAN_SPEC, "Load a Canada revenue delivery into BigQuery.")
