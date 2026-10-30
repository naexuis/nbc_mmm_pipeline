"""
Google Cloud sign-in, shared by every pipeline step.

Usage:
    from us_hment_mmm.gcp import bq_client, gcs_client
    bq = bq_client()
    gcs = gcs_client()

The service-account key is read from the GCP_KEY environment variable that
Domino provides. The key is never written to disk or into code.
"""

import json
import os
from functools import lru_cache

from google.cloud import bigquery, storage
from google.oauth2 import service_account

from us_hment_mmm import config

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@lru_cache(maxsize=1)
def _credentials() -> service_account.Credentials:
    raw = os.environ.get(config.GCP_KEY_ENV_VAR)
    if not raw:
        raise RuntimeError(
            f"Environment variable {config.GCP_KEY_ENV_VAR} is not set. "
            "Run this inside the Domino project where the GCP key is configured."
        )
    return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=_SCOPES)


@lru_cache(maxsize=1)
def bq_client() -> bigquery.Client:
    """BigQuery client for the project in config.PROJECT_ID."""
    return bigquery.Client(
        project=config.PROJECT_ID, credentials=_credentials(), location=config.LOCATION
    )


@lru_cache(maxsize=1)
def gcs_client() -> storage.Client:
    """Cloud Storage client using the same credentials."""
    return storage.Client(project=config.PROJECT_ID, credentials=_credentials())
