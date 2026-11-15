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


def test_short_plan():
    from us_hment_mmm.media.load_plans import parse_short_plan, read_xlsx  # noqa: F401
    rows = [
        (None, "Title:", "Film Y PVOD", "Platform - Type", "Flight", "Budget", "Est Impressions",
         "Est CPM", "Benchmarks", "Amount Spent", "Delivered Impressions"),
        (None, "PVOD Street Date:", D(2023, 7, 11), "FBIG - Vudu", "7/11 - 7/23", 75000, 18750000,
         4, "Comp", 64998.69, 14478968),
        (None, "Study", "N/A", "Total", None, 75000, 18750000, 4),
        (None,) * 6,
        (None, "Platform", "Type", "Objective"),
    ]
    p = parse_short_plan(rows, "2023_Film_Y_PVOD.xlsx")
    assert p["plan"]["plan_format"] == "short" and p["plan"]["campaign"] == "Film Y PVOD"
    assert p["plan"]["pvod_date"] == dt.date(2023, 7, 11)
    (ln,) = p["lines"]
    assert ln["publisher"] == "FBIG" and ln["placement"] == "Vudu"
    assert (ln["start_date"], ln["end_date"]) == (dt.date(2023, 7, 11), dt.date(2023, 7, 23))
    assert ln["planned_spend"] == 75000 and ln["actual_spend"] == 64998.69
    assert p["plan"]["net_media_total"] == 75000 and p["plan"]["check_note"].startswith("OK")


def test_older_layouts():
    """Targeting rows with their own spend, unlabelled subtotals, Comcast lines left out of
    the MVPD subtotal, and year-specific header labels."""
    rows = [
        (None, "Campaign:", None, "Film Z"),
        (None, "PEW Release Date:", None, D(2023, 3, 14)),
        (None, "EST/BD Street Date:", None, D(2023, 4, 14)),
        (None, "Channel", "Publisher", "Placement Details", "Targeting", "Start Date",
         "Planned Spend", "Planned Impressions", "Pricing Model"),
        (None, "CTV/OTT", "Xbox", "Banners", "Fans", D(2023, 3, 14), 10, 100, "CPM"),
        (None, None, None, None, "Racing games", None, 10, 100, "CPM"),       # another targeting
        (None, None, "Xbox Total", None, None, None, 20, 200, None),
        (None, "MVPD", "DirecTV", "Pause Ads", "All", D(2023, 3, 14), 30, 300, "CPM"),
        (None, "MVPD", "Comcast", "Fixed Banners", "Prime", D(2023, 3, 14), 40, 400, "Flat"),
        (None, None, "MVPD TOTAL", None, None, None, 30, 300, None),          # Comcast left out
        (None, "Display", "Bleacher Report", "ROS", "Sports", D(2023, 3, 14), 5, 50, "CPM"),
        (None, None, None, None, None, None, 5, 50, None),                    # unlabelled sum
        (None, None, None, None, "Jumbotron Videos"),                         # ad-unit note
        (None, None, None, None, "NET MEDIA TOTAL", None, 55),
    ]
    p = parse_plan(rows, "2023_Film_Z.xlsx")
    lines = p["lines"]
    assert p["plan"]["pvod_date"] == dt.date(2023, 3, 14) and p["plan"]["est_date"] == dt.date(2023, 4, 14)
    assert [ln["publisher"] for ln in lines] == ["Xbox", "Xbox", "DirecTV", "Comcast", "Bleacher Report"]
    assert lines[1]["targeting"] == "Racing games" and lines[1]["start_date"] == dt.date(2023, 3, 14)
    assert [ln["in_plan_total"] for ln in lines] == [True, True, True, False, True]
    assert lines[4]["block"].endswith("(unlabelled subtotal)")
    assert p["plan"]["check_note"].startswith("OK")
