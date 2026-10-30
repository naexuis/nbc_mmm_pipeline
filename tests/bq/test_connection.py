"""
Live connection checks. Run in Domino only:
    pytest -m bq tests/bq
"""

import pytest

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client, gcs_client

pytestmark = pytest.mark.bq


def test_datasets_exist():
    client = bq_client()
    for ds in (config.DATASET_RAW, config.DATASET_CORE, config.DATASET_MODEL):
        assert client.get_dataset(f"{config.PROJECT_ID}.{ds}").location == config.LOCATION


def test_media_bucket_readable():
    blobs = list(gcs_client().list_blobs(config.MEDIA_BUCKET, prefix=config.MEDIA_ROOT_PREFIX, max_results=1))
    assert blobs, "No objects found under the media prefix"


def test_revenue_bucket_readable():
    blobs = list(gcs_client().list_blobs(config.REVENUE_BUCKET, prefix=config.REVENUE_US_PREFIX, max_results=1))
    assert blobs, "No objects found under the revenue prefix"
