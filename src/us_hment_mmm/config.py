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


def table_id(dataset: str, table: str) -> str:
    """Return the fully qualified BigQuery table id, e.g. project.dataset.table."""
    return f"{PROJECT_ID}.{dataset}.{table}"
