"""Fast checks that run anywhere (no Google Cloud access needed)."""

from us_hment_mmm import config


def test_table_id_format():
    assert config.table_id(config.DATASET_RAW, "x") == "prj-cgh-glbl-serv-npd-glbl.us_hment_mmm_raw.x"


def test_dataset_names_share_prefix():
    for ds in (config.DATASET_RAW, config.DATASET_CORE, config.DATASET_MODEL):
        assert ds.startswith("us_hment_mmm_")


def test_prefixes_end_with_slash():
    assert config.MEDIA_ROOT_PREFIX.endswith("/")
    assert config.REVENUE_US_PREFIX.endswith("/")
