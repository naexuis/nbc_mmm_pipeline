"""
Single place for every project-wide setting.

If a project, dataset, bucket, or folder ever changes, change it here only.
No other file should contain these names.
"""

# ---- Google Cloud ----------------------------------------------------------
PROJECT_ID = "prj-cgh-glbl-serv-npd-glbl"
LOCATION = "US"

# Domino environment variable holding the service-account JSON key.
GCP_KEY_ENV_VAR = "GCP_KEY"

# Time zone used when showing timestamps to analysts.
DISPLAY_TIMEZONE = "America/Chicago"

# ---- BigQuery datasets -----------------------------------------------------
DATASET_RAW = "us_hment_mmm_raw"      # exact copies of source files + load/run logs
DATASET_CORE = "us_hment_mmm_core"    # cleaned, checked tables
DATASET_MODEL = "us_hment_mmm_model"  # versioned model-ready panels

# ---- Media source (Prisma + Mediaocean) ------------------------------------
MEDIA_BUCKET = "nbcuni-media-ocean"
MEDIA_ROOT_PREFIX = "nbcuni-media-ocean/"

# ---- Revenue source (exported by hand from NBC GCP) ------------------------
REVENUE_BUCKET = "nbcu-marcus-hilliard"
REVENUE_US_PREFIX = "hment_rev_usa/"

# Raw revenue table: one partition per delivery (each delivery is full history).
REVENUE_RAW_TABLE = "revenue_pos_usa"
# How many deliveries to keep in the raw table (older ones are removed).
REVENUE_KEEP_DELIVERIES = 2
# Columns the rest of the pipeline relies on; a delivery without them is rejected.
REVENUE_REQUIRED_COLUMNS = (
    "product_id1",
    "imdb_title_id",
    "release_date",
    "transaction_date",
    "royalty_total_usd",
)

# ---- Run log ---------------------------------------------------------------
# One row per load attempt (any source), kept in the raw dataset.
LOAD_REGISTRY_TABLE = "load_registry"


def table_id(dataset: str, table: str) -> str:
    """Return the fully qualified BigQuery table id, e.g. project.dataset.table."""
    return f"{PROJECT_ID}.{dataset}.{table}"
