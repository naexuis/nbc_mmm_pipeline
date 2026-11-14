"""Media plan parser: header block, blocks closed by subtotals, value added, SIF, totals."""

import datetime as dt

from us_hment_mmm.media.load_plans import check_note, parse_plan, stated_value

D = dt.datetime


def rows():
    blank = (None,) * 8
    return [
        (None, "Campaign:", None, "Film X"),
        (None, "PEW Date:", None, D(2025, 12, 30), "PEW"),
        (None, "EST Date: ", None, D(2026, 1, 20)),
        blank,
        (None, "Channel", "Publisher", "Placement Details", "Targeting", "Start Date",
         "Planned Spend", "Planned Impressions", "Pricing Model"),
        (None, "CTV/OTT", "Roku", "Homescreen", "All", D(2025, 12, 30), 100, 1000, "CPM"),
        (None, "CTV/OTT", "Roku", "Added Value: Pause Ads", "All", D(2025, 12, 30), 0, 500,
         "Added Value"),
        (None, "CTV/OTT", None, "Second flight", "All", D(2026, 1, 5), 50, 400, "CPM"),
        (None, None, "Roku Total", None, None, None, 150, 1900, None),
        (None, "MVPD", "Comcast", "Banners", "All", D(2025, 12, 30), 40, 900, "Flat"),
        (None, None, "Comcast SIF Total", None, None, None, 40, 900, None),
        (None, "Social", "TikTok", "PVOD - LP", "Landing Page (AV $20K)", D(2025, 12, 26), 10,
         100, "CPM"),
        (None, "Social", "TikTok", None, None, None, None, None, None),        # placeholder
        (None, None, "TikTok Total", None, None, None, 10, 100, None),
        blank,
        (None, None, None, None, "NET MEDIA TOTAL", None, 160),
        (None, None, None, None, "GRAND TOTAL", None, 175),
    ]


def test_parse_plan():
    p = parse_plan(rows(), "2025_Film_X.xlsx")
    plan, lines = p["plan"], p["lines"]
    assert plan["campaign"] == "Film X" and plan["plan_year"] == 2025
    assert plan["pvod_date"] == dt.date(2025, 12, 30) and plan["est_date"] == dt.date(2026, 1, 20)
    assert [ln["block"] for ln in lines] == ["Roku Total"] * 3 + ["Comcast SIF Total", "TikTok Total"]
    assert lines[2]["publisher"] == "Roku"                       # carried down from the line above
    assert [ln["is_value_added"] for ln in lines] == [False, True, False, False, True]
    assert [ln["is_sif"] for ln in lines] == [False, False, False, True, False]
    assert lines[4]["stated_value"] == 20000
    assert plan["net_media_total"] == 160 and plan["grand_total"] == 175
    assert plan["sif_spend"] == 40 and plan["check_note"].startswith("OK")


def test_stated_value_and_check():
    assert stated_value("Landing Page - (AV $2,215.42)") == 2215.42
    assert stated_value("LP (OCMR Test - AV $30,000)") == 30000
    assert stated_value("Plain placement") is None
    assert check_note({"net_media_total": None}).startswith("No NET MEDIA")
    assert check_note({"net_media_total": 100, "planned_spend": 150, "sif_spend": 30,
                       "co_op_total": 20}).startswith("OK")
