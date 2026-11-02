"""Unit tests for the channel label seed helpers (no Google Cloud)."""

import json
from pathlib import Path

from us_hment_mmm.channels import labels as L

D = {**json.loads((Path(__file__).resolve().parents[1] / "reference_seed" /
                   "nspack_network_dict.json").read_text()), **L.NETWORK_EXTRA}


def test_parent_of_prefers_longest_parent():
    assert L.parent_of("DISPLAY_VIDEO_IMDB") == "DISPLAY_VIDEO"
    assert L.parent_of("VIDEO_WILLOW") == "VIDEO"
    assert L.parent_of("SPOTTV_LOCALTV") == "SPOTTV"
    assert L.parent_of("WHATEVER") is None


def test_network_lookup_fixes_the_old_mistakes():
    assert L.network_label("DSC N", "CABLE", D)[1] == "CABLE_THEDISCOVERYCHANNEL"
    assert L.network_label("COX N", "CABLE", D)[1] == "MVPD_COX"
    # a local TV station is not radio
    assert L.network_label("STATION KABC-TV", "SPOT TV", D)[1] is None
    assert L.network_label("STATION WXYZ-FM", "SPOT RADIO", D)[1] == "AUDIO_IHEART"
    # a cable network whose first letters match a radio seller
    net, label, note = L.network_label("ACT N", "CABLE", D)
    assert label is None and "does not fit TV" in note
    assert L.network_label("FOX TV STATIONS", "SPOT TV", D)[1] == "SPOTTV_LOCALTV"
    assert L.network_label("ONE N", "CABLE", D)[1] == "MVPD_FREEWHEEL"
    assert L.network_label("ZZZZ N", "CABLE", D)[2] == "not in the network dictionary"


def test_mmm_overrides():
    assert L.mmm_label_for("CABLE_NETFLIX") == "STREAMING_NETFLIX"
    assert L.mmm_label_for("CABLE_ESPN") == "CABLE_ESPN"


def test_placement_rows_match_by_name_and_skip_duplicates():
    rows = [{"placement_number": "P1234567", "site": "A", "placement_name": "x",
             "mediaplan_label": "SOCIAL_FACEBOOK", "mmm_label": "SOCIAL_FACEBOOK"},
            {"placement_number": "P1234567", "site": "A", "placement_name": "x",
             "mediaplan_label": "SOCIAL_FACEBOOK", "mmm_label": "SOCIAL_FACEBOOK"},
            {"placement_number": "", "site": "Google", "placement_name": "ZUE_KF4_004_KFP HE YT",
             "mediaplan_label": "P", "mmm_label": "PROGRAMMATIC_YOUTUBE"},
            {"placement_number": "", "site": "x", "placement_name": "nowhere",
             "mediaplan_label": "P", "mmm_label": "P"}]
    out, missing = L.placement_rows(rows, {"ZUE KF4 004 KFP HE YT|GOOGLE": "P9999999"})
    assert [r["key"] for r in out] == ["P1234567", "P9999999"] and len(missing) == 1
    assert "matched by placement name" in out[1]["reason"]


def test_network_rows_leave_conflicts_for_review():
    codes = [{"code": "DSC N", "media_name": "CABLE", "spend": 10},
             {"code": "ACT N", "media_name": "CABLE", "spend": 5}]
    out, left = L.network_rows(codes, D)
    assert [r["key"] for r in out] == ["DSC N"] and out[0]["mmm_label"] == "STREAMING_THEDISCOVERYCHANNEL"
    assert left[0]["code"] == "ACT N"


def test_view_marks_missing_labels():
    sql = L.view_sql()
    assert "NEEDS A CHANNEL" in sql and "UNION ALL" in sql and "media_tv_daily" in sql
