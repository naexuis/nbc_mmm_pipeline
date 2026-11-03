"""
Channel review: Planning confirms the channel of media that has no channel label yet.

Same cycle as the title review, in its own folder (config.CHANNEL_REVIEW_DIR):
  issued    a new workbook to send to Planning
  returned  where the filled-in workbook is uploaded
  imported  workbooks already saved (moved there automatically)

The workbook lists, for titles released on or after config.MMM_FIRST_RELEASE_DATE only:
  1 · Placements     Prisma placements with no label, grouped by film (all of them, unless
                     config.CHANNEL_REVIEW_MAX_PLACEMENTS sets a limit), each with a How sure group:
                     Likely right / Closer look / New site (see channels/suggest.py)
  2 · TV and radio   network / station codes with no label
  Labels in use      the labels already in the table (reference only)
Suggestions come from a model retrained on every confirmed label each time this runs.
Nothing is used until a person confirms it. Saved labels and every change (title_history,
table_name = 'channel_label') record who, when and why.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from google.cloud import bigquery
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

from us_hment_mmm import config
from us_hment_mmm.channels import labels as L
from us_hment_mmm.channels import suggest as S
from us_hment_mmm.gcp import bq_client
from us_hment_mmm.reference import review as R
from us_hment_mmm.reference import titles as T
from us_hment_mmm.reference.review_workbook import (
    ID_FONT,
    ID_HEADER,
    INPUT_FILL,
    _banner_and_header,
    _dropdown,
)

CHOICES = ("Accept", "Use my answer", "Not yet", "Remove placement")
INPUT_FIELDS = ("decision", "your_mmm", "your_plan", "reason")
LABEL_HELP = ("Your media-plan label and Your MMM label have a drop-down of the labels already "
              "used; you can also type a new one. Labels look like PARENT_CHILD, e.g. "
              "CTVOTT_AMAZON_FIRETV or SOCIAL_TIKTOK, and the parent (first part) must be one of: "
              + ", ".join(config.CHANNEL_PARENTS) + ".")
HOW_SURE = {"likely": "Likely right", "closer": "Closer look", "new_site": "New site",
            "spend_changed": "Spend changed", "removed_changed": "Removed, spend changed",
            "credit": "Credit"}
CHANGED = ("spend_changed", "removed_changed")
SPEND_CHANGE = 1.0      # a decided placement or code comes back when its spend moves more than this
PLACEMENT_COLS = [
    ("Client", "client", 8), ("Film", "film", 26), ("Release date", "release_date", 12),
    ("How sure", "how_sure", 13), ("Site", "site", 22),
    ("Placement name", "name", 60), ("Buy type", "buy_type", 11), ("Package type", "package_type", 13),
    ("First day", "first_day", 11), ("Last day", "last_day", 11), ("Spend", "spend", 13),
    ("Suggested media-plan label", "suggested_plan", 26), ("Suggested MMM label", "suggested_mmm", 26),
    ("Decision", "decision", 15), ("Your media-plan label (optional)", "your_plan", 24),
    ("Your MMM label", "your_mmm", 24), ("Reason (optional)", "reason", 30),
]
TV_COLS = [
    ("Code", "key", 22), ("Network or station", "network", 24), ("Media", "media", 14),
    ("Source", "sources", 10), ("Clients", "clients", 9), ("Film", "film", 30),
    ("Release date", "release_date", 12),
    ("First spot", "first_day", 11), ("Last spot", "last_day", 11),
    ("Spend (this film)", "spend", 14), ("Spend (all films on this code)", "code_spend", 16),
    ("Suggested label", "suggested_mmm", 24), ("Decision", "decision", 15),
    ("Your media-plan label (optional)", "your_plan", 24), ("Your MMM label", "your_mmm", 24),
    ("Reason (optional)", "reason", 30),
]
TABS = {
    "placements": ("1 · Placements",
                   "Prisma placements with no channel yet, grouped by film (largest spend first). "
                   "How sure says how far to trust the suggestion: Likely right = this site has "
                   "always had this label (about 99% right in testing); Closer look = the site has "
                   "been labelled more than one way (e.g. YouTube can be CTV, programmatic or "
                   "streaming), or this client has no confirmed labels yet; New site = the model "
                   "has never seen this site. Accept uses both suggested labels. Use my answer uses "
                   "what you pick or type under Your media-plan label (optional, if the media plan "
                   "said something else) and Your MMM label. Not yet (or empty) leaves it for a later "
                   "workbook. Remove placement (with a Reason) marks a row that is not media we model, "
                   "e.g. $0 added value.\n" + LABEL_HELP, PLACEMENT_COLS),
    "tv": ("2 · TV and radio",
           "TV, radio and MVPD network or station codes with no channel yet, one row per code and "
           "film (filter on Film to see one film). A code gets ONE label, used for every spot on "
           "it: decide on any one of its rows (if you decide on more than one row of the same code, "
           "they must agree). Same choices as tab 1. Local TV stations are suggested as "
           "SPOTTV_LOCALTV.\n" + LABEL_HELP, TV_COLS),
}
NEW_LABEL_NOTE = ("This label is not in the list yet. It will be added as a new label if it starts "
                  "with a known parent channel (see Labels in use).")


MODEL_RUNS_SCHEMA = [
    bigquery.SchemaField("event", "STRING", mode="REQUIRED"),       # issued | saved
    bigquery.SchemaField("run_at", "TIMESTAMP"),
    bigquery.SchemaField("workbook", "STRING"),
    bigquery.SchemaField("training_rows", "INT64"),
    bigquery.SchemaField("exact", "FLOAT64"),
    bigquery.SchemaField("parent", "FLOAT64"),
    bigquery.SchemaField("likely_share", "FLOAT64"),
    bigquery.SchemaField("likely_acc", "FLOAT64"),
    bigquery.SchemaField("closer_acc", "FLOAT64"),
    bigquery.SchemaField("new_acc", "FLOAT64"),
    bigquery.SchemaField("listed_placements", "INT64"),
    bigquery.SchemaField("listed_tv", "INT64"),
    bigquery.SchemaField("accepted", "INT64"),
    bigquery.SchemaField("changed", "INT64"),
]


# =============================================================================
# Pure helpers
# =============================================================================

def compact(s) -> str:
    return re.sub(r"[^0-9A-Z]+", "", str(s or "").upper())


def suggest_credits(placements: list[dict], training: list[dict]) -> None:
    """
    A credit (Prisma CreditPlacement) offsets spend, so it should get the channel of the same
    film's other placements on the same site: confirmed labels first, otherwise the suggestions
    for that film's other placements on this workbook.
    """
    from collections import Counter

    confirmed: dict[tuple, Counter] = {}
    for t in training:
        confirmed.setdefault((t.get("film"), S.site_key(t.get("site"))), Counter())[
            (t["mmm_label"], t.get("mediaplan_label") or t["mmm_label"])] += 1
    suggested: dict[tuple, Counter] = {}
    for it in placements:
        if it.get("package_type") != "CreditPlacement" and it.get("suggested_mmm"):
            suggested.setdefault((it.get("imdb_id"), S.site_key(it.get("site"))), Counter())[
                (it["suggested_mmm"], it.get("suggested_plan") or it["suggested_mmm"])] += 1
    for it in placements:
        if it.get("package_type") != "CreditPlacement" or it.get("group") in CHANGED:
            continue
        key = (it.get("imdb_id"), S.site_key(it.get("site")))
        pool = confirmed.get(key) or suggested.get(key)
        it["group"] = "credit"
        if not pool:
            it["credit_note"] = "no other placements of this film on this site"
            continue
        (mmm, plan), _n = pool.most_common(1)[0]
        it.update(suggested_mmm=mmm, suggested_plan=plan)
        if len(pool) > 1:
            it["credit_note"] = "this film's placements on this site have more than one label"


def tv_suggestion(code: str, network: str | None, media: str) -> str | None:
    media = (media or "").upper()
    if "SPOT TV" in media:
        return "SPOTTV_LOCALTV"
    if any(m in media for m in config.CHANNEL_RADIO_MEDIA):
        return f"AUDIO_{compact(network or code)}"
    if network and ("CABLE" in media or "SYNDICATION" in media):
        return f"CABLE_{compact(network)}"
    if network and "NETWORK" in media:
        return f"NETWORK_{compact(network)}"
    return None


def clean_label(value) -> str:
    return re.sub(r"_+", "_", re.sub(r"[\s\-]+", "_", R.text(value).upper())).strip("_")


def label_problem(label: str) -> str | None:
    if not label:
        return "the MMM label is empty"
    if L.parent_of(label) is None:
        return (f"'{label}' does not start with a known parent channel "
                f"({', '.join(config.CHANNEL_PARENTS)})")
    return None


def row_id(item: dict) -> str:
    return f"{item['key_type']}|{item['key']}"


def decide(items_by_id: dict, rows: list[dict], known_labels: set | None = None
           ) -> tuple[list[dict], list[str], list[dict]]:
    """
    Turn workbook rows into label changes. Returns (changes, problems, skipped).
    Each change: key_type, key, name, detail, mediaplan_label, mmm_label, decision, reason, where.
    """
    changes, problems, skipped = [], [], []
    decided: dict[tuple, tuple] = {}
    for r in rows:
        decision = R.text(r.get("decision")).lower()
        if decision in ("", "not yet"):
            continue
        where = f"{r['tab']} row {r['row']}"
        item = items_by_id.get(R.text(r.get("_id")))
        if item is None:
            skipped.append({"where": where, "why": "already has a channel, or no longer on the list"})
            continue
        if decision == "accept":
            mmm = clean_label(r.get("suggested_mmm")) or item.get("suggested_mmm")
            plan = clean_label(r.get("suggested_plan")) or item.get("suggested_plan") or mmm
            if not mmm:
                problems.append(f"{where}: there is no suggestion to accept; choose Use my answer "
                                "and type the label")
                continue
        elif decision == "remove placement":
            if not R.text(r.get("reason")):
                problems.append(f"{where}: Remove placement needs a Reason (e.g. $0 added value)")
                continue
            mmm = plan = config.CHANNEL_REMOVED
        elif decision == "use my answer":
            mmm = clean_label(r.get("your_mmm"))
            plan = (clean_label(r.get("your_plan")) or clean_label(r.get("suggested_plan"))
                    or item.get("suggested_plan") or mmm)
        else:
            problems.append(f"{where}: Decision should be Accept, Use my answer, Not yet or "
                            "Remove placement")
            continue
        bad = None if mmm == config.CHANNEL_REMOVED else (
            label_problem(mmm) or (label_problem(plan) and f"media-plan label: {label_problem(plan)}"))
        if bad:
            problems.append(f"{where}: {bad}")
            continue
        key = (item["key_type"], item["key"])
        if key in decided:
            if decided[key] != (mmm, plan):
                problems.append(f"{where}: {item['key']} has a different label on another row "
                                f"({decided[key][0]}); one code gets one label")
            continue
        decided[key] = (mmm, plan)
        reason = R.text(r.get("reason")) or (
            "Planning accepted the suggested label" if decision == "accept"
            else "Planning gave the label")
        changes.append({"key_type": item["key_type"], "key": item["key"], "name": item.get("site")
                        or item.get("network"), "detail": item.get("name") or item.get("media"),
                        "mediaplan_label": plan, "mmm_label": mmm, "decision": decision,
                        "reason": reason, "where": where,
                        "suggested_mmm": item.get("suggested_mmm"),
                        "spend": float(item.get("spend") or 0),
                        "new_label": (known_labels is not None and mmm not in known_labels
                                      and mmm != config.CHANNEL_REMOVED)})
    return changes, problems, skipped


# =============================================================================
# Workbook
# =============================================================================

def _label_dropdown(ws, list_ref: str, col_letter: str, last_row: int):
    from openpyxl.worksheet.datavalidation import DataValidation

    dv = DataValidation(type="list", formula1=list_ref, allow_blank=True, showErrorMessage=True,
                        errorStyle="information", errorTitle="New label", error=NEW_LABEL_NOTE)
    ws.add_data_validation(dv)
    dv.add(f"{col_letter}3:{col_letter}{max(last_row, 3)}")


def _summary_sheet(wb, items: list[dict], after: dict):
    """First tab: what is on this workbook, per film, with live counts of rows decided."""
    from openpyxl.styles import PatternFill

    ws = wb.create_sheet("Summary", 0)
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="1F3864")
    tab = TABS["placements"][0]
    col = {f: i for i, (_h, f, _w) in enumerate(PLACEMENT_COLS, start=1)}
    letter = lambda f: ws.cell(row=1, column=col[f]).column_letter  # noqa: E731
    film_rng = f"'{tab}'!${letter('film')}:${letter('film')}"
    dec_rng = f"'{tab}'!${letter('decision')}:${letter('decision')}"
    placements = [i for i in items if i["group"] != "tv"]
    tv = [i for i in items if i["group"] == "tv"]
    films: dict[str, dict] = {}
    for i in placements:
        f = films.setdefault(i.get("film") or "(no film)", {"client": set(), "n": 0, "likely": 0,
                                                            "closer": 0, "new_site": 0,
                                                            "removed_changed": 0, "spend_changed": 0,
                                                            "credit": 0,
                                                            "spend": 0.0,
                                                            "release": i.get("release_date")})
        f["client"].add(i.get("client") or "")
        f["n"] += 1
        f[i["group"]] += 1
        f["spend"] += float(i.get("spend") or 0)
    tv_codes: dict[str, set] = {}
    for i in tv:
        tv_codes.setdefault(i.get("film") or "", set()).add(i["key"])

    ws["A1"] = "Channel review summary"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = ("What is on this workbook and what is left. 'Placements decided' and 'Still to "
                "review' (and the same columns per film) update by themselves as rows are filled in "
                "on 1 · Placements. Reference only: changes made here are not read.")
    ws["A2"].alignment = Alignment(wrap_text=True)
    ws.merge_cells("A2:M2")
    ws.row_dimensions[2].height = 30
    top = [("Films on this workbook", len(films)),
           ("Placements on 1 · Placements", len(placements)),
           ("Placements decided", None),
           ("Placements still to review", None),
           ("Films with placements still to review", None),
           ("TV and radio codes on 2 · TV and radio", len({i["key"] for i in tv}))]
    if after["placements"]:
        top.append(("Waiting for a later workbook",
                    f"{after['placements']:,} smaller placements across {after['films']:,} films "
                    "(nothing to do with them now)"))
    first_film_row = 4 + len(top) + 3
    last_film_row = first_film_row + max(len(films), 1) - 1
    for r, (label, value) in enumerate(top, start=4):
        ws.cell(row=r, column=1, value=label).font = bold
        if label == "Placements decided":
            value = f"=SUM(J{first_film_row}:J{last_film_row})"
        elif label == "Placements still to review":
            value = "=B5-B6"
        elif label == "Films with placements still to review":
            value = f'=COUNTIF(K{first_film_row}:K{last_film_row},">0")'
        ws.cell(row=r, column=2, value=value)
    header = ["Film", "Release date", "Client", "Placements", "Likely right", "Closer look",
              "New site", "Credit", "Spend changed", "Decided", "Still to review", "Spend",
              "TV / radio codes"]
    hr = first_film_row - 1
    ws.cell(row=hr - 1, column=1, value="By film (largest spend first)").font = bold
    for j, h in enumerate(header, start=1):
        c = ws.cell(row=hr, column=j, value=h)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), head_fill
    decided = " + ".join(f'COUNTIFS({film_rng},A{{r}},{dec_rng},"{d}")'
                         for d in ("Accept", "Use my answer", "Remove placement"))
    for r, (film, f) in enumerate(sorted(films.items(), key=lambda kv: -kv[1]["spend"]),
                                  start=first_film_row):
        release = f["release"].isoformat()[:10] if hasattr(f["release"], "isoformat") \
            else (f["release"] or "")
        values = [film, release, ", ".join(sorted(f["client"])), f["n"], f["likely"],
                  f["closer"], f["new_site"], f["credit"], f["spend_changed"] + f["removed_changed"],
                  "=" + decided.format(r=r), f"=D{r}-J{r}",
                  f["spend"], len(tv_codes.get(film, ()))]
        for j, v in enumerate(values, start=1):
            c = ws.cell(row=r, column=j, value=v)
            if j == 12:
                c.number_format = "$#,##0"
    for letter_, width in zip("ABCDEFGHIJKLM",
                              (44, 13, 10, 12, 12, 12, 10, 9, 13, 10, 15, 14, 16)):
        ws.column_dimensions[letter_].width = width
    ws.freeze_panes = ws.cell(row=first_film_row, column=1)
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0


def _instructions_sheet(wb):
    """Second tab: what each choice means, and a filled-in example."""
    from openpyxl.styles import PatternFill

    ws = wb.create_sheet("How to fill this in", 1)
    bold = Font(bold=True)
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3864")
    wrap = Alignment(wrap_text=True, vertical="top")
    r = 1

    def line(text="", font=None, height=None):
        nonlocal r
        c = ws.cell(row=r, column=1, value=text)
        c.alignment = wrap
        if font:
            c.font = font
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        if height:
            ws.row_dimensions[r].height = height
        r += 1

    def table(header, rows, inputs=()):
        nonlocal r
        for j, h in enumerate(header, start=1):
            c = ws.cell(row=r, column=j, value=h)
            c.font, c.fill, c.alignment = head_font, head_fill, wrap
        r += 1
        for row in rows:
            for j, v in enumerate(row, start=1):
                c = ws.cell(row=r, column=j, value=v)
                c.alignment = wrap
                if j - 1 in inputs:
                    c.fill = INPUT_FILL
            r += 1
        r += 1

    line("How to fill in this workbook", Font(bold=True, size=14))
    line("Each row is media spend that has no channel yet. Please tell us which channel it "
         "really ran as. Only fill in the yellow columns; leave everything else as it is.",
         height=32)
    r += 1
    line("1. Decision: pick one in the Decision column", bold)
    table(["Decision", "What it means and what to fill in"], [
        ("Accept", "The suggested labels are right. Nothing else to fill in (the Reason is "
                   "optional)."),
        ("Use my answer", "The suggestion is wrong, or there is none. Pick the right label under "
                          "Your MMM label (from the drop-down, or type a new one: see section 4). "
                          "Your media-plan label is optional: fill it in only if the media plan "
                          "said something else."),
        ("Not yet", "You are not sure, or someone else should decide. Fill in nothing; the row "
                    "comes back on a later workbook. (An empty Decision means the same.)"),
        ("Remove placement", "This row is not media we model, e.g. a $0 added-value or make-good "
                             "placement, a test or a duplicate. No label is needed, but a Reason "
                             "is (e.g. \"$0 added value\"). The row is kept in the data, marked "
                             "REMOVED and never counted in any channel. It is not asked about again unless its "
                             "spend changes later (then it comes back as \"Removed, spend changed\"). "
                             "Tip: sort by Spend, select the $0 rows and fill Remove placement down."),
    ])
    line("2. How sure (on 1 · Placements): how far to trust the suggestion", bold)
    table(["How sure", "What it means"], [
        ("Likely right", "This site has always had this label before. Usually right: a quick check."),
        ("Closer look", "This site has been labelled more than one way before (e.g. YouTube can be "
                        "CTV, programmatic or streaming), or this client has no confirmed labels "
                        "yet. Please check how it actually ran."),
        ("New site", "We have never seen this site. The suggestion is only a starting point."),
        ("Credit", "A credit (negative amount) gives back money on spend that already ran, so it "
                   "should have the same channel as that spend. The suggestion is the label of the "
                   "same film's other placements on the same site. If it says \"check\", that film "
                   "has more than one label on this site, or no other placements there."),
        ("Spend changed (was $X)", "This placement already had a label, but its spend has "
                                   "changed since it was decided. The suggestion is its current "
                                   "label: Accept keeps it, or Use my answer to change it. Until "
                                   "then it keeps its current label."),
        ("Removed, spend changed", "This placement was removed before (e.g. it was $0), but its "
                                   "spend has changed since. Please decide again: give it a label, "
                                   "or Remove placement again."),
    ])
    line("3. Labels", bold)
    line("A label looks like PARENT_CHILD, e.g. CTVOTT_AMAZON_FIRETV or SOCIAL_TIKTOK. The parent "
         "(the first part) is the channel in the model and must be one of: "
         + ", ".join(config.CHANNEL_PARENTS) + ". The Labels in use tab shows how labels have "
         "been used so far.", height=48)
    r += 1
    line("Example: 1 · Placements (only the columns you fill in are shown)", bold)
    table(["Placement (site)", "Suggested MMM label", "Decision", "Your media-plan label",
           "Your MMM label", "Reason"], [
        ("Facebook", "SOCIAL_FACEBOOK", "Accept", "", "", ""),
        ("YouTube", "CTVOTT_YOUTUBE", "Use my answer", "", "PROGRAMMATIC_YOUTUBE",
         "Bought through Google DV360, not as CTV"),
        ("The Trade Desk", "CTVOTT_YOUTUBE", "Use my answer", "PROGRAMMATIC_TRADEDESK",
         "PROGRAMMATIC_TRADEDESK", "New label: Trade Desk programmatic"),
        ("Bloody Disgusting", "DISPLAY_VIDEO_BLOODY_DISGUSTING", "Not yet", "", "",
         "Asking the buyer"),
        ("Samsung (added value, $0)", "CTVOTT_SAMSUNG", "Remove placement", "", "",
         "$0 added value"),
    ], inputs=(2, 3, 4, 5))
    line("Example: 2 · TV and radio", bold)
    line("A code (network or station) has one row per film but gets ONE label for all of them. "
         "Decide on any one of its rows; leave its other rows empty (if you fill more than one, "
         "they must agree).", height=32)
    table(["Code", "Film", "Suggested label", "Decision", "Your MMM label"], [
        ("FOXXN", "PRESSURE", "CABLE_FOXNEWS", "Accept", ""),
        ("FOXXN", "SONG SUNG BLUE", "CABLE_FOXNEWS", "", ""),
        ("STATION WNBC-TV", "HAMNET", "SPOTTV_LOCALTV", "Accept", ""),
    ], inputs=(3, 4))
    line("4. Adding a new label", bold)
    table(["Step", "What to do"], [
        ("1", "Check the drop-down and the Labels in use tab first: the label may already exist "
              "under a slightly different name."),
        ("2", "Choose Use my answer in the Decision column."),
        ("3", "Type the new label in Your MMM label as PARENT_CHILD: the parent from the list in "
              "section 3, then the partner name without spaces, e.g. PROGRAMMATIC_TRADEDESK or "
              "CTVOTT_TUBI. Upper or lower case and spaces are fine; they are tidied up."),
        ("4", "Excel shows a note \"This label is not in the list yet\". Click OK to keep it."),
        ("5", "Say in the Reason that it is a new label and why, e.g. \"New: Trade Desk "
              "programmatic\". New labels are listed separately in the check before anything is "
              "saved, and a label whose parent is not on the list is sent back to be fixed."),
    ])
    line("5. When you are done", bold)
    line("Save the file as .xlsx and keep its name. Do not rename or delete tabs or columns, and "
         "do not edit the Row ID column. Send it back to the person who sent it to you.",
         height=32)
    for letter_, width in zip("ABCDEF", (20, 44, 16, 26, 26, 40)):
        ws.column_dimensions[letter_].width = width
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0


def write_workbook(items: list[dict], pairs: dict, folder: Path, after: dict | None = None) -> Path:
    """pairs: {(media-plan label, MMM label): count} from the label table; after: what is left
    for later workbooks ({placements, films})."""
    mmm_list = sorted({m for (_p, m) in pairs if m}, key=lambda x: (L.parent_of(x) or "", x))
    plan_list = sorted({p for (p, _m) in pairs if p})
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"channel-review-{datetime.now():%Y%m%dT%H%M%S}.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    for group, (tab, banner, cols) in TABS.items():
        ws = wb.create_sheet(tab)
        all_cols = cols + [(ID_HEADER, "_id", 30)]
        _banner_and_header(ws, banner, all_cols)
        rows = [i for i in items if (i["group"] == "tv") == (group == "tv")]
        for r, it in enumerate(rows, start=3):
            for j, (_h, field, _w) in enumerate(all_cols, start=1):
                if field == "_id":
                    v = row_id(it)
                elif field == "how_sure":
                    v = HOW_SURE.get(it["group"], "")
                    if it["group"] in CHANGED:
                        v = f"{v} (was ${float(it.get('was_spend') or 0):,.0f})"
                    elif it["group"] == "credit" and it.get("credit_note"):
                        v = f"{v} (check: {it['credit_note']})"
                elif field == "network" and it.get("was_decided"):
                    what = ("removed before" if it.get("current_mmm") == config.CHANNEL_REMOVED
                            else f"was {it.get('current_mmm')}")
                    v = (f"{it.get('network') or ''} ({what}; spend changed from "
                         f"${float(it.get('was_spend') or 0):,.0f})").strip()
                else:
                    v = None if field in INPUT_FIELDS else it.get(field)
                if isinstance(v, (date, datetime)):
                    v = v.isoformat()[:10]
                c = ws.cell(row=r, column=j, value=v)
                c.number_format = "$#,##0" if field in ("spend", "code_spend") else "@"
                if field in ("spend", "code_spend") and v is not None:
                    c.value = float(v)
                if field in INPUT_FIELDS:
                    c.fill = INPUT_FILL
                if field == "_id":
                    c.font = ID_FONT
                if field == "name":
                    c.alignment = Alignment(wrap_text=True, vertical="top")
        col = {f: ws.cell(row=2, column=j).column_letter
               for j, (_h, f, _w) in enumerate(all_cols, start=1)}
        _dropdown(ws, CHOICES, col["decision"], len(rows) + 2)
        _label_dropdown(ws, f"Lists!$A$2:$A${max(len(mmm_list), 1) + 1}", col["your_mmm"],
                        len(rows) + 2)
        _label_dropdown(ws, f"Lists!$B$2:$B${max(len(plan_list), 1) + 1}", col["your_plan"],
                        len(rows) + 2)
        if rows:
            last = ws.cell(row=2, column=len(all_cols)).column_letter
            ws.auto_filter.ref = f"A2:{last}{len(rows) + 2}"
        else:
            ws.cell(row=3, column=1, value="Nothing to decide on this tab.")
    ws = wb.create_sheet("Labels in use")
    cols = [("Media-plan label", "plan", 34), ("MMM label", "mmm", 34), ("Parent", "parent", 16),
            ("Used on", "n", 10)]
    _banner_and_header(ws, "How labels are used today: each row is a media-plan label and the MMM "
                       "label it was given, with how many placements or codes have that pair. "
                       "Reference only: changes made here are not read.", cols)
    for i, ((plan, mmm), n) in enumerate(sorted((pairs or {}).items(),
                                                key=lambda kv: (kv[0][0] or "", -kv[1])), start=3):
        ws.cell(row=i, column=1, value=plan)
        ws.cell(row=i, column=2, value=mmm)
        ws.cell(row=i, column=3, value=L.parent_of(mmm) or "")
        ws.cell(row=i, column=4, value=int(n))
    ws.auto_filter.ref = f"A2:D{max(len(pairs or {}) + 2, 3)}"
    # the drop-down lists live on a hidden tab, so nobody reads them as pairs
    lists = wb.create_sheet("Lists")
    lists.sheet_state = "hidden"
    lists.cell(row=1, column=1, value="MMM labels")
    lists.cell(row=1, column=2, value="Media-plan labels")
    for i, label in enumerate(mmm_list, start=2):
        lists.cell(row=i, column=1, value=label)
    for i, label in enumerate(plan_list, start=2):
        lists.cell(row=i, column=2, value=label)
    _summary_sheet(wb, items, after or {"placements": 0, "films": 0})
    _instructions_sheet(wb)
    wb.active = 0
    wb.save(path)
    return path


def read_workbook(path: Path) -> tuple[list[dict], list[str]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    rows, problems = [], []
    for group, (tab, _banner, cols) in TABS.items():
        if tab not in wb.sheetnames:
            continue
        it = wb[tab].iter_rows(values_only=True)
        next(it, None)
        header = [R.text(h) for h in next(it, ())]
        want = cols + [(ID_HEADER, "_id", 0)]
        index = {}
        for h, field, _w in want:
            if h in header:
                index[field] = header.index(h)
            elif field in INPUT_FIELDS[:2] or field == "_id":
                problems.append(f"The '{tab}' tab has no '{h}' column. Do not rename or delete the "
                                "yellow columns or the Row ID column.")
        for n, values in enumerate(it, start=3):
            row = {f: (values[i] if i < len(values) else None) for f, i in index.items()}
            if any(R.text(row.get(f)) for f in INPUT_FIELDS):
                rows.append({**row, "tab": tab, "row": n})
    return rows, problems


# =============================================================================
# BigQuery
# =============================================================================

def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _q(client, sql):
    return [dict(r.items()) for r in client.query(sql).result()]


def gather(client) -> dict:
    labels_t = _core(config.CHANNEL_LABEL_TABLE)
    title_t = _core(config.TITLE_TABLE)
    first = config.MMM_FIRST_RELEASE_DATE
    # column added after the seed (safe to repeat)
    client.query(f"ALTER TABLE `{labels_t}` ADD COLUMN IF NOT EXISTS decided_spend FLOAT64").result()
    placements = _q(client, f"""
        WITH p AS (
          SELECT placement_number AS key, ANY_VALUE(business_code) AS client,
                 ANY_VALUE(imdb_id) AS imdb_id, ANY_VALUE(site) AS site,
                 ANY_VALUE(placement_name) AS name, ANY_VALUE(buy_type) AS buy_type,
                 ANY_VALUE(package_type) AS package_type, MIN(spend_date) AS first_day,
                 MAX(spend_date) AS last_day, SUM(spend_usd) AS spend
          FROM `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` WHERE link = 'film' GROUP BY 1)
        SELECT 'prisma_placement' AS key_type, p.*, t.title_name AS film, t.release_date,
               l.key IS NOT NULL AS was_decided, l.decided_spend AS was_spend,
               l.mmm_label AS current_mmm, l.mediaplan_label AS current_plan
        FROM p JOIN `{title_t}` t USING (imdb_id)
        LEFT JOIN `{labels_t}` l ON l.key_type = 'prisma_placement' AND l.key = p.key
        WHERE t.release_date >= DATE '{first}'
          AND (l.key IS NULL OR ABS(p.spend - IFNULL(l.decided_spend, p.spend)) > {SPEND_CHANGE})
        ORDER BY ABS(p.spend) DESC""")
    tv_base = f"""
        SELECT m.*, t.title_name, t.release_date
        FROM `{_core(config.MEDIA_TV_CORE_TABLE)}` m JOIN `{title_t}` t USING (imdb_id)
        WHERE m.link = 'film' AND t.release_date >= DATE '{first}'"""
    tv = _q(client, f"""
        WITH c AS (
          SELECT channel_code AS key, STRING_AGG(DISTINCT media_name, ', ') AS media,
                 STRING_AGG(DISTINCT source, ', ') AS sources,
                 STRING_AGG(DISTINCT business_code, ', ') AS clients, SUM(spend_usd) AS spend
          FROM ({tv_base}) GROUP BY 1)
        SELECT 'tv_network' AS key_type, c.*, l.key IS NOT NULL AS was_decided,
               l.decided_spend AS was_spend, l.mmm_label AS current_mmm,
               l.mediaplan_label AS current_plan
        FROM c LEFT JOIN `{labels_t}` l ON l.key_type = 'tv_network' AND l.key = c.key
        WHERE l.key IS NULL OR ABS(c.spend - IFNULL(l.decided_spend, c.spend)) > {SPEND_CHANGE}
        ORDER BY ABS(c.spend) DESC""")
    listed_codes = {r["key"] for r in tv}
    tv_films: dict[str, list[dict]] = {}
    for r in _q(client, f"""
            SELECT channel_code AS key, title_name AS film, ANY_VALUE(release_date) AS release_date,
                   MIN(spend_date) AS first_day,
                   MAX(spend_date) AS last_day, SUM(spend_usd) AS spend
            FROM ({tv_base}) GROUP BY 1, 2 ORDER BY 1, ABS(SUM(spend_usd)) DESC"""):
        if r["key"] in listed_codes:
            tv_films.setdefault(r["key"], []).append(r)
    training = _q(client, f"""
        SELECT l.key, l.name AS site, l.detail AS name, l.mediaplan_label, l.mmm_label,
               ANY_VALUE(p.imdb_id) AS film, ANY_VALUE(p.business_code) AS client
        FROM `{labels_t}` l
        LEFT JOIN `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` p ON p.placement_number = l.key
        WHERE l.key_type = 'prisma_placement' AND l.mmm_label != '{config.CHANNEL_REMOVED}'
        GROUP BY 1, 2, 3, 4, 5""")
    all_labels = {(r["key_type"], r["key"]): r for r in _q(client, f"SELECT * FROM `{labels_t}`")}
    pairs: dict = {}
    for r in all_labels.values():
        if r.get("mmm_label") and r["mmm_label"] != config.CHANNEL_REMOVED:
            k = (r.get("mediaplan_label") or r["mmm_label"], r["mmm_label"])
            pairs[k] = pairs.get(k, 0) + 1
    try:
        runs = _q(client, f"SELECT * FROM `{_core(config.CHANNEL_MODEL_RUNS_TABLE)}` "
                          "ORDER BY run_at DESC LIMIT 20")
    except Exception:  # noqa: BLE001 - no runs yet
        runs = []
    return {"placements": placements, "tv": tv, "tv_films": tv_films, "training": training,
            "all_labels": all_labels, "pairs": pairs, "runs": runs}


def _log_run(client, row: dict) -> None:
    payload = [{f.name: (row.get(f.name).isoformat() if isinstance(row.get(f.name), datetime)
                         else row.get(f.name)) for f in MODEL_RUNS_SCHEMA}]
    client.load_table_from_json(payload, _core(config.CHANNEL_MODEL_RUNS_TABLE),
                                job_config=bigquery.LoadJobConfig(
                                    schema=MODEL_RUNS_SCHEMA,
                                    schema_update_options=[
                                        bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
                                    write_disposition=bigquery.WriteDisposition.WRITE_APPEND)).result()


def _apply(client, all_labels: dict, changes: list[dict], workbook: str) -> int:
    who, now = R.current_user(), datetime.now(timezone.utc)
    history = []
    for c in changes:
        k = (c["key_type"], c["key"])
        old = all_labels.get(k, {})
        c["decided_spend"] = c.get("spend")
        for f in L.HISTORY_FIELDS:
            if old.get(f) != c.get(f):
                history.append({"change_id": uuid.uuid4().hex, "table_name": config.CHANNEL_LABEL_TABLE,
                                "record_key": f"{k[0]}|{k[1]}", "field": f,
                                "old_value": None if old.get(f) is None else str(old.get(f)),
                                "new_value": None if c.get(f) is None else str(c.get(f)),
                                "reason": f"{c['reason']} ({workbook})", "changed_by": who,
                                "changed_at": now.isoformat()})
        all_labels[k] = {"key_type": k[0], "key": k[1], "name": c["name"], "detail": c["detail"],
                         "mediaplan_label": c["mediaplan_label"], "mmm_label": c["mmm_label"],
                         "decided_spend": c.get("spend"),
                         "source": "channel review", "reason": f"{c['reason']} ({workbook})",
                         "updated_at": now, "updated_by": who}
    T._write(client, config.CHANNEL_LABEL_TABLE, L.LABEL_SCHEMA, list(all_labels.values()))
    client.load_table_from_json(history, _core(config.TITLE_HISTORY_TABLE),
                                job_config=bigquery.LoadJobConfig(
                                    schema=T.HISTORY_SCHEMA,
                                    write_disposition=bigquery.WriteDisposition.WRITE_APPEND)).result()
    return len(history)


# =============================================================================
# Run
# =============================================================================

def network_dictionary() -> dict:
    import json

    return {**json.loads((L.SEED_DIR / L.NETWORK_FILE).read_text()), **L.NETWORK_EXTRA}


def baseline(dry_run: bool = True, client=None) -> int:
    """Record today's spend (decided_spend) on labels that have none yet, so that only spend
    changes from now on send a row back for review. Run once after adding the column."""
    client = client or bq_client()
    labels_t, title_t = _core(config.CHANNEL_LABEL_TABLE), _core(config.TITLE_TABLE)
    client.query(f"ALTER TABLE `{labels_t}` ADD COLUMN IF NOT EXISTS decided_spend FLOAT64").result()
    spend = {("prisma_placement", r["key"]): float(r["spend"] or 0) for r in _q(client, f"""
        SELECT placement_number AS key, SUM(spend_usd) AS spend
        FROM `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` WHERE link = 'film' GROUP BY 1""")}
    spend.update({("tv_network", r["key"]): float(r["spend"] or 0) for r in _q(client, f"""
        SELECT m.channel_code AS key, SUM(m.spend_usd) AS spend
        FROM `{_core(config.MEDIA_TV_CORE_TABLE)}` m JOIN `{title_t}` t USING (imdb_id)
        WHERE m.link = 'film' AND t.release_date >= DATE '{config.MMM_FIRST_RELEASE_DATE}'
        GROUP BY 1""")})
    rows = _q(client, f"SELECT * FROM `{labels_t}`")
    filled = 0
    for r in rows:
        k = (r["key_type"], r["key"])
        if r.get("decided_spend") is None and k in spend:
            r["decided_spend"] = spend[k]
            filled += 1
    snap = _q(client, f"SELECT MAX(snapshot_date) AS d FROM "
                      f"`{_core(config.MEDIA_PRISMA_CORE_TABLE)}`")
    snap = snap[0]["d"] if snap else None
    print(f"Starting point: spend in the core media tables (media snapshot {snap}).")
    print(f"{filled:,} of {len(rows):,} labels get that spend as their starting point "
          f"({len(rows) - filled:,} have one already or no spend on 2023+ films).")
    if dry_run:
        print("Check only: nothing was written. To save, run again with --baseline --write.")
        return filled
    T._write(client, config.CHANNEL_LABEL_TABLE, L.LABEL_SCHEMA, rows)
    print(f"Saved. From now on, a labelled placement or code whose spend changes by more than "
          f"${SPEND_CHANGE:,.0f} goes back on the channel review workbook.")
    return filled


def _quality(training: list[dict]) -> dict | None:
    return S.evaluate(training, L.parent_of)


def run(dry_run: bool = True, root: str | Path | None = None, client=None) -> dict:
    client = client or bq_client()
    root = Path(root or config.CHANNEL_REVIEW_DIR)
    folders = R.review_folders(root)
    data = gather(client)
    waiting = R.returned_workbooks(folders["returned"])
    result = {"root": root, "changes": [], "skipped": [], "problems": [], "workbook": None,
              "remaining_placements": len(data["placements"]), "remaining_tv": len(data["tv"]),
              "runs": data["runs"], "quality": None}
    d = network_dictionary()
    for it in data["tv"]:
        it["network"] = L.network_label(it["key"], (it["media"] or "").split(", ")[0], d)[0]
        if it.get("was_decided") and it.get("current_mmm") != config.CHANNEL_REMOVED:
            it["suggested_mmm"] = it["current_mmm"]
            it["suggested_plan"] = it.get("current_plan") or it["current_mmm"]
        else:
            it["suggested_mmm"] = it["suggested_plan"] = tv_suggestion(it["key"], it["network"],
                                                                       it["media"])
        it["group"] = "tv"

    if not waiting:
        S.suggest(S.train(data["training"]), data["placements"])
        for it in data["placements"]:
            if it.get("was_decided"):
                if it.get("current_mmm") == config.CHANNEL_REMOVED:
                    it["group"] = "removed_changed"
                else:
                    it.update(group="spend_changed", suggested_mmm=it["current_mmm"],
                              suggested_plan=it.get("current_plan") or it["current_mmm"])
        suggest_credits(data["placements"], data["training"])
        cap = config.CHANNEL_REVIEW_MAX_PLACEMENTS
        listed = data["placements"] if cap is None else data["placements"][:cap]
        film_spend = {}
        for i in listed:
            film_spend[i.get("film")] = film_spend.get(i.get("film"), 0) + abs(float(i["spend"] or 0))
        items = sorted(listed, key=lambda i: (-film_spend[i.get("film")], i.get("film") or "",
                                              -abs(float(i["spend"] or 0))))
        for code in data["tv"]:
            for f in data["tv_films"].get(code["key"], [{}]):
                items.append({**code, "film": f.get("film"), "release_date": f.get("release_date"),
                              "first_day": f.get("first_day"),
                              "last_day": f.get("last_day"), "spend": f.get("spend"),
                              "code_spend": code["spend"]})
        later = data["placements"][len(listed):]
        after = {"placements": len(later), "films": len({i.get("film") for i in later})}
        path = write_workbook(items, data["pairs"], folders["issued"], after)
        q = _quality(data["training"]) or {}
        result.update(status="ISSUED", workbook=path.name, quality=q,
                      listed={"likely": sum(i["group"] == "likely" for i in items),
                              "closer": sum(i["group"] == "closer" for i in items),
                              "new_site": sum(i["group"] == "new_site" for i in items),
                              "changed": sum(i["group"] in CHANGED for i in items),
                              "credit": sum(i["group"] == "credit" for i in items),
                              "tv": len(data["tv"])})
        _log_run(client, {"event": "issued", "run_at": datetime.now(timezone.utc),
                          "workbook": path.name, **q,
                          "listed_placements": len(listed), "listed_tv": len(data["tv"])})
        return result

    path = waiting[0]
    result["workbook"] = path.name
    rows, problems = read_workbook(path)
    for it in data["placements"]:
        it.setdefault("group", "new_site")
    items_by_id = {row_id(i): i for i in data["placements"] + data["tv"]}
    known = {r["mmm_label"] for r in data["all_labels"].values() if r.get("mmm_label")}
    changes, more, skipped = decide(items_by_id, rows, known)
    result.update(changes=changes, skipped=skipped, problems=problems + more)
    if result["problems"]:
        result["status"] = "PROBLEMS"
        return result
    if dry_run:
        result["status"] = "READY" if changes else "NOTHING"
        return result
    if changes:
        _apply(client, data["all_labels"], changes, path.name)
    _log_run(client, {"event": "saved", "run_at": datetime.now(timezone.utc), "workbook": path.name,
                      "accepted": sum(c["decision"] == "accept" for c in changes),
                      "changed": sum(c["decision"] != "accept" for c in changes)})
    moved = R.move_to_imported(path, folders["imported"])
    result.update(status="APPLIED" if changes else "IMPORTED_NOTHING", moved=moved.name)
    return result


# =============================================================================
# Notebook display
# =============================================================================

def _pct(x) -> str:
    return "–" if x is None else f"{float(x):.1%}"


def status_markdown(result: dict):
    from IPython.display import Markdown

    root, name, n = result["root"], result["workbook"], len(result["changes"])
    remaining = (f"Still without a channel (titles released {config.MMM_FIRST_RELEASE_DATE[:4]} or "
                 f"later): **{result['remaining_placements']:,}** Prisma placements and "
                 f"**{result['remaining_tv']:,}** TV / radio codes.")
    upload = ("**Where to upload it:** " + R.where_to_find("returned", root)
              + "  \nUse the **Upload files** button on that page. If a copy with the same name "
              "is already there, replace it.")
    s = result["status"]
    if s == "ISSUED":
        lst = result["listed"]
        if not sum(lst.values()):
            head = "### ✅ Nothing needs a channel"
            body = ("Every placement and TV / radio code for titles in the MMM has a channel. "
                    f"A workbook was still made (`{name}`), but there is nothing in it to decide.")
        else:
            head = "### ✅ New workbook issued"
            body = (f"**File:** `{name}`  \n**On it:** "
                    f"{lst['likely'] + lst['closer'] + lst['new_site'] + lst['changed'] + lst['credit']:,} "
                    "placements on "
                    f"1 · Placements ({lst['likely']:,} likely right, {lst['closer']:,} closer look, "
                    f"{lst['new_site']:,} new site, {lst['credit']:,} credits"
                    + (f", {lst['changed']:,} decided before but spend changed"
                       if lst['changed'] else "")
                    + f") and {lst['tv']:,} codes on 2 · TV and radio."
                    "\n\n"
                    f"**Where to find it:** {R.where_to_find('issued', root)}  \n"
                    "Click the file name to download it, then send it to Planning.\n\n"
                    "**What Planning does in Excel:** on each tab, choose a Decision in the yellow "
                    "columns: **Accept** the suggested labels, or **Use my answer** and pick (or "
                    "type) the label. Leave rows they are not sure about empty. Save it as .xlsx (keep the "
                    f"name).\n\n{upload}\n\n**Then:** come back here and **Run All** (still a "
                    "check only).")
    elif s == "PROBLEMS":
        head = f"### 🛑 STOP — please fix these rows in `{name}`"
        body = ("\n".join(f"- {p}" for p in result["problems"])
                + "\n\nNothing was changed. Fix the rows in Excel, upload the file again, and "
                f"**Run All**.\n\n{upload}")
    elif s == "READY":
        head = f"### ✅ PASS — `{name}`: {n} label(s) ready"
        body = ("Check the labels in **Step 3** and anything in **Step 4**. If they are right, set "
                "`DRY_RUN = False` in Step 1 and **Run All** again.")
    elif s == "NOTHING":
        head = f"### ℹ️ `{name}` has no decisions yet"
        body = "No row has Accept or Use my answer. Nothing was changed."
    elif s == "APPLIED":
        head = f"### ✅ SAVED — {n} label(s) from `{name}`"
        body = (f"The workbook moved to the `imported` folder as `{result['moved']}`.\n\n"
                "**Next:** set `DRY_RUN = True` again in Step 1, then run **06_Build_Core** "
                "if you want the QA numbers refreshed (the labels already show in `media_daily`).")
    else:
        head = f"### ✅ `{name}` moved to imported"
        body = "It had no decisions, so nothing was changed."
    return Markdown(f"{head}\n\n{body}\n\n{remaining}")


def changes_table(changes: list[dict]):
    import pandas as pd

    from us_hment_mmm.display import table_or_note

    df = pd.DataFrame([{"Workbook row": c["where"], "Item": c["key"], "Name": c["name"],
                        "MMM label": c["mmm_label"], "Media-plan label": c["mediaplan_label"],
                        "Decision": c["decision"][:1].upper() + c["decision"][1:],
                        "New label?": "new label" if c.get("new_label") else "",
                        "Reason": c["reason"]}
                       for c in changes])
    return table_or_note(df, "No changes.")


def notes_markdown(skipped: list[dict]):
    from IPython.display import Markdown

    if not skipped:
        return Markdown("ℹ️ Every decided row was used.")
    return Markdown("\n".join(f"- {s['where']}: {s['why']}" for s in skipped))


def quality_markdown(result: dict):
    """The 'How good are the suggestions?' step."""
    from IPython.display import Markdown

    issued = [r for r in result["runs"] if r["event"] == "issued" and r.get("exact") is not None]
    saved = [r for r in result["runs"] if r["event"] == "saved"]
    q = result.get("quality") or (issued[0] if issued else None)
    if not q or q.get("exact") is None:
        return Markdown("ℹ️ Not enough confirmed labels yet to test the suggestions.")
    lines = [f"Trained on **{q['training_rows']:,}** confirmed placements.",
             f"Tested on films the model had not seen: **{_pct(q['exact'])}** of labels exactly "
             f"right, **{_pct(q['parent'])}** of parent channels right.",
             f"How sure: \"Likely right\" **{_pct(q['likely_acc'])}** right · \"Closer look\" "
             f"**{_pct(q['closer_acc'])}** · \"New site\" **{_pct(q.get('new_acc'))}**."]
    if saved and (saved[0].get("accepted") or 0) + (saved[0].get("changed") or 0):
        a, c = saved[0].get("accepted") or 0, saved[0].get("changed") or 0
        lines.append(f"**Last round:** Planning accepted **{a} of {a + c}** suggestions "
                     f"({a / (a + c):.0%}).")
    # runs were read before this run was logged: when this run issued a workbook, the
    # previous run is the newest logged one; otherwise the newest logged one is this result
    if result.get("quality"):
        previous = issued[0] if issued else None
    else:
        previous = issued[1] if len(issued) > 1 else None
    if previous and previous.get("exact") is not None and \
            q["exact"] < float(previous["exact"]) - 0.02:
        lines.append(f"⚠️ This is lower than the previous run ({_pct(previous['exact'])}). New "
                     "kinds of placements may be harder to suggest; Planning's answers will help.")
    return Markdown("  \n".join(lines))
