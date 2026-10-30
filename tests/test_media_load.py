"""Unit tests for the media loader's pure helpers (no Google Cloud needed)."""

import gzip
import zipfile

from us_hment_mmm.media.load_raw import (
    choose_snapshot,
    clean_column,
    file_type,
    folder_check,
    is_manifest,
    name_date,
    parse_manifest,
    transcode_zip_to_gzip,
)

PRISMA_FILES = [
    "463_PrismaCampaigns_3_0_20261030102042.txt.zip",
    "465_PrismaPlacements_8_0_20261030055447.txt.zip",
    "466_PrismaAdvancedPlacementDetails_2_1_20261030061914.txt.zip",
    "545_PrismaPlacementBuys_3_0_20261030080819.txt.zip",
]
MO_FILES = [
    "300_MediaoceanPrintPak_3_0_20261030144214.txt.zip",
    "301_MediaoceanSpotPak_3_0_20261030144142.txt.zip",
    "302_MediaoceanNetPak_3_0_20261030144209.txt.zip",
    "561_MediaoceanTargetNetPak_1_0_20261030144219.txt.zip",
]


def test_file_type_does_not_confuse_similar_names():
    assert file_type(PRISMA_FILES[1]) == "PrismaPlacements"
    assert file_type(PRISMA_FILES[3]) == "PrismaPlacementBuys"
    assert file_type(PRISMA_FILES[2]) == "PrismaAdvancedPlacementDetails"
    assert file_type(MO_FILES[2]) == "MediaoceanNetPak"
    assert file_type(MO_FILES[3]) is None          # TargetNetPak is not NetPak
    assert file_type(MO_FILES[0]) is None          # PrintPak not loaded


def test_manifest_and_dates():
    assert is_manifest("463_Prisma_Manifest_20261030103930436.txt")
    assert not is_manifest(PRISMA_FILES[0])
    assert name_date("463_Prisma_Manifest_20261030103930436.txt") == "20261030"
    assert name_date(PRISMA_FILES[0]) == "20261030"


def test_parse_manifest_utf16():
    raw = "\n".join(PRISMA_FILES).encode("utf-16")
    assert parse_manifest(raw) == PRISMA_FILES


def test_folder_check_complete_and_missing():
    ok, _ = folder_check("nbc-zue", PRISMA_FILES, set(PRISMA_FILES))
    assert ok
    ok, reason = folder_check("nbc-zue", PRISMA_FILES[:3], set(PRISMA_FILES))
    assert not ok and "PrismaPlacementBuys" in reason
    ok, reason = folder_check("nbc-zue", PRISMA_FILES, set(PRISMA_FILES[:3]))
    assert not ok and "not in bucket" in reason
    ok, reason = folder_check("nbc-zue", None, set())
    assert not ok and reason == "no manifest"
    ok, _ = folder_check("nbc-netpak-spotpak", MO_FILES, set(MO_FILES))
    assert ok


def _status(d, missing=()):
    folders = ["nbc-zue", "nbc-zff", "nbc-zfe", "nbc-netpak-spotpak"]
    return {d: {f: (f not in missing, "") for f in folders}}


def test_choose_newest_date_where_everything_is_complete():
    status = {**_status("20261028"), **_status("20261029", missing=["nbc-netpak-spotpak"]),
              **_status("20261030")}
    assert choose_snapshot(status) == "20261030"
    status["20261030"]["nbc-zff"] = (False, "")
    assert choose_snapshot(status) == "20261028"          # falls back past the 29th too
    assert choose_snapshot(status, "20261029") is None
    assert choose_snapshot(status, "20261028") == "20261028"


def test_clean_column_keeps_case():
    assert clean_column("CampaignPublicId") == "CampaignPublicId"
    assert clean_column("Pub Display") == "Pub_Display"


def test_transcode_utf16_zip(tmp_path):
    text = "A\tB\r\n1\tx\r\n\r\n2\ty\rz\r\n"
    zpath = tmp_path / "f.txt.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("f.txt", text.encode("utf-16"))
    header, rows = transcode_zip_to_gzip(str(zpath), str(tmp_path / "f.gz"))
    assert header == ["A", "B"]
    assert rows == 2
    out = gzip.open(tmp_path / "f.gz", "rt", encoding="utf-8").read()
    assert out == "A\tB\n1\tx\n2\ty z\n"
