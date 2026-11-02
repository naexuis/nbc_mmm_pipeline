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

# Advertiser codes: ZUE = UP Home Ent, ZFF/ZFE = Focus, ZUV = MVPD. (ZUP = theatrical, excluded.)
# Prisma folders that belong to US Home Entertainment.
MEDIA_PRISMA_FOLDERS = ("nbc-zue", "nbc-zff", "nbc-zfe")
# Mediaocean folder (holds every client; US HE rows are filtered later, in the core layer).
MEDIA_MEDIAOCEAN_FOLDER = "nbc-netpak-spotpak"

# Business code -> business group used to read product codes (codes are unique within a group).
# ZUV (MVPD, in Mediaocean) buys for UP Home Ent titles with UP Home Ent codes.
MEDIA_BUSINESS_GROUPS = {"ZUE": "UP_HE", "ZUV": "UP_HE", "ZFF": "FOCUS", "ZFE": "FOCUS"}

# File type (token in the file name) -> raw table name.
MEDIA_PRISMA_TYPES = {
    "PrismaCampaigns": "prisma_campaigns",
    "PrismaPlacements": "prisma_placements",
    "PrismaAdvancedPlacementDetails": "prisma_advanced_placement_details",
    "PrismaPlacementBuys": "prisma_placement_buys",
}
MEDIA_MEDIAOCEAN_TYPES = {
    "MediaoceanNetPak": "mediaocean_netpak",
    "MediaoceanSpotPak": "mediaocean_spotpak",
}
# How many snapshot dates to keep in each raw media table.
MEDIA_KEEP_SNAPSHOTS = 3

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

# ---- Reference CSVs copied by hand from NBC GCP (one dated file per copy) ---
# Each source: folder in REVENUE_BUCKET, file-name pattern (date in group 1), raw table,
# required columns, and how many copies to keep.
REFERENCE_SOURCES = {
    "enrichment_title": {
        "bucket": "nbcu-marcus-hilliard",
        "prefix": "enrichments_table_stage/",
        "pattern": r"^(\d{8})_enrichments_title\.csv$",
        "table": "nbc_enrichment_title",
        "required": ("imdb_id", "title_name", "studio_distributor", "us_widest_release_date",
                     "us_pvod_start_date", "us_est_date"),
        "keep": 3,
        "description": "NBC enrichment title table (titles, distributor, genre, window dates)",
    },
    "std_chtt": {
        "bucket": "nbcu-marcus-hilliard",
        "prefix": "std_chtt_table_stage/",
        "pattern": r"^he_ent_std_chtt_(\d{8})\.csv$",
        "table": "finance_std_chtt",
        "required": ("imdb_id", "title_name", "premium_release_date", "est_release_date",
                     "total_digital_premium_revenue"),
        "keep": 6,
        "description": "NBC Finance Standard CHTT (Finance window dates and premium revenue)",
    },
}

# ---- Sharded CSV sources (dated folder of export shards) ---------------------
REVENUE_SPEC = {
    "source": "revenue_usa",
    "label": "REVENUE LOAD",
    "description": "US HE digital POS revenue (royalty and retail, by sale)",
    "bucket": REVENUE_BUCKET,
    "prefix": REVENUE_US_PREFIX,
    "table": REVENUE_RAW_TABLE,
    "required": REVENUE_REQUIRED_COLUMNS,
    "keep": REVENUE_KEEP_DELIVERIES,
    "date_col": "transaction_date", "amount_col": "royalty_total_usd",
    "first_label": "First sale", "last_label": "Last sale", "amount_label": "Royalty (USD)",
}

