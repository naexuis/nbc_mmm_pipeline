"""
Load the media plans (Excel, one per film) from GCS into BigQuery (us_hment_mmm_raw).

Planning's media plans list every line bought for a film, including what never reaches the
buying systems: value-added media (free placements, bonus impressions) and Comcast SIF (funded
by Comcast, not billed). These lines are kept out of the plans' own totals, so this is the only
place they can be found.

Source: gs://<config.MEDIA_PLAN_BUCKET>/<config.MEDIA_PLAN_PREFIX>*.xlsx (e.g. 2025_Wicked For
Good.xlsx). Every run reads every plan in the folder and replaces the tables, so a corrected or
re-copied plan simply replaces the old one.

Most plans are one sheet: a header block of labelled fields ("Campaign:", "PEW Date:", "EST Date:",
...) above a table whose header row holds Channel, Publisher, Placement Details, Start Date,
Planned Spend, Planned Impressions, Pricing Model, ... Column positions differ between years, so
columns are found by name. Each publisher block ends in a subtotal row ("Roku Total", "Comcast
SIF Total"); after the last block come the plan totals (NET MEDIA TOTAL, fees, GRAND TOTAL, co-op).

Small titles often have a short plan instead (no plan details table): a 'Title:' and a summary
table (Platform - Type, Flight, Budget, Est Impressions, Est CPM, Amount Spent, Delivered
Impressions) with a Total row. Those load as plan_format 'short', one line per summary row.
A film can have more than one plan (e.g. a PVOD plan and an HE plan).

Tables
  media_plan        one row per plan: file, campaign, dates, line counts, plan totals and a
                    check that the lines add up to the plan's NET MEDIA TOTAL
  media_plan_line   one row per plan line, with block (the subtotal that closes it), the
                    value-added and SIF flags, and any value stated in the text ("AV $20K")
  media_plan_total  one row per plan total (NET MEDIA TOTAL, AGENCY FEE TOTAL, GRAND TOTAL, ...)

Rules
  - value added: "Added Value" as the planned spend or pricing model, or a placement / targeting
    text that starts with "Added Value" or "AV:", or carries "(AV $...)"
  - SIF: "SIF" in the publisher or in the block's subtotal (on newer plans the lines just say
    "Comcast" and only the subtotal says "Comcast SIF Total")
  - a line with a blank window / channel / publisher / site takes it from the line above
  - a row with nothing planned (no start date, no spend, no impressions) is skipped

Run (from /mnt/UniversalPicturesMMM):
    python -m us_hment_mmm.media.load_plans            # check only: reads the plans, writes nothing
    python -m us_hment_mmm.media.load_plans --write    # replace the three tables
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import re

from us_hment_mmm import config

HEADER_FIELDS = {          # header label (lower case, no colon) -> field
    "campaign": "campaign", "pvod release date": "pvod_date", "pew date": "pvod_date",
    "pvod date": "pvod_date", "pew 2 date": "pew2_date", "est street date": "est_date",
    "est date": "est_date", "pvod street date": "pvod_date", "title": "campaign",
    "vod start date": "vod_date", "bd/dvd street date": "bd_dvd_date", "bd/dvd date": "bd_dvd_date",
    "vod release date": "vod_date", "vod date": "vod_date", "approval date": "approval_date",
    "genre": "genre", "rating": "rating", "reporting comp": "reporting_comp",
}
COLUMNS = {                # table header (lower case) -> field
    "campaign window": "window", "channel": "plan_channel", "publisher": "publisher",
    "site(s)": "site", "site section": "site_section", "placement details": "placement",
    "targeting": "targeting", "start date": "start_date", "end date": "end_date",
    "planned spend": "planned_spend", "actual spend": "actual_spend",
    "planned impressions": "planned_impressions", "planned impression": "planned_impressions",
    "actual impressions": "actual_impressions",
    "pricing model": "pricing_model", "ecpm": "ecpm", "sov": "sov", "platform": "platform",
    "ad unit": "ad_unit", "ad size": "ad_size",
}
CARRY = ("window", "plan_channel", "publisher", "site", "site_section")
TOTAL_WORDS = ("NET MEDIA", "AGENCY FEE", "AD SERVING", "GRAND TOTAL", "CO-OP", "COOP",
               "CONTINGENCY")
AV_START = re.compile(r"^\s*(ADDED\s*VALUE|AV\s*:)", re.I)
AV_AMOUNT = re.compile(r"\bAV\s*:?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(K|M)?", re.I)

LINE_SCHEMA = [
    ("plan_file", "STRING"), ("campaign", "STRING"), ("line_no", "INT64"), ("sheet_row", "INT64"),
    ("window", "STRING"), ("plan_channel", "STRING"), ("publisher", "STRING"), ("site", "STRING"),
    ("site_section", "STRING"), ("placement", "STRING"), ("targeting", "STRING"),
    ("start_date", "DATE"), ("end_date", "DATE"), ("planned_spend", "FLOAT64"),
    ("actual_spend", "FLOAT64"), ("planned_impressions", "FLOAT64"),
    ("actual_impressions", "FLOAT64"), ("pricing_model", "STRING"), ("ecpm", "FLOAT64"),
    ("sov", "STRING"), ("platform", "STRING"), ("ad_unit", "STRING"), ("ad_size", "STRING"),
    ("block", "STRING"), ("block_total", "FLOAT64"), ("in_plan_total", "BOOL"),
    ("outside_reason", "STRING"), ("is_value_added", "BOOL"), ("is_sif", "BOOL"),
    ("stated_value", "FLOAT64"), ("spend_text", "STRING"),
]
PLAN_SCHEMA = [
    ("plan_file", "STRING"), ("plan_year", "INT64"), ("plan_format", "STRING"),
    ("campaign", "STRING"),
    ("pvod_date", "DATE"), ("pew2_date", "DATE"), ("est_date", "DATE"), ("bd_dvd_date", "DATE"),
    ("vod_date", "DATE"), ("approval_date", "DATE"), ("genre", "STRING"), ("rating", "STRING"),
    ("reporting_comp", "STRING"), ("lines", "INT64"), ("value_added_lines", "INT64"),
    ("sif_lines", "INT64"), ("planned_spend", "FLOAT64"), ("sif_spend", "FLOAT64"),
    ("outside_total_lines", "INT64"), ("outside_total_spend", "FLOAT64"),
    ("planned_impressions", "FLOAT64"), ("value_added_impressions", "FLOAT64"),
    ("net_media_total", "FLOAT64"), ("co_op_total", "FLOAT64"), ("grand_total", "FLOAT64"),
    ("check_note", "STRING"), ("loaded_at", "TIMESTAMP"),
]
TOTAL_SCHEMA = [("plan_file", "STRING"), ("label", "STRING"), ("amount", "FLOAT64")]


# --- reading one plan -----------------------------------------------------------------------

def _text(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _text(v).replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return None


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(_text(v)[:10], fmt).date()
        except ValueError:
            pass
    return None


def stated_value(*texts) -> float | None:
    """A value written in the text, e.g. 'Landing Page (AV $20K)' -> 20000."""
    for t in texts:
        m = AV_AMOUNT.search(_text(t))
        if m:
            x = float(m.group(1).replace(",", ""))
            return x * {"K": 1e3, "M": 1e6}.get((m.group(2) or "").upper(), 1)
    return None


def header_field(label: str) -> str | None:
    """Header label -> field, tolerant of wording that changes by year ('PEW Release Date:',
    'EST/BD Street Date:', 'BD/DVDStreet Date:', 'VOD Start Date:', 'Title:')."""
    key = _text(label).rstrip(":").strip().lower()
    if key in HEADER_FIELDS:
        return HEADER_FIELDS[key]
    u = key.upper()
    if "DATE" in u:
        if "PEW 2" in u or "PEW2" in u:
            return "pew2_date"
        if re.match(r"^(PEW|PVOD)\b", u):
            return "pvod_date"
        if re.match(r"^EST\b", u):
            return "est_date"
        if re.match(r"^(BD|DVD)", u):
            return "bd_dvd_date"
        if re.match(r"^VOD\b", u):
            return "vod_date"
        if "APPROVAL" in u:
            return "approval_date"
    return None


def _first_label(row) -> str:
    return next((_text(v) for v in row if _text(v)), "")


def parse_plan(rows: list[tuple], plan_file: str) -> dict:
    """Parse one plan sheet (list of row tuples). Returns {'plan', 'lines', 'totals'}."""
    hdr_i = next((i for i, r in enumerate(rows)
                  if {"channel", "publisher"} <= {_text(v).lower() for v in r}), None)
    if hdr_i is None:
        raise ValueError(f"{plan_file}: no table header row with Channel and Publisher")

    plan = {"plan_file": plan_file, "plan_format": "full"}
    m = re.match(r"^(\d{4})", plan_file.rsplit("/", 1)[-1])
    plan["plan_year"] = int(m.group(1)) if m else None
    for r in rows[:hdr_i]:
        cells = [v for v in r if _text(v)]
        for j, v in enumerate(cells[:-1]):
            field = header_field(v) if _text(v).endswith(":") else None
            if field:
                val = cells[j + 1]
                plan.setdefault(field, _date(val) if field.endswith("_date") else _text(val))

    cols = {}
    for j, v in enumerate(rows[hdr_i]):
        key = _text(v).lower()
        if key in COLUMNS and COLUMNS[key] not in cols:
            cols[COLUMNS[key]] = j

    def get(r, f):
        j = cols.get(f)
        return r[j] if j is not None and j < len(r) else None

    label_end = cols.get("placement", cols.get("start_date", len(rows[hdr_i])))
    lines, totals, pending, last, in_footer = [], [], [], {}, False
    for i, r in enumerate(rows[hdr_i + 1:], hdr_i + 2):
        if not any(_text(v) for v in r):
            continue
        start, channel = _date(get(r, "start_date")), _text(get(r, "plan_channel"))
        placement = _text(get(r, "placement"))
        spend_raw, imps = get(r, "planned_spend"), _number(get(r, "planned_impressions"))
        if not start and not channel and not placement:
            label, amount = _first_label(r), _number(spend_raw)
            left = _first_label(r[:label_end])            # text left of the placement column
            if label and (any(w in label.upper() for w in TOTAL_WORDS)
                          or re.search(r"\bFEES?\b", label.upper())):
                totals.append({"plan_file": plan_file, "label": label, "amount": amount})
                in_footer = True
                continue
            elif in_footer:
                continue                                  # below the plan totals: no lines
            elif left and amount is not None:             # block subtotal: closes the block
                for ln in pending:
                    ln["block"], ln["block_total"] = left, amount
                    ln["block_row"] = i
                pending = []
                continue
            elif amount is not None and pending and not any(
                    _text(v) and _number(v) is None and _date(v) is None for v in r):
                # an unlabelled subtotal (numbers only): closes the block, named after it
                name = f"{pending[-1].get('publisher') or 'Block'} (unlabelled subtotal)"
                for ln in pending:
                    ln["block"], ln["block_total"], ln["block_row"] = name, amount, i
                pending = []
                continue
            elif not (amount is not None and lines):
                continue          # ad-unit notes and other text: nothing planned here
            # else: a continuation row - another targeting of the line above with its own
            # spend (e.g. Fast X Xbox banners split by audience); it inherits the rest below
            start = lines[-1]["start_date"]
        if not start and _number(spend_raw) is None and imps is None:
            continue                                      # placeholder row, nothing planned
        ln = {"plan_file": plan_file, "campaign": plan.get("campaign"), "sheet_row": i}
        for f in COLUMNS.values():
            if f in cols:
                ln[f] = get(r, f)
        for f in CARRY:
            if not _text(ln.get(f)) and last.get(f):
                ln[f] = last[f]
        if not channel and not placement and lines:          # continuation row
            for f in ("placement", "pricing_model", "end_date"):
                if not _text(ln.get(f)):
                    ln[f] = lines[-1].get(f)
        for f in ("window", "plan_channel", "publisher", "site", "site_section", "placement",
                  "targeting", "pricing_model", "sov", "platform", "ad_unit", "ad_size"):
            ln[f] = _text(ln.get(f)) or None
        ln["start_date"], ln["end_date"] = _date(start), _date(ln.get("end_date"))
        ln["spend_text"] = _text(spend_raw) if _number(spend_raw) is None else None
        for f in ("planned_spend", "actual_spend", "planned_impressions", "actual_impressions",
                  "ecpm"):
            ln[f] = _number(ln.get(f))
        texts = (ln["placement"], ln["targeting"])
        ln["is_value_added"] = bool(
            "ADDED VALUE" in (_text(spend_raw).upper() + " " + _text(ln["pricing_model"]).upper())
            or any(AV_START.match(t or "") for t in texts) or stated_value(*texts) is not None)
        ln["stated_value"] = stated_value(*texts)
        ln["block"], ln["block_total"], ln["block_row"] = None, None, None
        last = {f: ln.get(f) for f in CARRY}
        lines.append(ln)
        pending.append(ln)
    for k, ln in enumerate(lines, 1):
        ln["line_no"] = k
        ln["is_sif"] = bool(re.search(r"\bSIF\b", f"{ln['publisher'] or ''} {ln['block'] or ''}",
                                      re.I))
    mark_outside_totals(lines)

    def total(*words):
        for t in totals:
            if all(w in t["label"].upper() for w in words) and t["amount"] is not None:
                return t["amount"]
        return None

    plan["lines"] = len(lines)
    plan["value_added_lines"] = sum(ln["is_value_added"] for ln in lines)
    plan["sif_lines"] = sum(ln["is_sif"] for ln in lines)
    plan["planned_spend"] = sum(ln["planned_spend"] or 0 for ln in lines)
    plan["sif_spend"] = sum(ln["planned_spend"] or 0 for ln in lines if ln["is_sif"])
    plan["outside_total_spend"] = sum(ln["planned_spend"] or 0 for ln in lines
                                      if not ln["in_plan_total"])
    plan["outside_total_lines"] = sum(not ln["in_plan_total"] for ln in lines)
    plan["planned_impressions"] = sum(ln["planned_impressions"] or 0 for ln in lines)
    plan["value_added_impressions"] = sum(ln["planned_impressions"] or 0 for ln in lines
                                          if ln["is_value_added"])
    plan["net_media_total"] = total("NET MEDIA")
    plan["co_op_total"] = next((t["amount"] for t in totals
                                if re.search(r"CO-?OP TOTAL", t["label"].upper())
                                and not re.match(r"^(PEW|HE|PVOD)", t["label"].upper())), None)
    plan["grand_total"] = total("GRAND TOTAL")
    plan["check_note"] = check_note(plan)
    return {"plan": plan, "lines": lines, "totals": totals}


def _subset_matching(xs: list[dict], target: float, tol: float = 2.0) -> list[dict] | None:
    """Lines whose planned spend adds up to target (within tol); small lists only."""
    from itertools import combinations

    if len(xs) > 18:
        return None
    for k in range(1, len(xs) + 1):
        for combo in combinations(xs, k):
            if abs(sum(x["planned_spend"] or 0 for x in combo) - target) <= tol:
                return list(combo)
    return None


def mark_outside_totals(lines: list[dict]) -> None:
    """Lines the plan keeps out of its own totals. SIF lines (labelled) are always outside.
    A block whose other lines add up to more than its subtotal row: the lines of one
    publisher that make up exactly the difference are outside the plan total (on older plans
    unbilled Comcast lines are not always labelled SIF)."""
    for ln in lines:
        ln["in_plan_total"] = not ln["is_sif"]
        ln["outside_reason"] = "SIF" if ln["is_sif"] else None
    blocks: dict[tuple, list[dict]] = {}
    for ln in lines:
        if ln["block"] is not None:
            blocks.setdefault((ln["block"], ln["block_row"]), []).append(ln)
    for (name, _), bl in blocks.items():
        subtotal = bl[0].get("block_total")
        inside = [x for x in bl if x["in_plan_total"]]
        gap = sum(x["planned_spend"] or 0 for x in inside) - (subtotal or 0)
        if subtotal is None or abs(gap) <= 2 or gap < 0:
            continue
        by_pub: dict[str, list[dict]] = {}
        for x in inside:
            by_pub.setdefault(_text(x["publisher"]).upper(), []).append(x)
        for pub, xs in sorted(by_pub.items(), key=lambda kv: "COMCAST" not in kv[0]):
            hit = _subset_matching(xs, gap)
            if hit:
                for x in hit:
                    x["in_plan_total"] = False
                    x["outside_reason"] = (f"not in the '{name}' subtotal (block lines "
                                           f"${gap + subtotal:,.0f} vs subtotal ${subtotal:,.0f})")
                break


def check_note(plan: dict) -> str:
    """Do the lines add up to NET MEDIA TOTAL? SIF is kept out of the plan totals; co-op may
    sit inside a block but outside net media."""
    net = plan.get("net_media_total")
    if net is None:
        return "No NET MEDIA TOTAL found (is the plan complete?)"
    outside = plan.get("outside_total_spend", plan.get("sif_spend") or 0)
    paid = plan["planned_spend"] - outside
    tol = max(5, 0.0001 * abs(net))                      # rounding inside the plan
    for label, x in (("lines in the plan total", paid),
                     ("lines in the plan total, less co-op", paid - (plan.get("co_op_total") or 0))):
        if abs(x - net) <= tol:
            return f"OK: {label} = NET MEDIA TOTAL"
    return f"Check: lines in the plan total ${paid:,.0f} vs NET MEDIA TOTAL ${net:,.0f}"


SHORT_COLUMNS = {           # short plans (small titles): one summary table, no plan details
    "platform - type": "platform_type", "flight": "flight", "budget": "planned_spend",
    "est impressions": "planned_impressions", "est cpm": "ecpm", "amount spent": "actual_spend",
    "delivered impressions": "actual_impressions",
}


def _flight(text: str, year: int | None):
    """'4/28 - 6/05' -> two dates in the plan's year (the end rolls into the next year if
    it is before the start)."""
    parts = re.findall(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", _text(text))
    out = []
    for mo, d, y in parts[:2]:
        yy = int(y) + (2000 if y and len(y) == 2 else 0) if y else year
        try:
            out.append(dt.date(yy, int(mo), int(d)) if yy else None)
        except ValueError:
            out.append(None)
    while len(out) < 2:
        out.append(None)
    if out[0] and out[1] and out[1] < out[0] and not parts[1][2]:
        out[1] = out[1].replace(year=out[1].year + 1)
    return out[0], out[1]


def parse_short_plan(rows: list[tuple], plan_file: str) -> dict:
    """Short plan used for small titles: 'Title:' and a summary table with Platform - Type,
    Flight, Budget, Est Impressions, Est CPM, Amount Spent, Delivered Impressions; a 'Total'
    row. (The audience table below it is not loaded.)"""
    hdr_i = next((i for i, r in enumerate(rows)
                  if {"platform - type", "budget"} <= {_text(v).lower() for v in r}), None)
    if hdr_i is None:
        raise ValueError(f"{plan_file}: no plan details table and no short-plan summary")
    plan = {"plan_file": plan_file, "plan_format": "short"}
    m = re.match(r"^(\d{4})", plan_file.rsplit("/", 1)[-1])
    plan["plan_year"] = int(m.group(1)) if m else None
    cols = {SHORT_COLUMNS[_text(v).lower()]: j for j, v in enumerate(rows[hdr_i])
            if _text(v).lower() in SHORT_COLUMNS}
    first = min(cols.values())
    for r in rows[hdr_i:hdr_i + 6]:                 # labels sit left of the summary table
        cells = [v for v in r[:first] if _text(v)]
        for j, v in enumerate(cells[:-1]):
            field = header_field(v) if _text(v).endswith(":") else None
            if field:
                val = cells[j + 1]
                plan.setdefault(field, _date(val) if field.endswith("_date") else _text(val))
    year = plan.get("pvod_date").year if plan.get("pvod_date") else plan["plan_year"]

    def get(r, f):
        j = cols.get(f)
        return r[j] if j is not None and j < len(r) else None

    lines, totals = [], []
    for i, r in enumerate(rows[hdr_i + 1:], hdr_i + 2):
        label = _text(get(r, "platform_type"))
        if not label:
            if not any(_text(v) for v in r):
                break                                   # the summary table has ended
            continue
        if label.lower() == "total" or any(_text(v).lower() == "total" for v in r[:first + 1]):
            totals.append({"plan_file": plan_file, "label": "NET MEDIA TOTAL",
                           "amount": _number(get(r, "planned_spend"))})
            continue
        if _number(get(r, "planned_spend")) is None:
            continue
        pub, _, kind = label.partition(" - ")
        start, end = _flight(get(r, "flight"), year)
        ln = {"plan_file": plan_file, "campaign": plan.get("campaign"), "sheet_row": i,
              "publisher": pub.strip() or None, "placement": kind.strip() or label,
              "start_date": start, "end_date": end, "block": None,
              "is_value_added": False, "is_sif": False, "stated_value": None, "spend_text": None,
              "block_total": None, "in_plan_total": True, "outside_reason": None}
        for f in ("planned_spend", "actual_spend", "planned_impressions", "actual_impressions",
                  "ecpm"):
            ln[f] = _number(get(r, f))
        lines.append(ln)
    for k, ln in enumerate(lines, 1):
        ln["line_no"] = k
    plan.update(lines=len(lines), value_added_lines=0, sif_lines=0,
                planned_spend=sum(ln["planned_spend"] or 0 for ln in lines), sif_spend=0,
                outside_total_lines=0, outside_total_spend=0,
                planned_impressions=sum(ln["planned_impressions"] or 0 for ln in lines),
                value_added_impressions=0,
                net_media_total=totals[0]["amount"] if totals else None,
                co_op_total=None, grand_total=None)
    plan["check_note"] = check_note(plan)
    return {"plan": plan, "lines": lines, "totals": totals}


def read_xlsx(data: bytes, plan_file: str) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sheets = [list(ws.iter_rows(values_only=True)) for ws in wb.worksheets]
    for parse in (parse_plan, parse_short_plan):    # full plan details first, else short plan
        for rows in sheets:
            try:
                return parse(rows, plan_file)
            except ValueError:
                pass
    raise ValueError(f"{plan_file}: no plan details table and no short-plan summary found")


# --- run ------------------------------------------------------------------------------------

def _value(v, kind: str):
    if v is None:
        return None
    if kind == "DATE":
        d = _date(v)
        return d.isoformat() if d else None
    if kind == "TIMESTAMP" and isinstance(v, dt.datetime):
        return v.isoformat()
    return v


def _rows_for(schema, records):
    return [{n: _value(r.get(n), kind) for n, kind in schema} for r in records]


def run(write: bool = False) -> dict:
    from google.cloud import bigquery

    from us_hment_mmm.gcp import bq_client, gcs_client

    blobs = [b for b in gcs_client().list_blobs(config.MEDIA_PLAN_BUCKET,
                                               prefix=config.MEDIA_PLAN_PREFIX)
             if b.name.lower().endswith(".xlsx") and not b.name.rsplit("/", 1)[-1].startswith("~")]
    plans, lines, totals, problems = [], [], [], []
    now = dt.datetime.now(dt.timezone.utc)
    for b in sorted(blobs, key=lambda b: b.name):
        name = b.name.rsplit("/", 1)[-1]
        try:
            p = read_xlsx(b.download_as_bytes(), name)
        except Exception as exc:                  # noqa: BLE001 — report and carry on
            problems.append(f"{name}: {exc}")
            continue
        p["plan"]["loaded_at"] = now
        plans.append(p["plan"])
        lines += p["lines"]
        totals += p["totals"]
    if write and plans:
        client = bq_client()
        for table, schema, recs in ((config.MEDIA_PLAN_TABLE, PLAN_SCHEMA, plans),
                                    (config.MEDIA_PLAN_LINE_TABLE, LINE_SCHEMA, lines),
                                    (config.MEDIA_PLAN_TOTAL_TABLE, TOTAL_SCHEMA, totals)):
            job = client.load_table_from_json(
                _rows_for(schema, recs), config.table_id(config.DATASET_RAW, table),
                job_config=bigquery.LoadJobConfig(
                    schema=[bigquery.SchemaField(n, t) for n, t in schema],
                    write_disposition="WRITE_TRUNCATE"))
            job.result()
    return {"plans": plans, "lines": lines, "totals": totals, "problems": problems,
            "files": len(blobs), "written": bool(write and plans)}


def summary_frame(result: dict):
    import pandas as pd

    rows = [{"Plan file": p["plan_file"], "Format": p.get("plan_format"),
             "Campaign": p.get("campaign"),
             "PVOD": p.get("pvod_date"), "EST": p.get("est_date"), "Lines": p["lines"],
             "Value-added lines": p["value_added_lines"], "SIF lines": p["sif_lines"],
             "SIF spend": p["sif_spend"],
             "Other outside total": p["outside_total_spend"] - p["sif_spend"],
             "Net media (plan)": p["net_media_total"],
             "Check": p["check_note"]} for p in result["plans"]]
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Load the media plans from GCS into BigQuery.")
    ap.add_argument("--write", action="store_true", help="replace the media plan tables")
    import pandas as pd
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 60)
    res = run(write=ap.parse_args().write)
    print(f"{res['files']} plan files in gs://{config.MEDIA_PLAN_BUCKET}/{config.MEDIA_PLAN_PREFIX}; "
          f"read {len(res['plans'])}, {len(res['lines']):,} lines.\n")
    if res["plans"]:
        print(summary_frame(res).to_string(index=False))
    for p in res["problems"]:
        print(f"⚠️ {p}")
    print("\nSaved." if res["written"] else "\nCheck only: nothing was written. To save, add --write.")


if __name__ == "__main__":
    main()
