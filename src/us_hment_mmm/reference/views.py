"""
Shared views on the title tables (us_hment_mmm_core), listed in DATA_OWNERS.md.

  title_as_of(as_of TIMESTAMP)  table function: every film's values exactly as they stood at
                                that moment, rebuilt from title_history (history starts with
                                the seed on 2026-10-31). Used to rebuild past MMM / MMT runs.
  title_detail                  view: the title table plus the revenue product IDs and media
                                product codes linked to each film (from title_alias).

Create or refresh them (safe to repeat; also adds any title columns added since the seed):
    python -m us_hment_mmm.reference.views --create
Check that title_as_of(now) matches the title table exactly:
    python -m us_hment_mmm.reference.views --check
"""

from __future__ import annotations

import argparse

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client

AS_OF_FUNCTION = "title_as_of"
DETAIL_VIEW = "title_detail"
TEXT_FIELDS = ("title_name", "distributor", "nbc_genre_1", "nbc_genre_2", "in_mmm_reason",
               "review_note")
DATE_FIELDS = ("release_date", "pvod_date", "est_date")
COMPARE_FIELDS = ("title_name", "distributor", *DATE_FIELDS, "nbc_genre_1", "nbc_genre_2",
                  "in_mmm", "in_mmm_reason", "review_note")


def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def as_of_sql() -> str:
    """CREATE statement for the title_as_of table function."""
    pick = lambda f: f"MAX(IF(field = '{f}', new_value, NULL))"  # noqa: E731
    cols = (["imdb_id"]
            + [f"{pick(f)} AS {f}" for f in ("title_name", "distributor")]
            + [f"SAFE_CAST({pick(f)} AS DATE) AS {f}" for f in DATE_FIELDS]
            + [f"{pick(f)} AS {f}" for f in ("nbc_genre_1", "nbc_genre_2")]
            + [f"CASE LOWER({pick('in_mmm')}) WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE "
               f"END AS in_mmm"]
            + [f"{pick(f)} AS {f}" for f in ("in_mmm_reason", "review_note")])
    body = ",\n           ".join(cols)
    return f"""
CREATE OR REPLACE TABLE FUNCTION `{_core(AS_OF_FUNCTION)}`(as_of TIMESTAMP) AS
WITH latest AS (
  SELECT record_key AS imdb_id, field, new_value,
         ROW_NUMBER() OVER (PARTITION BY record_key, field
                            ORDER BY changed_at DESC, change_id DESC) AS rn
  FROM `{_core(config.TITLE_HISTORY_TABLE)}`
  WHERE table_name = '{config.TITLE_TABLE}' AND changed_at <= as_of
)
SELECT {body}
FROM latest
WHERE rn = 1
GROUP BY imdb_id
"""


def detail_sql() -> str:
    """CREATE statement for the title_detail view."""
    alias = _core(config.TITLE_ALIAS_TABLE)
    return f"""
CREATE OR REPLACE VIEW `{_core(DETAIL_VIEW)}` AS
SELECT t.*,
       ARRAY(SELECT a.alias_value FROM `{alias}` a
             WHERE a.imdb_id = t.imdb_id AND a.alias_type = 'revenue_product_id'
               AND a.category = 'film' ORDER BY a.alias_value) AS revenue_product_ids,
       ARRAY(SELECT CONCAT(a.business_group, ':', a.alias_value) FROM `{alias}` a
             WHERE a.imdb_id = t.imdb_id AND a.alias_type = 'media_product_code'
               AND a.category = 'film' ORDER BY 1) AS media_product_codes
FROM `{_core(config.TITLE_TABLE)}` t
"""


def check_sql() -> str:
    """Rows where title_as_of(now) differs from the title table (should be none)."""
    diffs = " OR ".join(f"t.{f} IS DISTINCT FROM h.{f}" for f in COMPARE_FIELDS)
    return f"""
SELECT COALESCE(t.imdb_id, h.imdb_id) AS imdb_id
FROM `{_core(config.TITLE_TABLE)}` t
FULL OUTER JOIN `{_core(AS_OF_FUNCTION)}`(CURRENT_TIMESTAMP()) h USING (imdb_id)
WHERE t.imdb_id IS NULL OR h.imdb_id IS NULL OR {diffs}
"""


def upgrade_sql() -> str:
    """Add columns introduced after the seed (safe to repeat)."""
    adds = ", ".join(f"ADD COLUMN IF NOT EXISTS {f} STRING" for f in ("nbc_genre_1", "nbc_genre_2"))
    return f"ALTER TABLE `{_core(config.TITLE_TABLE)}` {adds}"


def create_views(client=None) -> None:
    client = client or bq_client()
    for sql in (upgrade_sql(), as_of_sql(), detail_sql()):
        client.query(sql).result()


def check(client=None) -> list[str]:
    client = client or bq_client()
    return [r.imdb_id for r in client.query(check_sql()).result()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Shared views on the title tables.")
    ap.add_argument("--create", action="store_true", help="Create or refresh the views")
    ap.add_argument("--check", action="store_true", help="Compare title_as_of(now) with title")
    args = ap.parse_args()
    if not (args.create or args.check):
        ap.error("use --create and/or --check")
    client = bq_client()
    if args.create:
        create_views(client)
        print(f"Created {config.DATASET_CORE}.{AS_OF_FUNCTION} and {config.DATASET_CORE}."
              f"{DETAIL_VIEW}.")
    if args.check:
        bad = check(client)
        if bad:
            print(f"{len(bad)} film(s) differ between title and title_as_of(now): "
                  + ", ".join(bad[:20]))
        else:
            print("title_as_of(now) matches the title table exactly.")


if __name__ == "__main__":
    main()