# US cinema box office (Comscore daily gross per theater and title), copied by hand.
# Opening weekend = release day (Thursday or Friday) through Sunday, inclusive.
BOX_OFFICE_SPEC = {
    "source": "box_office_usa",
    "label": "BOX OFFICE LOAD",
    "description": "US cinema box office, daily gross per theater and title (Comscore)",
    "bucket": "nbcu-marcus-hilliard",
    "prefix": "up_bo_usa/",
    "table": "box_office_usa",
    "required": ("imdb_id", "title_name", "release_date", "tracking_date", "day_rev_usd"),
    "keep": 2,
    "date_col": "tracking_date", "amount_col": "day_rev_usd",
    "first_label": "First day", "last_label": "Last day", "amount_label": "Box office (USD)",
}

# ---- Title reference (core layer; changed only through the review notebook) ----
TITLE_TABLE = "title"                  # one row per film, keyed on IMDb ID
TITLE_ALIAS_TABLE = "title_alias"      # product codes (media) and product IDs (revenue) -> film
TITLE_HISTORY_TABLE = "title_history"  # every change: who, when, why, old -> new
# Domino dataset folder for title review workbooks, with subfolders:
#   issued/    new workbooks to download    returned/  filled-in workbooks to upload
#   imported/  workbooks already saved to BigQuery
REVIEW_DIR = "/domino/datasets/local/nbcu_mmm_dev/us_hment_mmm/review/titles"

# ---- Distributor standard names (agreed with Marcus 2026-11-01) --------------------
# Any spelling on the left (old dictionary or NBC enrichment) is stored as the name on the
# right. Matching ignores case, punctuation and extra spaces. Unlisted names are kept as is.
DISTRIBUTOR_STANDARD = {
    "UNIVERSAL PICTURES": "UNIVERSAL",
    "FOCUS FEATURES": "FOCUS",
    "STX ENTERTAINMENT": "STX", "STX FILMS": "STX",
    "BLEECKER ST": "BLEECKER STREET", "BLEECKER STREET MEDIA": "BLEECKER STREET",
    "BRIARCLIFF ENTERTAINMENT": "BRIARCLIFF",
    "OPEN ROAD FILMS": "OPEN ROAD",
    "OPENROAD BRIARCLIFF": "OPEN ROAD/BRIARCLIFF", "OPEN ROAD BRIARCLIFF": "OPEN ROAD/BRIARCLIFF",
    "METRO GOLDWYN MAYER": "MGM",
    "MOMENTUM PICTURES": "MOMENTUM", "MMTP": "MOMENTUM",
    "1440 ENTERTAINMENT": "UNIVERSAL 1440", "UNIVERSAL 1440 ENTERTAINMENT": "UNIVERSAL 1440",
    "PURE FLIX ENTERTAINMENT": "PURE FLIX", "PURE FLIX DIGITAL": "PURE FLIX", "PURE": "PURE FLIX",
    "FATHOM EVENTS": "FATHOM", "FTHM": "FATHOM",
    "GO": "WELL GO USA", "WELL GO USA INC": "WELL GO USA", "WELL GO USA ENT": "WELL GO USA",
    "DREAMWORKKS": "DREAMWORKS",
    "AVIRON PICTURES": "AVIRON",
    "GE": "GREENWICH ENTERTAINMENT",
}

# ---- Shared tables owned by other projects (see DATA_OWNERS.md) ----------------
# Amazon MMT Pipeline: first week each film earned Amazon revenue, and Amazon week quality.
AMAZON_FIRST_REVENUE_TABLE = f"{PROJECT_ID}.UPNA_client_he_amazon_mmt_dev.film_first_revenue"
AMAZON_WEEK_QUALITY_TABLE = f"{PROJECT_ID}.UPNA_client_he_amazon_mmt_dev.amazon_week_quality"

# ---- Run log ---------------------------------------------------------------
# One row per load attempt (any source), kept in the raw dataset.
LOAD_REGISTRY_TABLE = "load_registry"


def table_id(dataset: str, table: str) -> str:
    """Return the fully qualified BigQuery table id, e.g. project.dataset.table."""
    return f"{PROJECT_ID}.{dataset}.{table}"
