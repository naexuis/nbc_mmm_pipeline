"""
Write and read the title review workbook (Excel).

Every tab starts with a plain-English banner (row 1) saying what the problem is and what to
do, then the column headers (row 2), then the rows (row 3 on).

  Read me                 how the workbook works
  1 · Product codes       media product codes not linked to a film
  2 · Flagged films       films flagged when the title table was built
  3 · Dates               NBC or Finance dates that differ from the current date
  4 · Revenue product IDs product IDs NBC tags with a film but not linked yet
  Add films, Change films, Map codes, Map product IDs   changes typed by hand
  Films now               the title table today (reference only, never read back)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from us_hment_mmm import config
from us_hment_mmm.reference import review as R

BANNER_FILL = PatternFill("solid", fgColor="DDEBF7")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
ID_FONT = Font(color="808080", size=8)
INPUT_ROWS = 200            # input rows on the hand-entry tabs
ID_HEADER = "Row ID (do not edit)"

CHOICES = ("Accept", "Use my answer", "Keep as is")
NOTE_CHOICES = ("Resolved", "Not yet")
DECISION_MAP = {"accept": "approve", "use my answer": "use instead", "keep as is": "reject",
                "resolved": "approve", "not yet": ""}

# Review tabs: section -> (tab, banner, [(header, show-field or input name, width)], choices)
REVIEW_TABS = {
    1: ("1 · Product codes",
        "These media product codes are not linked to any film, so their spend is not counted for "
        "a film. For each row choose one: Accept uses the proposed fix. Use my answer uses what "
        "you type under Your answer: the film's IMDb ID (like tt1234567), or non_film. "
        "Keep as is leaves the code unlinked. Leave Decision empty to decide later.\n"
        "non_film = the code is not a film (drafting/draft, anniversary, brand or TV campaigns); "
        "its spend is kept but never counted for a film. It replaces the old tt00000000.\n"
        "If the film is not on the Films now tab, also add it on the Add films tab.",
        [("Business", "business", 13), ("Code", "code", 8), ("Name in Prisma", "name", 26),
         ("Media rows", "rows", 11), ("Proposed fix", "proposed", 46), ("Why", "why", 46),
         ("Decision", "decision", 15), ("Your answer (IMDb ID or non_film)", "answer", 22),
         ("Reason", "reason", 40)],
        CHOICES),
    2: ("2 · Flagged films",
        "These films were flagged when the title table was built. Read the problem, check it "
        "(fix it on the Add films, Change films or Map codes tab if needed), then choose "
        "Resolved to clear the flag. Not yet leaves it flagged.",
        [("Film", "film", 34), ("IMDb ID", "imdb_id", 13), ("Problem found", "problem", 70),
         ("Decision", "decision", 13), ("Reason", "reason", 50)],
        NOTE_CHOICES),
    3: ("3 · Dates",
        "NBC or Finance has a date that differs from the current date (the date the model uses "
        "now, or empty if there is none yet). Accept replaces the current date with the proposed "
        "date. Keep as is keeps the current date (you will not be asked about this proposed date "
        "again). Use my answer lets you type the right date (like 2026-06-30) under Your answer.\n"
        "Amazon check (PVOD rows): when the Amazon MMT team's data has the film, it shows the "
        "first week of Amazon sales and whether the proposed date fits it. It is a hint only.",
        [("Film", "film", 34), ("IMDb ID", "imdb_id", 13), ("In MMM", "in_mmm", 12),
         ("Date", "date_type", 13), ("Current date (used now)", "now", 14),
         ("Proposed date", "proposed", 14), ("From", "source", 14),
         ("Amazon check", "amazon", 44), ("Decision", "decision", 15),
         ("Your answer (date)", "answer", 14), ("Reason", "reason", 40)],
        CHOICES),
    4: ("4 · Revenue product IDs",
        "NBC's revenue data tags these product IDs with a film, but they are not linked yet, so "
        "their revenue is not counted for the film. Check that the name in revenue is that film "
        "(not a bundle or a different film), then choose Accept to link them. Use my answer lets "
        "you type a different IMDb ID. Keep as is leaves them unlinked.",
        [("Product ID", "product_id", 12), ("Name in revenue", "revenue_name", 34),
         ("Royalty (USD)", "royalty", 14), ("Sales", "sales", 24),
         ("NBC tags it as", "tagged", 34), ("Proposed fix", "proposed", 40), ("Why", "why", 46),
         ("Decision", "decision", 15), ("Your answer (IMDb ID)", "answer", 18),
         ("Reason", "reason", 40)],
        CHOICES),
    5: ("5 · NBC genre",
        "NBC's master genre for these films differs from the film's current genre (or the film "
        "has none yet). Accept uses NBC's genre. Keep as is keeps the current genre (you will "
        "not be asked about this NBC genre again). Use my answer lets you type a genre.",
        [("Film", "film", 34), ("IMDb ID", "imdb_id", 13), ("In MMM", "in_mmm", 12),
         ("Genre", "genre_type", 12), ("Current genre", "now", 22),
         ("NBC genre", "proposed", 22), ("Decision", "decision", 15),
         ("Your answer (genre)", "answer", 22), ("Reason", "reason", 40)],
        CHOICES),
}
INPUT_FIELDS = {"decision", "answer", "reason"}

# Hand-entry tabs: tab -> (data key, banner, [(header, field, width)])
HAND_TABS = {
    "Add films": ("add_films",
                  "Add a film that is not in the title table yet (check Films now first). "
                  "IMDb ID and Title are required; dates look like 2026-06-30.",
                  [("IMDb ID", "imdb_id", 14), ("Title", "title_name", 34),
                   ("Distributor", "distributor", 18), ("Release date", "release_date", 14),
                   ("PVOD date", "pvod_date", 14), ("EST date", "est_date", 14),
                   ("Reason", "reason", 40)]),
    "Change films": ("change_films",
                     "Change one value of a film already in the title table. Field is one of: "
                     + ", ".join(R.SETTABLE_FIELDS) + ". in_mmm takes yes or no (the Reason "
                     "becomes the In MMM reason). Leave New value empty to clear review_note.",
                     [("Film (IMDb ID)", "film", 16), ("Field", "field", 16),
                      ("New value", "new_value", 30), ("Reason", "reason", 40)]),
    "Map codes": ("map_codes",
                  "Link any media product code to a film by hand, including one that is already "
                  "linked to the wrong film. Business code is ZUE, ZUV, ZFF or ZFE. Film is an "
                  "IMDb ID, or non_film when the code is not a film (drafting, anniversary, brand "
                  "or TV campaigns; replaces the old tt00000000).",
                  [("Business code", "business_code", 14), ("Product code", "product_code", 14),
                   ("Film (IMDb ID or non_film)", "film", 26), ("Reason", "reason", 40)]),
    "Map product IDs": ("map_product_ids",
                        "Link any revenue product ID to a film by hand.",
                        [("Product ID", "product_id", 14), ("Film (IMDb ID)", "film", 16),
                         ("Reason", "reason", 40)]),
}
READ_ME = [
    "TITLE REVIEW WORKBOOK",
    "",
    "This workbook lists everything about the film list that needs a person's decision.",
    "Every decision is saved in BigQuery with your reason, so this file is only the form.",
    "",
    "HOW TO USE IT",
    "  1. Work through the numbered tabs. Each one starts with a blue box saying what the",
    "     problem is and what each choice does. Fill in the yellow columns only.",
    "  2. Every row you decide needs a Reason (a few words is fine).",
    "  3. Leave Decision empty for anything you want to decide later; it will be on the next",
    "     workbook again.",
    "  4. Save as .xlsx, upload it to the 'returned' folder, and run notebook 05_Review_Titles.",
    "",
    "NON_FILM",
    "  Type non_film for a product code that is not a film: drafting/draft, anniversary,",
    "  brand or TV campaigns. Its spend is kept but never counted for a film. non_film",
    "  replaces the old tt00000000 placeholder (tt00000000 is also understood).",
    "",
    "NEW FILMS",
    "  When you link a code or product ID to a film that is not on the Films now tab, also",
    "  add the film on the Add films tab (or the check run will ask you to).",
    "",
    "THE TABS",
    "  1 · Product codes        media codes not linked to a film",
    "  2 · Flagged films        films with a problem found when the title table was built",
    "  3 · Dates                NBC or Finance dates that differ from the current date",
    "  4 · Revenue product IDs  revenue codes not linked to a film yet",
    "  5 · NBC genre            NBC master genres that differ from the film's genre",
    "  Add films / Change films / Map codes / Map product IDs   fixes you type by hand",
    "  Films now                the current film list, to look up IMDb IDs (not read back)",
    "",
    "SAFETY",
    "  Nothing is saved until the notebook runs with DRY_RUN = False, and it shows every",
    "  change first. Rows whose values changed after this workbook was issued are skipped.",
]


def _banner_and_header(ws, banner: str, cols):
    ws.cell(row=1, column=1, value=banner)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    c = ws.cell(row=1, column=1)
    c.fill, c.font = BANNER_FILL, Font(size=11)
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[1].height = 15 * (banner.count("\n") + 1) + 45
    for j, (name, _field, width) in enumerate(cols, start=1):
        h = ws.cell(row=2, column=j, value=name)
        h.font, h.fill = Font(bold=True, color="FFFFFF"), HEADER_FILL
        h.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[h.column_letter].width = width
    ws.row_dimensions[2].height = 30
    ws.freeze_panes = "A3"
    ws.page_setup.orientation = "landscape"          # prints one tab across a page
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0


def _dropdown(ws, choices, col_letter, last_row):
    dv = DataValidation(type="list", formula1='"' + ",".join(choices) + '"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"{col_letter}3:{col_letter}{max(last_row, 3)}")


def write_workbook(items: list[dict], titles: dict, folder: str | Path | None = None,
                   aliases: dict | None = None) -> Path:
    folder = Path(folder or config.REVIEW_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"title-review-{datetime.now():%Y%m%dT%H%M%S}.xlsx"
    wb = Workbook()

    ws = wb.active
    ws.title = "Read me"
    for i, line in enumerate(READ_ME, start=1):
        ws.cell(row=i, column=1, value=line).font = Font(bold=(i == 1 or line.isupper()))
    ws.column_dimensions["A"].width = 100

    for section, (tab, banner, cols, choices) in REVIEW_TABS.items():
        ws = wb.create_sheet(tab)
        all_cols = cols + [(ID_HEADER, "_id", 26)]
        _banner_and_header(ws, banner, all_cols)
        rows = [i for i in items if i["section"] == section]
        for r, it in enumerate(rows, start=3):
            for j, (_h, field, _w) in enumerate(all_cols, start=1):
                if field == "_id":
                    v = f"{it['key']}#{R.item_check(it)}"
                elif field in INPUT_FIELDS:
                    v = None
                else:
                    v = it["show"].get(field)
                c = ws.cell(row=r, column=j, value=v)
                if field == "rows" or field == "royalty":
                    c.number_format = "#,##0"
                else:
                    c.number_format = "@"
                if field in INPUT_FIELDS:
                    c.fill = INPUT_FILL
                if field == "_id":
                    c.font = ID_FONT
                if field in ("proposed", "why", "problem", "reason"):
                    c.alignment = Alignment(wrap_text=True, vertical="top")
        dec_col = next(j for j, (_h, f, _w) in enumerate(all_cols, start=1) if f == "decision")
        _dropdown(ws, choices, ws.cell(row=2, column=dec_col).column_letter, len(rows) + 2)
        if rows:
            last_col = ws.cell(row=2, column=len(all_cols)).column_letter
            ws.auto_filter.ref = f"A2:{last_col}{len(rows) + 2}"
        else:
            ws.cell(row=3, column=1, value="Nothing to decide on this tab.")

    for tab, (_key, banner, cols) in HAND_TABS.items():
        ws = wb.create_sheet(tab)
        _banner_and_header(ws, banner, cols)
        for r in range(3, INPUT_ROWS + 3):
            for j in range(1, len(cols) + 1):
                c = ws.cell(row=r, column=j)
                c.fill, c.number_format = INPUT_FILL, "@"
        if tab == "Change films":
            _dropdown(ws, R.SETTABLE_FIELDS, "B", INPUT_ROWS + 2)
        if tab == "Map codes":
            _dropdown(ws, tuple(config.MEDIA_BUSINESS_GROUPS), "A", INPUT_ROWS + 2)

    ws = wb.create_sheet("Films now")
    cols = [("IMDb ID", "imdb_id", 14), ("Title", "title_name", 36),
            ("Distributor", "distributor", 18), ("Release", "release_date", 12),
            ("PVOD", "pvod_date", 12), ("EST", "est_date", 12),
            ("NBC genre 1", "nbc_genre_1", 16), ("NBC genre 2", "nbc_genre_2", 16),
            ("In MMM", "in_mmm", 9), ("In MMM reason", "in_mmm_reason", 40),
            ("Revenue product IDs", "_pids", 22), ("Media product codes", "_codes", 22),
            ("Review note", "review_note", 50)]
    _banner_and_header(ws, "The film list as it is today, to look up IMDb IDs, dates, genres and "
                       "the product IDs and codes linked to each film. Reference only: changes "
                       "made here are not read.", cols)
    links = film_links(aliases or {})
    for i, t in enumerate(sorted(titles.values(), key=lambda t: t["title_name"]), start=3):
        for j, (_h, field, _w) in enumerate(cols, start=1):
            if field in ("_pids", "_codes"):
                v = ", ".join(links.get(t["imdb_id"], {}).get(field, []))
            else:
                v = t.get(field)
            if field == "in_mmm":
                v = {True: "yes", False: "no"}.get(v, "")
            ws.cell(row=i, column=j, value=R.text(v)).number_format = "@"
    last_col = ws.cell(row=2, column=len(cols)).column_letter
    ws.auto_filter.ref = f"A2:{last_col}{max(len(titles) + 2, 3)}"

    wb.active = 1
    wb.save(path)
    return path


def film_links(aliases: dict) -> dict[str, dict[str, list[str]]]:
    """{imdb: {'_pids': [product ids], '_codes': ['UP_HE:ABC', ...]}} from title_alias rows."""
    out: dict[str, dict[str, list[str]]] = {}
    for (alias_type, group, value), row in sorted(aliases.items(), key=lambda kv: str(kv[0])):
        imdb = row.get("imdb_id")
        if not imdb or row.get("category") != "film":
            continue
        d = out.setdefault(imdb, {"_pids": [], "_codes": []})
        if alias_type == "revenue_product_id":
            d["_pids"].append(value)
        elif alias_type == "media_product_code":
            d["_codes"].append(f"{group}:{value}")
    return out


def _rows(ws, cols, tab, problems, required=None):
    """Rows of one tab as dicts keyed by field name (header on row 2, data from row 3).
    Only the 'required' columns must be present (default: all); others are read when there."""
    required = {f for _h, f, _w in cols} if required is None else required
    it = ws.iter_rows(values_only=True)
    next(it, None)                                   # banner
    header = [R.text(h) for h in next(it, ())]
    index = {}
    for h, field, _w in cols:
        if h in header:
            index[field] = header.index(h)
        elif field in required:
            problems.append(f"The '{tab}' tab has no '{h}' column. Do not rename or delete the "
                            "yellow columns or the Row ID column.")
    for n, values in enumerate(it, start=3):
        yield n, {f: (values[i] if i < len(values) else None) for f, i in index.items()}


def read_workbook(path: str | Path) -> tuple[dict, list[str]]:
    """Return ({'review': [...], 'add_films': [...], ...}, problems). Empty rows are skipped."""
    problems: list[str] = []
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - shown to the analyst as a plain STOP message
        return {}, [f"The workbook could not be opened ({exc}). Was it saved as .xlsx?"]
    data: dict[str, list[dict]] = {"review": []}

    for section, (tab, _banner, cols, _choices) in REVIEW_TABS.items():
        if tab not in wb.sheetnames:
            continue            # a workbook issued before this tab existed: nothing to read
        label_field = cols[0][1]
        for n, row in _rows(wb[tab], cols + [(ID_HEADER, "_id", 0)], tab, problems,
                            required=INPUT_FIELDS | {"_id"}):
            raw = R.text(row.get("decision"))
            if not raw and not R.text(row.get("answer")) and not R.text(row.get("reason")):
                continue
            data["review"].append({
                "row": n, "tab": tab, "key": R.text(row.get("_id")),
                "label": (f"{R.text(row.get('business'))} {R.text(row.get('code'))}".strip()
                          if section == 1 else R.text(row.get(label_field))),
                "raw_decision": raw,
                "decision": DECISION_MAP.get(raw.lower(), raw.lower()),
                "use_instead": row.get("answer"), "reason": row.get("reason")})

    for tab, (key, _banner, cols) in HAND_TABS.items():
        data[key] = []
        if tab not in wb.sheetnames:
            problems.append(f"The '{tab}' tab is missing. Use a workbook issued by the notebook.")
            continue
        for n, row in _rows(wb[tab], cols, tab, problems):
            if any(R.text(v) for v in row.values()):
                row["row"] = n
                data[key].append(row)
    wb.close()
    return data, problems
