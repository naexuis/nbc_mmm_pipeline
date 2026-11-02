"""
Channel review: Planning confirms the channel of media that has no channel label yet.

Same cycle as the title review, in its own folder (config.CHANNEL_REVIEW_DIR):
  issued    a new workbook to send to Planning
  returned  where the filled-in workbook is uploaded
  imported  workbooks already saved (moved there automatically)

The workbook lists, for titles released on or after config.MMM_FIRST_RELEASE_DATE only:
  1 · Likely right   Prisma placements whose suggested label is already used on that site
  2 · Closer look    the other Prisma placements (largest spend first, up to
                     config.CHANNEL_REVIEW_MAX_PLACEMENTS placements on tabs 1 and 2 together)
  3 · TV and radio   network / station codes with no label
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
from openpyxl.styles import Alignment

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

CHOICES = ("Accept", "Use my answer", "Not yet")
INPUT_FIELDS = ("decision", "your_mmm", "your_plan", "reason")
LABEL_HELP = ("Labels look like PARENT_CHILD, e.g. CTVOTT_AMAZON_FIRETV or SOCIAL_TIKTOK. The parent "
              "(first part) must be one of: " + ", ".join(config.CHANNEL_PARENTS) + ". See the "
              "Labels in use tab for labels already used.")
PLACEMENT_COLS = [
    ("Client", "client", 8), ("Film", "film", 26), ("Site", "site", 22),
    ("Placement name", "name", 60), ("Buy type", "buy_type", 11), ("Package type", "package_type", 13),
    ("First day", "first_day", 11), ("Last day", "last_day", 11), ("Spend", "spend", 13),
    ("Suggested media-plan label", "suggested_plan", 26), ("Suggested MMM label", "suggested_mmm", 26),
    ("Decision", "decision", 15), ("Your MMM label", "your_mmm", 24),
    ("Your media-plan label (optional)", "your_plan", 24), ("Reason (optional)", "reason", 30),
]
TV_COLS = [
    ("Code", "key", 24), ("Network or station", "network", 26), ("Media", "media", 18),
    ("Source", "sources", 14), ("Clients", "clients", 14), ("Spend", "spend", 13),
    ("Suggested label", "suggested_mmm", 26), ("Decision", "decision", 15),
    ("Your MMM label", "your_mmm", 24), ("Your media-plan label (optional)", "your_plan", 24),
    ("Reason (optional)", "reason", 30),
]
TABS = {
    "likely": ("1 · Likely right",
               "Prisma placements with no channel yet. The suggested label has already been used on "
               "this site, so it is usually right (about 98% in testing). Accept uses both suggested "
               "labels. Use my answer uses what you type under Your MMM label (and Your media-plan "
               "label, if the media plan said something else). Not yet (or empty) leaves it for a "
               "later workbook.\n" + LABEL_HELP, PLACEMENT_COLS),
    "closer": ("2 · Closer look",
               "Prisma placements the model is unsure about: a new site, or a site that has been "
               "used for other channels. Please check how the placement actually ran. Same choices "
               "as tab 1.\n" + LABEL_HELP, PLACEMENT_COLS),
    "tv": ("3 · TV and radio",
           "TV, radio and MVPD network or station codes with no channel yet (one label per code, "
           "used for every spot on it). Same choices as tab 1. Local TV stations are suggested as "
           "SPOTTV_LOCALTV.\n" + LABEL_HELP, TV_COLS),
}
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


def decide(items_by_id: dict, rows: list[dict]) -> tuple[list[dict], list[str], list[dict]]:
    """
    Turn workbook rows into label changes. Returns (changes, problems, skipped).
    Each change: key_type, key, name, detail, mediaplan_label, mmm_label, decision, reason, where.
    """
    changes, problems, skipped = [], [], []
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
        elif decision == "use my answer":
            mmm = clean_label(r.get("your_mmm"))
            plan = (clean_label(r.get("your_plan")) or clean_label(r.get("suggested_plan"))
                    or item.get("suggested_plan") or mmm)
        else:
            problems.append(f"{where}: Decision should be Accept, Use my answer or Not yet")
            continue
        bad = label_problem(mmm) or (label_problem(plan) and f"media-plan label: {label_problem(plan)}")
        if bad:
            problems.append(f"{where}: {bad}")
            continue
        reason = R.text(r.get("reason")) or (
            "Planning accepted the suggested label" if decision == "accept"
            else "Planning gave the label")
        changes.append({"key_type": item["key_type"], "key": item["key"], "name": item.get("site")
                        or item.get("network"), "detail": item.get("name") or item.get("media"),
                        "mediaplan_label": plan, "mmm_label": mmm, "decision": decision,
                        "reason": reason, "where": where,
                        "suggested_mmm": item.get("suggested_mmm")})
    return changes, problems, skipped


# =============================================================================
# Workbook
# =============================================================================

def write_workbook(items: list[dict], labels_in_use: list[dict], folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"channel-review-{datetime.now():%Y%m%dT%H%M%S}.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    for group, (tab, banner, cols) in TABS.items():
        ws = wb.create_sheet(tab)
        all_cols = cols + [(ID_HEADER, "_id", 30)]
        _banner_and_header(ws, banner, all_cols)
        rows = [i for i in items if i["group"] == group]
        for r, it in enumerate(rows, start=3):
            for j, (_h, field, _w) in enumerate(all_cols, start=1):
                v = row_id(it) if field == "_id" else (None if field in INPUT_FIELDS else it.get(field))
                if isinstance(v, (date, datetime)):
                    v = v.isoformat()[:10]
                c = ws.cell(row=r, column=j, value=v)
                c.number_format = "$#,##0" if field == "spend" else "@"
                if field == "spend" and v is not None:
                    c.value = float(v)
                if field in INPUT_FIELDS:
                    c.fill = INPUT_FILL
                if field == "_id":
                    c.font = ID_FONT
                if field == "name":
                    c.alignment = Alignment(wrap_text=True, vertical="top")
        dec = next(j for j, (_h, f, _w) in enumerate(all_cols, start=1) if f == "decision")
        _dropdown(ws, CHOICES, ws.cell(row=2, column=dec).column_letter, len(rows) + 2)
        if rows:
            last = ws.cell(row=2, column=len(all_cols)).column_letter
            ws.auto_filter.ref = f"A2:{last}{len(rows) + 2}"
        else:
            ws.cell(row=3, column=1, value="Nothing to decide on this tab.")
    ws = wb.create_sheet("Labels in use")
    cols = [("Parent", "parent", 16), ("MMM label", "mmm_label", 34), ("Used on", "n", 10)]
    _banner_and_header(ws, "The MMM labels already used, with how many placements or codes have "
                       "each one. Reference only: changes made here are not read.", cols)
    for i, r in enumerate(labels_in_use, start=3):
        ws.cell(row=i, column=1, value=L.parent_of(r["mmm_label"]) or "")
        ws.cell(row=i, column=2, value=r["mmm_label"])
        ws.cell(row=i, column=3, value=int(r["n"]))
    ws.auto_filter.ref = f"A2:C{max(len(labels_in_use) + 2, 3)}"
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
    placements = _q(client, f"""
        WITH p AS (
          SELECT placement_number AS key, ANY_VALUE(business_code) AS client,
                 ANY_VALUE(imdb_id) AS imdb_id, ANY_VALUE(site) AS site,
                 ANY_VALUE(placement_name) AS name, ANY_VALUE(buy_type) AS buy_type,
                 ANY_VALUE(package_type) AS package_type, MIN(spend_date) AS first_day,
                 MAX(spend_date) AS last_day, SUM(spend_usd) AS spend
          FROM `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` WHERE link = 'film' GROUP BY 1)
        SELECT 'prisma_placement' AS key_type, p.*, t.title_name AS film
        FROM p JOIN `{title_t}` t USING (imdb_id)
        LEFT JOIN `{labels_t}` l ON l.key_type = 'prisma_placement' AND l.key = p.key
        WHERE l.key IS NULL AND t.release_date >= DATE '{first}'
        ORDER BY ABS(p.spend) DESC""")
    tv = _q(client, f"""
        SELECT 'tv_network' AS key_type, m.channel_code AS key,
               STRING_AGG(DISTINCT m.media_name, ', ') AS media,
               STRING_AGG(DISTINCT m.source, ', ') AS sources,
               STRING_AGG(DISTINCT m.business_code, ', ') AS clients, SUM(m.spend_usd) AS spend
        FROM `{_core(config.MEDIA_TV_CORE_TABLE)}` m JOIN `{title_t}` t USING (imdb_id)
        LEFT JOIN `{labels_t}` l ON l.key_type = 'tv_network' AND l.key = m.channel_code
        WHERE m.link = 'film' AND l.key IS NULL AND t.release_date >= DATE '{first}'
        GROUP BY 1, 2 ORDER BY ABS(SUM(m.spend_usd)) DESC""")
    training = _q(client, f"""
        SELECT l.key, l.name AS site, l.detail AS name, l.mediaplan_label, l.mmm_label,
               ANY_VALUE(p.imdb_id) AS film
        FROM `{labels_t}` l
        LEFT JOIN `{_core(config.MEDIA_PRISMA_CORE_TABLE)}` p ON p.placement_number = l.key
        WHERE l.key_type = 'prisma_placement'
        GROUP BY 1, 2, 3, 4, 5""")
    in_use = _q(client, f"""
        SELECT mmm_label, COUNT(*) AS n FROM `{labels_t}` WHERE mmm_label IS NOT NULL
        GROUP BY 1 ORDER BY 1""")
    all_labels = {(r["key_type"], r["key"]): r for r in _q(client, f"SELECT * FROM `{labels_t}`")}
    try:
        runs = _q(client, f"SELECT * FROM `{_core(config.CHANNEL_MODEL_RUNS_TABLE)}` "
                          "ORDER BY run_at DESC LIMIT 20")
    except Exception:  # noqa: BLE001 - no runs yet
        runs = []
    return {"placements": placements, "tv": tv, "training": training, "in_use": in_use,
            "all_labels": all_labels, "runs": runs}


def _log_run(client, row: dict) -> None:
    payload = [{f.name: (row.get(f.name).isoformat() if isinstance(row.get(f.name), datetime)
                         else row.get(f.name)) for f in MODEL_RUNS_SCHEMA}]
    client.load_table_from_json(payload, _core(config.CHANNEL_MODEL_RUNS_TABLE),
                                job_config=bigquery.LoadJobConfig(
                                    schema=MODEL_RUNS_SCHEMA,
                                    write_disposition=bigquery.WriteDisposition.WRITE_APPEND)).result()


def _apply(client, all_labels: dict, changes: list[dict], workbook: str) -> int:
    who, now = R.current_user(), datetime.now(timezone.utc)
    history = []
    for c in changes:
        k = (c["key_type"], c["key"])
        old = all_labels.get(k, {})
        for f in L.LABEL_FIELDS:
            if old.get(f) != c[f]:
                history.append({"change_id": uuid.uuid4().hex, "table_name": config.CHANNEL_LABEL_TABLE,
                                "record_key": f"{k[0]}|{k[1]}", "field": f,
                                "old_value": old.get(f), "new_value": c[f],
                                "reason": f"{c['reason']} ({workbook})", "changed_by": who,
                                "changed_at": now.isoformat()})
        all_labels[k] = {"key_type": k[0], "key": k[1], "name": c["name"], "detail": c["detail"],
                         "mediaplan_label": c["mediaplan_label"], "mmm_label": c["mmm_label"],
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
        it["suggested_mmm"] = it["suggested_plan"] = tv_suggestion(it["key"], it["network"],
                                                                   it["media"])
        it["group"] = "tv"

    if not waiting:
        S.suggest(S.train(data["training"]), data["placements"])
        listed = data["placements"][:config.CHANNEL_REVIEW_MAX_PLACEMENTS]
        items = sorted(listed, key=lambda i: (i["group"] != "likely", -abs(float(i["spend"] or 0))))
        items += data["tv"]
        path = write_workbook(items, data["in_use"], folders["issued"])
        q = _quality(data["training"]) or {}
        result.update(status="ISSUED", workbook=path.name, quality=q,
                      listed={"likely": sum(i["group"] == "likely" for i in items),
                              "closer": sum(i["group"] == "closer" for i in items),
                              "tv": len(data["tv"])})
        _log_run(client, {"event": "issued", "run_at": datetime.now(timezone.utc),
                          "workbook": path.name, **q,
                          "listed_placements": len(listed), "listed_tv": len(data["tv"])})
        return result

    path = waiting[0]
    result["workbook"] = path.name
    rows, problems = read_workbook(path)
    for it in data["placements"]:
        it.setdefault("group", "closer")
    items_by_id = {row_id(i): i for i in data["placements"] + data["tv"]}
    changes, more, skipped = decide(items_by_id, rows)
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
            body = (f"**File:** `{name}`  \n**On it:** {lst['likely']:,} placements on 1 · Likely "
                    f"right, {lst['closer']:,} on 2 · Closer look, {lst['tv']:,} codes on "
                    "3 · TV and radio.\n\n"
                    f"**Where to find it:** {R.where_to_find('issued', root)}  \n"
                    "Click the file name to download it, then send it to Planning.\n\n"
                    "**What Planning does in Excel:** on each tab, choose a Decision in the yellow "
                    "columns: **Accept** the suggested labels, or **Use my answer** and type the "
                    "label. Leave rows they are not sure about empty. Save it as .xlsx (keep the "
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
                        "Decision": c["decision"].capitalize(), "Reason": c["reason"]}
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
             f"\"Likely right\" group: **{_pct(q['likely_acc'])}** right · \"Closer look\" group: "
             f"**{_pct(q['closer_acc'])}** right."]
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
