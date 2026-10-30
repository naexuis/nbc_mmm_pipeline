"""
create_bq_datasets.py  —  One-time setup: create the three US Home Entertainment MMM datasets.

Creates (only if they don't already exist; safe to re-run):
    prj-cgh-glbl-serv-npd-glbl.us_hment_mmm_raw    exact copies of source files + load/run logs
    prj-cgh-glbl-serv-npd-glbl.us_hment_mmm_core   cleaned, checked media spend, revenue, title reference
    prj-cgh-glbl-serv-npd-glbl.us_hment_mmm_model  versioned title x day panel the model reads

Nothing else is created, changed, or deleted.

Run in the Domino workspace terminal:
    python scripts/create_bq_datasets.py
"""

import os
import json
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT_ID = "prj-cgh-glbl-serv-npd-glbl"
LOCATION = "US"  # matches the multi-region US buckets, so GCS -> BigQuery loads work

DATASETS = {
    "us_hment_mmm_raw":   "US Home Entertainment MMM - exact copies of source files (Prisma, Mediaocean, revenue) and load/run logs.",
    "us_hment_mmm_core":  "US Home Entertainment MMM - cleaned and checked media spend, revenue, title reference, channel mappings.",
    "us_hment_mmm_model": "US Home Entertainment MMM - versioned model-ready title x day panels.",
}


def get_client() -> bigquery.Client:
    info = json.loads(os.environ["GCP_KEY"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return bigquery.Client(project=PROJECT_ID, credentials=creds)


def main():
    client = get_client()
    print(f"Project: {PROJECT_ID}   Location: {LOCATION}\n")

    for name, description in DATASETS.items():
        ds = bigquery.Dataset(f"{PROJECT_ID}.{name}")
        ds.location = LOCATION
        ds.description = description
        ds.labels = {"project": "us_hment_mmm", "managed_by": "pipeline"}
        try:
            existing = client.get_dataset(ds.reference)
            print(f"EXISTS   {name}  (location {existing.location}) - left unchanged")
        except NotFound:
            client.create_dataset(ds)
            print(f"CREATED  {name}")

    print("\nDatasets now visible in the project that start with 'us_hment_mmm':")
    for d in client.list_datasets():
        if d.dataset_id.startswith("us_hment_mmm"):
            print(f"  - {d.dataset_id}")


if __name__ == "__main__":
    main()
