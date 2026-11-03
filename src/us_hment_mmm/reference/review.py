"""
Title review: the only way the title and title_alias tables change after seeding.

How it works (notebook 05_Review_Titles):
  1. Make a review workbook. Its "Review" tab lists everything that needs a decision:
       - product codes in the newest media that are not mapped yet (with a suggestion when
         one can be made: an IMDb tag in the Prisma campaign name, a matching title name, or
         "non-film" for drafting / anniversary / brand campaigns)
       - films carrying a review note
       - dates where NBC's enrichment table or Finance's CHTT differs from the approved date
       - revenue product IDs that NBC tags with a known film but that are not mapped yet
     Other tabs let the analyst add films, change film values, and map codes or product IDs
     by hand.
  2. The workbook is issued to the Domino dataset folder 'issued'. The analyst downloads it,
     fills in Decision / Use instead / Reason in Excel, and uploads it to 'returned'.
  3. The notebook reads it, shows every change (old -> new), and when DRY_RUN is False saves
     the changes and moves the workbook to 'imported'. Every change, and every rejected
     suggestion, writes a title_history row.

A row is skipped (never applied) when the item is no longer on the list or its values changed
after the workbook was made, so an old workbook can never overwrite newer decisions.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client
from us_hment_mmm.reference import titles as T

IMDB_RE = re.compile(r"^tt\d{7,9}$")
NON_FILM_RE = re.compile(r"DRAFT|DRFT|ANNIV|\b\d{2,3}(ST|ND|RD|TH)\b|BRAND|CATALOG", re.IGNORECASE)
DATE_FIELDS = ("release_date", "pvod_date", "est_date")
# Dates before this year are placeholders, not real dates (e.g. 1899-12-30 is how a blank or
# zero comes out of Excel, which Finance's CHTT uses for "no premium window").
MIN_YEAR = 1950
GENRE_FIELDS = ("nbc_genre_1", "nbc_genre_2")
GENRE_LABELS = {"nbc_genre_1": "NBC genre 1", "nbc_genre_2": "NBC genre 2"}
SETTABLE_FIELDS = ("title_name", "distributor", "release_date", "pvod_date", "est_date",
                   "nbc_genre_1", "nbc_genre_2", "imdb_genres",
                   "in_mmm", "review_note")
FIELD_LABELS = {"release_date": "Release", "pvod_date": "PVOD", "est_date": "EST"}
SECTION_NAMES = {1: "Product code", 2: "Review note", 3: "Date", 4: "Product ID",
                 5: "NBC genre", 6: "Distributor", 7: "IMDb genres"}
IMDB_GENRE_FILE = "universal_genre_mapping.json"     # in reference_seed/ (IMDb website genres)
IMDB_WEBSITE_SOURCE = "IMDb website (genre file)"
IMDB_BASICS_PATTERN = "imdb_genres_title_basics_*.csv"   # from the Amazon MMT Pipeline
IMDB_BASICS_SOURCE = "IMDb title.basics"
HAND_SOURCE = "entered by hand"
DECISIONS = ("Approve", "Use instead", "Reject")
REVIEW_TABLE = "review_decision"   # table_name used in title_history for rejected suggestions
NO_SUGGESTION = "—"
NO_PROPOSAL = "No proposal. Please fill in Your answer."


# =============================================================================
# Pure helpers (unit tested)
# =============================================================================

def current_user() -> str:
    return os.environ.get("DOMINO_STARTING_USERNAME") or getpass.getuser()


def parse_date(value) -> str | None:
    """Return an ISO date string from the common text formats (or an Excel date), or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat() if value.year >= MIN_YEAR else None
    s = str(value).strip()
    if not s or s in T.EMPTY_VALUES:
        return None
    for fmt, text in (("%Y-%m-%d", s[:10]), ("%m/%d/%Y", s.split()[0]), ("%Y%m%d", s)):
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return d.isoformat() if d.year >= MIN_YEAR else None
    return None


def parse_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value or "").strip().lower()
    return {"yes": True, "y": True, "true": True, "1": True,
            "no": False, "n": False, "false": False, "0": False}.get(s)


def text(value) -> str:
    """Cell value as trimmed text ('' for empty); whole numbers lose their '.0'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (datetime, date)):
        return parse_date(value) or ""
    return str(value).strip()


def norm_name(s: str | None) -> str:
    """Uppercase, '&' -> AND, punctuation removed, leading 'THE ' dropped, spaces squeezed."""
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r",? THE$", "", s)
    return s[4:] if s.startswith("THE ") else s


def display_name(s: str | None) -> str | None:
    """'UPRISING, THE' -> 'THE UPRISING' (NBC lists some titles with the article last)."""
    if not s:
        return s
    m = re.fullmatch(r"(.*?),\s*(THE|A|AN)", s.strip(), re.IGNORECASE)
    return f"{m.group(2)} {m.group(1)}".upper() if m else s.upper()


def names_agree(a: str | None, b: str | None) -> bool:
    """Loose check that two names are the same film (Prisma cuts names at 20 characters)."""
    na, nb = norm_name(a), norm_name(b)
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if not ca or not cb:
        return False
    if ca[:5] in cb or cb[:5] in ca:
        return True
    return bool({w for w in na.split() if len(w) >= 4} & {w for w in nb.split() if len(w) >= 4})


def suggest_for_code(sample_name: str, tags, titles: dict,
                     enrichment: dict) -> tuple[str | None, str | None, str]:
    """
    Suggest what an unmapped product code is. Returns (category, imdb_id, why);
    category is 'film', 'non_film' or None (no suggestion).
    tags = the IMDb IDs found in the code's Prisma campaign names (a list, or one ID).
    """
    if NON_FILM_RE.search(sample_name or ""):
        return "non_film", None, ("the name looks like a drafting, anniversary or brand campaign, "
                                  "which is not part of any film")
    tags = sorted({tags} if isinstance(tags, str) else set(tags or ()))
    if len(tags) > 1:
        return None, None, ("Planning's campaign names for this code show different IMDb IDs ("
                            + ", ".join(tags) + "); check with Planning which film it is")
    tag_imdb = tags[0] if tags else None
    if tag_imdb and (tag_imdb in titles or tag_imdb in enrichment):
        tag_name = (titles.get(tag_imdb) or enrichment.get(tag_imdb))["title_name"]
        if not names_agree(sample_name, tag_name):
            return None, None, (f"Planning's campaign names point to {display_name(tag_name)} "
                                f"({tag_imdb}), but that name does not match the Prisma name; "
                                "check whether it is the same film (e.g. a title change)")
        return "film", tag_imdb, "Planning's campaign names for this code include this IMDb ID"
    key = norm_name(sample_name)
    if len(key) >= 4:
        hits = {i for i, t in titles.items() if norm_name(t["title_name"]).startswith(key)}
        if len(hits) == 1:
            return "film", hits.pop(), "the Prisma name matches this film in the title table"
        if not hits:
            hits = {i for i, e in enrichment.items() if norm_name(e["title_name"]).startswith(key)}
            if len(hits) == 1:
                return "film", hits.pop(), "the Prisma name matches this film in NBC's title table"
    return None, None, ("no matching film found; look it up on the Films now tab (or IMDb) and "
                        "type its IMDb ID, or non_film if it is not a film")


BUNDLE_RE = re.compile(r"COLLECTION|BUNDLE|\bPACK\b|TRILOGY|DOUBLE FEATURE|\bBOX SET|"
                       r"\b\d+[- ]?(FILM|MOVIE)\b| \+ | / ", re.IGNORECASE)


def suggest_for_pid(name: str | None, imdb: str, titles: dict) -> tuple[bool, str]:
    """Should NBC's IMDb tag on a revenue product ID be proposed? Returns (propose, why)."""
    film = titles[imdb]["title_name"]
    if name and BUNDLE_RE.search(name):
        return False, ("the revenue name looks like a bundle or collection; linking it would give "
                       f"all of its revenue to {film}. Keep as is unless you are sure")
    if name and not names_agree(name, film):
        return False, (f"NBC tags it with {film} ({imdb}), but the revenue name does not match; "
                       "check which film it is")
    return True, "NBC's revenue data tags these sales with this IMDb ID, and the names match"


def suggest_for_untagged(p: dict, film: str) -> tuple[bool, str]:
    """Proposal for a product ID found by name or date (its revenue rows have no IMDb ID)."""
    if p.get("found_by") == "date":
        return False, (f"NBC has no IMDb ID on these sales and the name does not match, but the "
                       f"first sale ({p.get('first_sale')}) lines up with {film}'s release and "
                       f"PVOD dates. If it is {film}, choose Use my answer and type "
                       f"{p['imdb_id']}; if not, choose Keep as is")
    if BUNDLE_RE.search(p.get("name") or ""):
        return False, ("NBC has no IMDb ID on these sales; the revenue name looks like a bundle "
                       f"or collection with {film}. Keep as is unless you are sure")
    if not p.get("top"):
        return False, (f"NBC has no IMDb ID on these sales; the name matches {film}, but another "
                       "product ID for it has more sales. Check which one is the film")
    return True, ("NBC has no IMDb ID on these sales; the revenue name matches this film and "
                  "the sales start at its release")


# Product IDs whose revenue rows carry no IMDb ID (NBC stopped tagging many new titles) are
# matched to films two ways:
#   by name  - the film's name is in the revenue name, and the first sale falls between two
#              weeks before release and two weeks after PVOD (proposed for Accept)
#   by date  - for films the name search missed: the first sale falls in that window and the
#              product sold at least UNTAGGED_DATE_MIN_ROYALTY (listed, no proposal)
# A smaller name match is listed only if it has at least UNTAGGED_MIN_SHARE of the best
# match's royalty, or is a bundle (so the reviewer sees it).
UNTAGGED_MIN_SHARE = 0.10
UNTAGGED_DAYS_BEFORE_RELEASE = 14
UNTAGGED_DAYS_AFTER_PVOD = 14
UNTAGGED_DATE_MIN_ROYALTY = 10_000
UNTAGGED_DATE_MAX_PER_FILM = 5


def _match_name(s: str | None) -> str:
    """norm_name, with apostrophes dropped first (FREDDY'S = FREDDYS)."""
    return norm_name(re.sub(r"['’`]", "", s or ""))


def _in_window(sold: date | None, release: date, pvod: date | None) -> bool:
    if sold is None:
        return True
    if sold < release - timedelta(days=UNTAGGED_DAYS_BEFORE_RELEASE):
        return False
    return pvod is None or sold <= pvod + timedelta(days=UNTAGGED_DAYS_AFTER_PVOD)


def match_untagged(products: list[dict], titles: dict,
                   linked_films: set[str] = frozenset()) -> list[dict]:
    """
    Match untagged revenue product IDs to films released on or after
    config.MMM_FIRST_RELEASE_DATE (see the notes above). Name matches: per film, the non-bundle
    product with the most royalty is 'top' (proposed). Date matches only for films with no name
    match and no product ID linked yet; never proposed.
    """
    first = config.MMM_FIRST_RELEASE_DATE
    films = {}
    for imdb, t in titles.items():
        release, pvod = parse_date(t.get("release_date")), parse_date(t.get("pvod_date"))
        if release and release >= first:
            films[imdb] = (_match_name(t.get("title_name")), date.fromisoformat(release),
                           date.fromisoformat(pvod) if pvod else None)
    by_film: dict[str, list[dict]] = {}
    unmatched = []
    for p in products:
        name = f" {_match_name(p.get('name'))} "
        sold = parse_date(p.get("first_sale"))
        sold = date.fromisoformat(sold) if sold else None
        hits = [(imdb, key) for imdb, (key, release, pvod) in films.items()
                if len(key.replace(" ", "")) >= 4 and f" {key} " in name
                and _in_window(sold, release, pvod)]
        longest = max((len(k) for _, k in hits), default=0)   # most specific film name wins
        best = [imdb for imdb, k in hits if len(k) == longest]
        if len(best) == 1:
            by_film.setdefault(best[0], []).append(p)
        elif not hits:
            unmatched.append((p, sold))
    out = []
    for imdb, found in by_film.items():
        found.sort(key=lambda p: -(p.get("royalty") or 0))
        singles = [p for p in found if not BUNDLE_RE.search(p.get("name") or "")]
        top = singles[0]["product_id"] if singles else None
        top_royalty = (singles[0].get("royalty") or 0) if singles else 0
        for p in found:
            bundle = bool(BUNDLE_RE.search(p.get("name") or ""))
            if p["product_id"] == top or bundle or (p.get("royalty") or 0) >= (
                    UNTAGGED_MIN_SHARE * top_royalty):
                out.append({**p, "imdb_id": imdb, "found_by": "name",
                            "top": p["product_id"] == top})
    # Date matches: each product goes to the film whose release or PVOD is closest to its
    # first sale; each film keeps its closest few.
    missed = {imdb: f for imdb, f in films.items()
              if imdb not in by_film and imdb not in linked_films and f[2] is not None}
    by_date: dict[str, list[tuple[int, dict]]] = {}
    for p, sold in unmatched:
        if sold is None or (p.get("royalty") or 0) < UNTAGGED_DATE_MIN_ROYALTY:
            continue
        near = [(min(abs((sold - release).days), abs((sold - pvod).days)), imdb)
                for imdb, (_k, release, pvod) in missed.items()
                if _in_window(sold, release, pvod)]
        if near:
            gap, imdb = min(near)
            by_date.setdefault(imdb, []).append((gap, p))
    for imdb, found in by_date.items():
        for _gap, p in sorted(found, key=lambda x: (x[0], -(x[1].get("royalty") or 0)))[
                :UNTAGGED_DATE_MAX_PER_FILM]:
            out.append({**p, "imdb_id": imdb, "found_by": "date", "top": False})
    return out


def date_differences(titles: dict, enrichment: dict, chtt: dict) -> list[dict]:
    """Dates where NBC or Finance has a value that differs from the approved one."""
    out = []
    for imdb, t in titles.items():
        for field in DATE_FIELDS:
            proposed: dict[str, list[str]] = {}
            e, c = enrichment.get(imdb, {}), chtt.get(imdb, {})
            for source, value in (("NBC", e.get(field)), ("Finance", c.get(field))):
                if value and value != t.get(field):
                    proposed.setdefault(value, []).append(source)
            for value, sources in sorted(proposed.items()):
                out.append({"imdb_id": imdb, "title_name": t["title_name"], "in_mmm": t["in_mmm"],
                            "field": field, "current": t.get(field), "new": value,
                            "source": " + ".join(sources)})
    order = {True: 0, None: 1, False: 2}
    out.sort(key=lambda d: (order[d["in_mmm"]], d["title_name"], d["field"], d["new"]))
    return out


def _dist_key(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9/ ]", " ", text(s).upper())).strip()


_DIST_STANDARD = {_dist_key(k): v for k, v in config.DISTRIBUTOR_STANDARD.items()}


def standard_distributor(value) -> str | None:
    """The agreed standard name for a distributor spelling (unlisted names: uppercase, as is)."""
    key = _dist_key(value)
    if not key:
        return None
    return _DIST_STANDARD.get(key, text(value).upper())


def distributor_differences(titles: dict, enrichment: dict) -> list[dict]:
    """Distributors not in their standard spelling, and blank ones NBC can fill."""
    out = []
    for imdb, t in titles.items():
        cur = text(t.get("distributor")) or None
        if cur:
            std = standard_distributor(cur)
            if std != cur:
                out.append({"imdb_id": imdb, "title_name": t["title_name"], "in_mmm": t["in_mmm"],
                            "current": cur, "new": std, "why": "standard name for this distributor"})
        else:
            nbc = standard_distributor(enrichment.get(imdb, {}).get("distributor"))
            if nbc:
                out.append({"imdb_id": imdb, "title_name": t["title_name"], "in_mmm": t["in_mmm"],
                            "current": None, "new": nbc, "why": "NBC distributor (standard name)"})
    order = {True: 0, None: 1, False: 2}
    out.sort(key=lambda d: (order[d["in_mmm"]], d["title_name"]))
    return out


def genre_list(value) -> str | None:
    """'drama,  Comedy;sci-fi' -> 'Comedy, Drama, Sci-Fi' (sorted, one spelling per genre)."""
    parts = [p.strip() for p in re.split(r"[,;|]", text(value)) if p.strip()]
    fixed = {"SCIFI": "Sci-Fi", "SCI-FI": "Sci-Fi", "BASKETALL": "Basketball"}
    out = sorted({fixed.get(p.upper().replace(" ", ""), p.title()) for p in parts})
    return ", ".join(out) or None


def imdb_genres_from_file(data: dict) -> dict[str, str]:
    """{imdb_id: 'Adventure, Animation, ...'} from the is_* flags of universal_genre_mapping.json."""
    out = {}
    for v in data.values():
        flags = [k[3:] for k, on in v.items() if k.startswith("is_") and on is True]
        g = genre_list(",".join(flags))
        if v.get("imdb_id") and g:
            out[v["imdb_id"]] = g
    return out


def imdb_genre_differences(titles: dict, proposals: dict[str, tuple[str, str]]) -> list[dict]:
    """proposals = {imdb: (genre list, source)}; rows where it differs from the film's list."""
    out = []
    for imdb, (genres, source) in proposals.items():
        t = titles.get(imdb)
        if t is None or genre_list(genres) == genre_list(t.get("imdb_genres")):
            continue
        out.append({"imdb_id": imdb, "title_name": t["title_name"], "in_mmm": t["in_mmm"],
                    "current": t.get("imdb_genres"), "new": genre_list(genres), "source": source})
    order = {True: 0, None: 1, False: 2}
    out.sort(key=lambda d: (order[d["in_mmm"]], d["title_name"]))
    return out


def genre_differences(titles: dict, enrichment: dict) -> list[dict]:
    """NBC master genres that differ from the film's current genre (blank counts as different)."""
    out = []
    for imdb, t in titles.items():
        e = enrichment.get(imdb, {})
        for field, src in (("nbc_genre_1", "master_genre_1"), ("nbc_genre_2", "master_genre_2")):
            value = text(e.get(src)).upper() or None
            if value and value != (text(t.get(field)).upper() or None):
                out.append({"imdb_id": imdb, "title_name": t["title_name"], "in_mmm": t["in_mmm"],
                            "field": field, "current": t.get(field), "new": value})
    order = {True: 0, None: 1, False: 2}
    out.sort(key=lambda d: (order[d["in_mmm"]], d["title_name"], d["field"]))
    return out


def amazon_check(day: str | None, ev: dict | None, missing_weeks: set[str] = frozenset()) -> str:
    """
    Plain-English comparison of a PVOD date with the first week of Amazon revenue.
    Amazon weeks start on Sunday. A PVOD date fits when it falls from 7 days before the first
    sales week up to the end of that week (sales normally start on, or soon after, PVOD).
    """
    if not ev:
        return ""
    if not ev.get("usable_as_evidence", True):
        return "Amazon: already selling when Amazon's data starts (June 2024); no evidence"
    quality = text(ev.get("first_revenue_week_quality")).upper()
    week = parse_date(ev.get("first_revenue_week") if quality == "PARTIAL"
                      else ev.get("first_week_over_threshold") or ev.get("first_revenue_week"))
    if not week:
        return ""
    price = ev.get("first_threshold_week_avg_price")
    kind = ""
    if price:
        kind = " premium purchase" if price >= 15 else (" rental" if price < 10 else "")
        kind = f", ${price:,.2f} avg{kind}"
    notes = []
    if quality == "PARTIAL":
        notes.append("partial week")
    w = datetime.fromisoformat(week).date()
    if (w - timedelta(days=7)).isoformat() in missing_weeks:
        notes.append("the week before is missing in Amazon's data")
    head = f"Amazon: first sales week of {week}{kind}" + (f" ({'; '.join(notes)})" if notes else "")
    d = parse_date(day)
    if not d:
        return head
    gap = (datetime.fromisoformat(d).date() - w).days
    if -7 <= gap <= 6:
        verdict = "date fits"
    elif gap < -7:
        verdict = f"date is {-gap} days before that week"
    else:
        verdict = f"date is {gap} days after Amazon sales began"
    return f"{head}. {verdict}"


def build_items(codes: list[dict], titles: dict, enrichment: dict, dates: list[dict],
                pids: list[dict], rejected: set[tuple[str, str]] = frozenset(),
                genres: list[dict] = (), amazon: dict | None = None,
                distributors: list[dict] = (), imdb_genres: list[dict] = (),
                missing_weeks: set[str] = frozenset()) -> list[dict]:
    """
    The review list. Each item has a stable key, a 'current' and 'suggested' value (used to
    spot items that changed after a workbook was issued), 'show' (the columns its workbook
    tab displays) and a target (what a decision changes). Suggestions rejected before are
    left out (a product code stays on the list, without the rejected suggestion).
    """
    items = []
    in_mmm_text = {True: "yes", False: "no", None: "not reviewed"}

    def add(section, key, current, suggested, why, target, show):
        if section == 4 and not suggested and (key, NO_SUGGESTION) in rejected:
            return                       # kept unlinked before (e.g. a bundle)
        if suggested and (key, suggested) in rejected:
            if section != 1:
                return
            suggested, why = None, "an earlier proposal was turned down"
            show["proposed"] = NO_PROPOSAL
        show["why"] = why
        items.append({"section": section, "key": key, "current": current,
                      "suggested": suggested or NO_SUGGESTION, "has_suggestion": bool(suggested),
                      "why": why, "target": target, "show": show,
                      "what": show.get("label", key)})

    for c in sorted(codes, key=lambda c: (c["business_group"], c["product_code"])):
        cat, imdb, why = suggest_for_code(c["sample_name"], c.get("tags"), titles, enrichment)
        name = (titles.get(imdb) or enrichment.get(imdb) or {}).get("title_name") if imdb else None
        name = display_name(name)
        suggested = {"film": f"{name} ({imdb})", "non_film": "non_film"}.get(cat)
        proposed = {"film": f"Link to {name} ({imdb})",
                    "non_film": "Mark as a non-film campaign (spend is not counted for any film)"
                    }.get(cat, NO_PROPOSAL)
        group_label = {"UP_HE": "UP Home Ent", "FOCUS": "Focus"}.get(c["business_group"],
                                                                     c["business_group"])
        add(1, f"code|{c['business_group']}|{c['product_code']}", "not mapped", suggested, why,
            {"group": c["business_group"], "code": c["product_code"], "label": c["sample_name"],
             "imdb_id": imdb if cat == "film" else None},
            {"label": f"{c['business_group']} {c['product_code']}", "business": group_label,
             "code": c["product_code"], "name": c["sample_name"], "rows": c["rows"],
             "proposed": proposed})
    for imdb, t in sorted(titles.items(), key=lambda kv: kv[1]["title_name"]):
        if t.get("review_note"):
            add(2, f"note|{imdb}", t["review_note"], "clear the note",
                "check the problem, then mark it Resolved",
                {"imdb_id": imdb, "field": "review_note", "value": None},
                {"label": t["title_name"], "film": t["title_name"], "imdb_id": imdb,
                 "problem": explain_note(t["review_note"])})
    for d in dates:
        add(3, f"date|{d['imdb_id']}|{d['field']}|{d['source']}", d["current"] or "(none)",
            d["new"], f"{d['source']} date",
            {"imdb_id": d["imdb_id"], "field": d["field"], "value": d["new"]},
            {"label": f"{d['title_name']} {FIELD_LABELS[d['field']]}", "film": d["title_name"],
             "imdb_id": d["imdb_id"], "in_mmm": in_mmm_text[d["in_mmm"]],
             "date_type": FIELD_LABELS[d["field"]] + " date",
             "now": d["current"] or "(none)", "proposed": d["new"], "source": d["source"],
             "amazon": amazon_check(d["new"], (amazon or {}).get(d["imdb_id"]), missing_weeks)
             if d["field"] == "pvod_date" else ""})
    for p in sorted(pids, key=lambda p: -(p["royalty"] or 0)):
        name = titles[p["imdb_id"]]["title_name"]
        if p.get("found_by") in ("name", "date"):
            propose, why = suggest_for_untagged(p, name)
        else:
            propose, why = suggest_for_pid(p.get("name"), p["imdb_id"], titles)
        add(4, f"pid|{p['product_id']}", "not mapped",
            f"{name} ({p['imdb_id']})" if propose else None, why,
            {"product_id": p["product_id"], "imdb_id": p["imdb_id"] if propose else None,
             "label": name},
            {"label": f"Product ID {p['product_id']}", "product_id": p["product_id"],
             "revenue_name": p.get("name"), "royalty": round(p["royalty"] or 0),
             "sales": f"{p.get('first_sale') or '?'} to {p.get('last_sale') or '?'}",
             "tagged": {"name": f"(no IMDb ID) name matches {name}",
                        "date": f"(no IMDb ID) first sale lines up with {name}"}.get(
                            p.get("found_by"), f"{name} ({p['imdb_id']})"),
             "proposed": f"Link to {name} ({p['imdb_id']})" if propose else NO_PROPOSAL})
    for g in genres:
        add(5, f"genre|{g['imdb_id']}|{g['field']}", g["current"] or "(none)", g["new"],
            "NBC master genre",
            {"imdb_id": g["imdb_id"], "field": g["field"], "value": g["new"]},
            {"label": f"{g['title_name']} {GENRE_LABELS[g['field']]}", "film": g["title_name"],
             "imdb_id": g["imdb_id"], "in_mmm": in_mmm_text[g["in_mmm"]],
             "genre_type": GENRE_LABELS[g["field"]], "now": g["current"] or "(none)",
             "proposed": g["new"]})
    for d in imdb_genres:
        add(7, f"imdbgenre|{d['imdb_id']}", d["current"] or "(none)", d["new"], d["source"],
            {"imdb_id": d["imdb_id"], "field": "imdb_genres", "value": d["new"],
             "source": d["source"]},
            {"label": d["title_name"], "film": d["title_name"], "imdb_id": d["imdb_id"],
             "in_mmm": in_mmm_text[d["in_mmm"]], "now": d["current"] or "(none)",
             "proposed": d["new"], "source": d["source"]})
    for d in distributors:
        add(6, f"dist|{d['imdb_id']}", d["current"] or "(none)", d["new"], d["why"],
            {"imdb_id": d["imdb_id"], "field": "distributor", "value": d["new"]},
            {"label": d["title_name"], "film": d["title_name"], "imdb_id": d["imdb_id"],
             "in_mmm": in_mmm_text[d["in_mmm"]], "now": d["current"] or "(none)",
             "proposed": d["new"]})
    return items


def explain_note(note: str) -> str:
    """Turn a stored review note into a plain-English problem description."""
    m = re.fullmatch(r"IMDb ID also used for: (.*)\. Check which name is right\.", note or "")
    if m:
        return (f"In the old title dictionary, this film's IMDb ID was also given to: {m.group(1)}. "
                "Either that was a code name for this film (then nothing to fix), or the other "
                "film has the wrong IMDb ID (then add it on Add films and relink its product code "
                "on Map codes). When checked, choose Resolved.")
    return note


def item_check(item: dict) -> str:
    """Short fingerprint of an item's current and proposed values (stored in the workbook)."""
    raw = f"{text(item['current'])}|{text(item['suggested'])}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


class Plan:
    """Collects changes against in-memory copies of the tables and checks them."""

    def __init__(self, titles: dict, aliases: dict, enrichment: dict, chtt: dict | None = None):
        self.titles = {k: dict(v) for k, v in titles.items()}
        self.aliases = {k: dict(v) for k, v in aliases.items()}
        self.enrichment = enrichment
        self.chtt = chtt or {}
        self.changes: list[dict] = []
        self.problems: list[str] = []
        self.skipped: list[str] = []
        self.new_titles: set[str] = set()
        self.reason = ""
        self.where = ""
        self.genre_source: str | None = None   # source label for the next imdb_genres change
        self.auto_films: list[dict] = []        # films changed by the automatic release rule
        self._set_by: dict[tuple, object] = {}

    # -- low level -------------------------------------------------------------
    def problem(self, msg: str):
        self.problems.append(f"{self.where}: {msg}" if self.where else msg)

    def _record(self, table, key, field, old, new):
        k = (table, key, field)
        if k in self._set_by:
            if self._set_by[k] != new:
                self.problem(f"{key} {field} is set to two different values in this workbook")
            return                      # same value again: already recorded
        self._set_by[k] = new
        if old != new:
            self.changes.append({"table": table, "key": key, "field": field, "old": old,
                                 "new": new, "reason": self.reason, "where": self.where})

    def _title_from_enrichment(self, imdb):
        e = self.enrichment.get(imdb)
        if not e:
            return False
        self.add_film({"imdb_id": imdb, "title_name": display_name(e["title_name"]),
                       "distributor": e.get("distributor"), "release_date": e.get("release_date"),
                       "pvod_date": e.get("pvod_date"), "est_date": e.get("est_date")})
        return True

    # -- operations ------------------------------------------------------------
    def add_film(self, row: dict):
        imdb = text(row.get("imdb_id"))
        if not IMDB_RE.match(imdb):
            self.problem(f"'{imdb}' is not an IMDb ID like tt1234567")
            return
        if imdb in self.titles:
            if imdb not in self.new_titles:
                self.problem(f"{imdb} is already in the title table "
                             f"({self.titles[imdb]['title_name']}); use the Change films tab")
            return
        if not text(row.get("title_name")):
            self.problem(f"{imdb} needs a title_name")
            return
        new = {f.name: None for f in T.TITLE_SCHEMA}
        new["imdb_id"] = imdb
        self.titles[imdb] = new
        self.new_titles.add(imdb)
        for field in ("title_name", "distributor", *DATE_FIELDS):
            raw = row.get(field)
            value = text(raw) or None
            if field in DATE_FIELDS and value is not None:
                value = parse_date(raw)
                if value is None:
                    self.problem(f"{imdb} {field} '{text(raw)}' is not a date like 2026-06-30")
                    continue
            if value is not None:
                new[field] = (value.upper() if field == "title_name" else
                              standard_distributor(value) if field == "distributor" else value)
                self._record(config.TITLE_TABLE, imdb, field, None, new[field])

    def set_value(self, imdb: str, field: str, value):
        imdb, field = text(imdb), text(field)
        if imdb not in self.titles:
            self.problem(f"{imdb or '(blank)'} is not in the title table")
            return
        if field not in SETTABLE_FIELDS:
            self.problem(f"'{field}' cannot be changed; use one of " + ", ".join(SETTABLE_FIELDS))
            return
        if field in DATE_FIELDS and text(value):
            parsed = parse_date(value)
            if parsed is None:
                self.problem(f"{imdb} {field} '{text(value)}' is not a date like 2026-06-30")
                return
            value = parsed
        elif field == "in_mmm":
            value = parse_bool(value)
            if value is None:
                self.problem(f"In MMM for {imdb} must be yes or no")
                return
        else:
            value = text(value) or None
            if field in ("title_name", *GENRE_FIELDS) and value:
                value = value.upper()
            if field == "distributor":
                value = standard_distributor(value)
            if field == "imdb_genres":
                value = genre_list(value)
        row = self.titles[imdb]
        self._record(config.TITLE_TABLE, imdb, field, row.get(field), value)
        row[field] = value
        if field in DATE_FIELDS:
            self._settle_date(imdb, field, value)
        if field == "imdb_genres":
            source = self.genre_source or HAND_SOURCE
            self._record(config.TITLE_TABLE, imdb, "imdb_genres_source",
                         row.get("imdb_genres_source"), source if value else None)
            row["imdb_genres_source"] = source if value else None
            self.genre_source = None
        if field == "in_mmm":
            self._record(config.TITLE_TABLE, imdb, "in_mmm_reason", row.get("in_mmm_reason"),
                         self.reason)
            row["in_mmm_reason"] = self.reason

    def _settle_date(self, imdb: str, field: str, final):
        """
        A person has decided this date, so every NBC / Finance value that differs from it is
        turned down too (recorded like Keep as is). Otherwise the other source's date would
        come back on the next workbook as a new proposal.
        """
        proposed: dict[str, list[str]] = {}
        for source, value in (("NBC", self.enrichment.get(imdb, {}).get(field)),
                              ("Finance", self.chtt.get(imdb, {}).get(field))):
            if value and value != final:
                proposed.setdefault(value, []).append(source)
        for value, sources in proposed.items():
            self._record(REVIEW_TABLE, f"date|{imdb}|{field}|{' + '.join(sources)}", "reject",
                         None, value)

    def _map(self, alias_type, group, value, category, imdb, label):
        if category == "film" and imdb not in self.titles and not self._title_from_enrichment(imdb):
            self.problem(f"{imdb} is not in the title table or NBC's enrichment table; add it on "
                         "the Add films tab")
            return
        key = (alias_type, group, value)
        row = self.aliases.get(key)
        if row is None:
            row = {f.name: None for f in T.ALIAS_SCHEMA}
            row.update(alias_type=alias_type, business_group=group, alias_value=value,
                       label=label)
            self.aliases[key] = row
        rkey = "|".join(str(x or "") for x in key)
        for field, new in (("category", category), ("imdb_id", imdb)):
            self._record(config.TITLE_ALIAS_TABLE, rkey, field, row.get(field), new)
            row[field] = new

    def map_code(self, group, code, film, label=None):
        g = text(group).upper()
        g = config.MEDIA_BUSINESS_GROUPS.get(g, g)
        if g not in set(config.MEDIA_BUSINESS_GROUPS.values()):
            self.problem(f"business code '{text(group)}' should be ZUE, ZUV, ZFF or ZFE")
            return
        code, film = text(code).upper(), text(film)
        if not code:
            self.problem("the product code is blank")
        elif film.lower() in ("non_film", "non-film", "nonfilm", "non film") \
                or re.fullmatch(r"tt0+", film.lower()):   # the old tt00000000 placeholder
            self._map("media_product_code", g, code, "non_film", None, label or code)
        elif not IMDB_RE.match(film):
            self.problem(f"'{film}' should be an IMDb ID like tt1234567 or non_film")
        else:
            self._map("media_product_code", g, code, "film", film, label or code)

    def map_pid(self, product_id, film, label=None):
        pid, film = text(product_id), text(film)
        if not pid.isdigit():
            self.problem(f"'{pid}' is not a product ID")
        elif not IMDB_RE.match(film):
            self.problem(f"'{film}' is not an IMDb ID like tt1234567")
        else:
            self._map("revenue_product_id", None, pid, "film", film, label or pid)

    def reject(self, item: dict):
        self._record(REVIEW_TABLE, item["key"], "reject", None, item["suggested"])

    def decide(self, item: dict, decision: str, use_instead):
        """Apply one Review-tab decision to its item."""
        t, section = item["target"], item["section"]
        if decision == "reject":
            if item["has_suggestion"] or section == 4:
                self.reject(item)        # a product ID kept unlinked does not come back
            return
        if decision == "approve":
            if not item["has_suggestion"]:
                self.problem(f"{item['what']} has no proposed fix to accept; choose 'Use my answer' "
                             "and fill in Your answer")
                return
            value = t.get("imdb_id") if section in (1, 4) else t.get("value")
            if section == 1 and t["imdb_id"] is None:
                value = "non_film"
        else:
            value = use_instead
            if not text(value) and section != 2:
                self.problem(f"{item['what']}: 'Use my answer' is chosen but Your answer is empty")
                return
        if section == 1:
            self.map_code(t["group"], t["code"], value, t["label"])
        elif section == 2:
            self.set_value(t["imdb_id"], "review_note", value)
        elif section == 7:
            self.genre_source = t["source"] if decision == "approve" else HAND_SOURCE
            self.set_value(t["imdb_id"], "imdb_genres", value)
        elif section in (3, 5, 6):
            self.set_value(t["imdb_id"], t["field"], value)
        else:
            self.map_pid(t["product_id"], value, t["label"])


RELEASE_RULE_WHERE = "Automatic rule"
RELEASE_RULE_REASON = ("Released before {year}: Home Entertainment ran PVOD media from {year} "
                       "(automatic rule)")


def apply_release_rule(plan: Plan) -> int:
    """Set In MMM = no for every film released before config.MMM_FIRST_RELEASE_DATE (the MMM
    only uses titles from then on). Returns how many films it changes."""
    first = config.MMM_FIRST_RELEASE_DATE
    before = len(plan.changes)
    films = 0
    for imdb, row in sorted(plan.titles.items()):
        released = parse_date(row.get("release_date"))
        if released and released < first and row.get("in_mmm") is not False:
            plan.auto_films.append({"imdb_id": imdb, "title": row.get("title_name"),
                                    "released": released, "was": row.get("in_mmm")})
            plan.where = RELEASE_RULE_WHERE
            plan.reason = RELEASE_RULE_REASON.format(year=first[:4])
            plan.set_value(imdb, "in_mmm", "no")
            films += 1
    plan.where = plan.reason = ""
    return films if len(plan.changes) > before else 0


def plan_from_workbook(data: dict, items: list[dict], titles: dict, aliases: dict,
                       enrichment: dict, chtt: dict | None = None) -> Plan:
    """Turn the rows read from a review workbook into a checked Plan."""
    plan = Plan(titles, aliases, enrichment, chtt)
    by_key = {i["key"]: i for i in items}

    def start(tab, row, reason) -> bool:
        plan.where, plan.reason = f"{tab} row {row}", text(reason)
        if not plan.reason:
            plan.problem("the Reason column is empty")
            return False
        return True

    for r in data.get("add_films", []):
        if start("Add films", r["row"], r.get("reason")):
            plan.add_film(r)
    for r in data.get("review", []):
        decision = text(r.get("decision")).lower()
        if not decision:
            continue
        tab = r.get("tab", "Review")
        plan.where = f"{tab} row {r['row']}"
        if decision not in {"approve", "use instead", "reject"}:
            plan.problem(f"Decision '{text(r.get('raw_decision') or r.get('decision'))}' is not "
                         "one of the choices in the dropdown")
            continue
        key, _, check = text(r.get("key")).partition("#")
        item = by_key.get(key)
        label = text(r.get("label")) or key
        if item is None:
            plan.skipped.append(f"{tab} row {r['row']} ({label}): no longer needs a decision "
                                "(already handled, or the data changed). Not applied.")
            continue
        if check != item_check(item):
            plan.skipped.append(f"{tab} row {r['row']} ({label}): the values changed after this "
                                "workbook was issued. Not applied; it will be on the next workbook.")
            continue
        if start(tab, r["row"], r.get("reason")):
            plan.decide(item, decision, r.get("use_instead"))
    for r in data.get("map_codes", []):
        if start("Map codes", r["row"], r.get("reason")):
            plan.map_code(r.get("business_code"), r.get("product_code"), r.get("film"))
    for r in data.get("map_product_ids", []):
        if start("Map product IDs", r["row"], r.get("reason")):
            plan.map_pid(r.get("product_id"), r.get("film"))
    for r in data.get("change_films", []):
        if start("Change films", r["row"], r.get("reason")):
            plan.set_value(r.get("film"), r.get("field"), r.get("new_value"))
    plan.where = ""
    return plan


# =============================================================================
# Google Cloud steps
# =============================================================================

def _core(name):
    return config.table_id(config.DATASET_CORE, name)


def _raw(name):
    return config.table_id(config.DATASET_RAW, name)


def _rows(client, sql, params=()):
    cfg = bigquery.QueryJobConfig(query_parameters=list(params))
    return [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]


def _read_tables(client):
    titles = {}
    for r in _rows(client, f"SELECT * FROM `{_core(config.TITLE_TABLE)}`"):
        for f in DATE_FIELDS:
            r[f] = parse_date(r[f])
        titles[r["imdb_id"]] = r
    aliases = {(r["alias_type"], r["business_group"], r["alias_value"]): r
               for r in _rows(client, f"SELECT * FROM `{_core(config.TITLE_ALIAS_TABLE)}`")}
    return titles, aliases


def _read_rejections(client) -> set[tuple[str, str]]:
    sql = (f"SELECT record_key, new_value FROM `{_core(config.TITLE_HISTORY_TABLE)}` "
           f"WHERE table_name = '{REVIEW_TABLE}' AND field = 'reject'")
    return {(r["record_key"], r["new_value"]) for r in _rows(client, sql)}


def _latest(table):
    return f"_delivery_date = (SELECT MAX(_delivery_date) FROM `{_raw(table)}`)"


def _read_enrichment(client, ids, tags):
    """NBC enrichment rows for films in the title table, recent UP/Focus films and tagged IDs."""
    t = config.REFERENCE_SOURCES["enrichment_title"]["table"]
    sql = f"""
        SELECT imdb_id, ANY_VALUE(title_name) AS title_name,
               ANY_VALUE(studio_distributor) AS distributor,
               ANY_VALUE(us_widest_release_date) AS release_date,
               ANY_VALUE(us_pvod_start_date) AS pvod_date, ANY_VALUE(us_est_date) AS est_date,
               ANY_VALUE(master_genre_1) AS master_genre_1,
               ANY_VALUE(master_genre_2) AS master_genre_2
        FROM `{_raw(t)}`
        WHERE {_latest(t)}
          AND (imdb_id IN UNNEST(@ids)
               OR (UPPER(studio_distributor) LIKE '%UNIVERSAL%'
                   OR UPPER(studio_distributor) LIKE '%FOCUS%')
                  AND us_widest_release_date >= '2019')
        GROUP BY imdb_id
    """
    out = {}
    for r in _rows(client, sql, [bigquery.ArrayQueryParameter("ids", "STRING",
                                                              sorted(set(ids) | set(tags)))]):
        for f in DATE_FIELDS:
            r[f] = parse_date(r[f])
        out[r["imdb_id"]] = r
    return out


def _read_chtt(client, ids):
    t = config.REFERENCE_SOURCES["std_chtt"]["table"]
    sql = f"""
        SELECT imdb_id, ANY_VALUE(premium_release_date) AS pvod_date,
               ANY_VALUE(est_release_date) AS est_date
        FROM `{_raw(t)}` WHERE {_latest(t)} AND imdb_id IN UNNEST(@ids) GROUP BY imdb_id
    """
    out = {}
    for r in _rows(client, sql, [bigquery.ArrayQueryParameter("ids", "STRING", sorted(ids))]):
        out[r["imdb_id"]] = {f: parse_date(r[f]) for f in ("pvod_date", "est_date")}
    return out


def _read_unmapped_codes(client, aliases):
    """Codes in the newest media snapshot with no alias row, with row counts and IMDb tags."""
    groups = config.MEDIA_BUSINESS_GROUPS
    codes = ", ".join(f"'{c}'" for c in groups)
    latest = lambda t: f"_snapshot_date = {media_snapshot_expr(t)}"  # noqa: E731
    sql = f"""
        WITH m AS (
          SELECT AdvertiserCode AS biz, ProductCode AS code, ProductName AS name
          FROM `{_raw('prisma_placements')}` WHERE {latest('prisma_placements')}
          UNION ALL
          SELECT Client_Code, Product_Code, Product_Name
          FROM `{_raw('mediaocean_netpak')}` WHERE {latest('mediaocean_netpak')}
          UNION ALL
          SELECT Client_Code, Product_Code, Product_Name
          FROM `{_raw('mediaocean_spotpak')}` WHERE {latest('mediaocean_spotpak')}
        ),
        tags AS (
          SELECT TRIM(AdvertiserCode) AS biz, TRIM(ProductCode) AS code,
                 ARRAY_AGG(DISTINCT LOWER(REGEXP_EXTRACT(CampaignName,
                                                         r'(?i)imd\\.(tt[0-9]{{7,9}})'))) AS tags
          FROM `{_raw('prisma_placement_buys')}` WHERE {latest('prisma_placement_buys')}
            AND REGEXP_CONTAINS(CampaignName, r'(?i)imd\\.tt[0-9]')
          GROUP BY 1, 2
        ),
        media AS (
          SELECT TRIM(biz) AS biz, TRIM(code) AS code, MAX(name) AS name, COUNT(*) AS n
          FROM m WHERE TRIM(biz) IN ({codes}) AND TRIM(IFNULL(code, '')) != ''
          GROUP BY 1, 2
        )
        SELECT media.*, tags.tags
        FROM media LEFT JOIN tags USING (biz, code)
    """
    merged: dict[tuple, dict] = {}
    for r in _rows(client, sql):
        key = (groups[r["biz"]], r["code"])
        if ("media_product_code", *key) in aliases:
            continue
        m = merged.setdefault(key, {"business_group": key[0], "product_code": key[1],
                                    "sample_name": r["name"], "rows": 0, "tags": set()})
        m["rows"] += r["n"]
        m["tags"].update(r["tags"] or [])
    for m in merged.values():
        m["tags"] = sorted(m["tags"])
    return list(merged.values())


def media_snapshot_expr(table: str) -> str:
    """
    SQL for the media snapshot to read: the newest snapshot whose load finished successfully
    (load_registry 'media_snapshot' SUCCESS row), so a half-finished load is never used.
    Falls back to the newest snapshot in the table when the log has no such row.
    """
    reg = config.table_id(config.DATASET_RAW, config.LOAD_REGISTRY_TABLE)
    return (f"COALESCE((SELECT SAFE.PARSE_DATE('%Y%m%d', delivery) FROM `{reg}` "
            f"WHERE source = 'media_snapshot' AND status = 'SUCCESS' "
            f"ORDER BY finished_at DESC LIMIT 1), "
            f"(SELECT MAX(_snapshot_date) FROM `{_raw(table)}`))")


def basics_genres_from_csv(path) -> dict[str, tuple[str, str]]:
    """{imdb: (genres, source)} from the Amazon MMT Pipeline's IMDb title.basics CSV."""
    import csv

    out = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            g = genre_list(r.get("genres"))
            if r.get("imdb_id") and g:
                snap = text(r.get("snapshot_id"))[:12] or "unknown"
                out[r["imdb_id"].strip()] = (g, f"{IMDB_BASICS_SOURCE}, snapshot {snap}")
    return out


def _imdb_genre_proposals(seed_dir=None) -> dict[str, tuple[str, str]]:
    """
    IMDb genre lists to propose, from reference_seed/:
      - the IMDb website genre file (finer tags; wins where a film is in both), and
      - the newest IMDb title.basics CSV from the Amazon MMT Pipeline (basic genres).
    """
    import json

    seed_dir = seed_dir or T.SEED_DIR
    out: dict[str, tuple[str, str]] = {}
    basics = sorted(seed_dir.glob(IMDB_BASICS_PATTERN))
    if basics:
        out.update(basics_genres_from_csv(basics[-1]))
    path = seed_dir / IMDB_GENRE_FILE
    if path.exists():
        data = json.loads(path.read_text())
        out.update({k: (v, IMDB_WEBSITE_SOURCE) for k, v in imdb_genres_from_file(data).items()})
    return out


def _read_amazon(client, ids) -> tuple[dict, set[str], str]:
    """
    First Amazon revenue week per film and the missing Amazon weeks (owned by the Amazon MMT
    Pipeline). Returns ({imdb: row}, missing_week_starts, note). Never fails the review: if the
    tables cannot be read, the check is left out and the note says why.
    """
    try:
        rows = _rows(client, f"""
            SELECT imdb_id, first_revenue_week, first_week_over_threshold,
                   first_threshold_week_avg_price, first_revenue_week_quality, usable_as_evidence
            FROM `{config.AMAZON_FIRST_REVENUE_TABLE}` WHERE imdb_id IN UNNEST(@ids)""",
            [bigquery.ArrayQueryParameter("ids", "STRING", sorted(ids))])
        weeks = _rows(client, f"SELECT * FROM `{config.AMAZON_WEEK_QUALITY_TABLE}`")
    except GoogleAPIError as exc:
        msg = getattr(exc, "message", None) or str(exc)
        return {}, set(), f"Amazon check left out (could not read the Amazon tables: {msg[:200]})"
    missing = set()
    for w in weeks:
        status = text(next((w[k] for k in w if k.lower() == "status"), "")).upper()
        week = next((w[k] for k in ("week_start", "week_start_date", "amazon_week", "week")
                     if k in w), None)
        if status == "MISSING" and parse_date(week):
            missing.add(parse_date(week))
    return {r["imdb_id"]: r for r in rows}, missing, ""


def _read_unmapped_pids(client, titles, aliases):
    t = config.REVENUE_RAW_TABLE
    sql = f"""
        SELECT CAST(product_id1 AS STRING) AS product_id, ANY_VALUE(imdb_title_id) AS imdb_id,
               SUM(SAFE_CAST(royalty_total_usd AS FLOAT64)) AS royalty,
               APPROX_TOP_COUNT(reported_product_name, 1)[SAFE_OFFSET(0)].value AS name,
               MIN(transaction_date) AS first_sale, MAX(transaction_date) AS last_sale
        FROM `{_raw(t)}`
        WHERE {_latest(t)} AND imdb_title_id IN UNNEST(@ids)
        GROUP BY 1
    """
    rows = _rows(client, sql, [bigquery.ArrayQueryParameter("ids", "STRING", sorted(titles))])
    return [r for r in rows if r["product_id"]
            and ("revenue_product_id", None, r["product_id"]) not in aliases]


def _read_untagged_pids(client, titles, aliases):
    """Unlinked product IDs whose rows have no IMDb ID, first sold since the MMM's first year."""
    t = config.REVENUE_RAW_TABLE
    exclude = ", ".join(f"'{x}'" for x in config.REVENUE_EXCLUDE_SOURCES) or "''"
    day = "SAFE.PARSE_DATE('%Y-%m-%d', TRIM(transaction_date))"
    sql = f"""
        SELECT TRIM(CAST(product_id1 AS STRING)) AS product_id,
               APPROX_TOP_COUNT(TRIM(reported_product_name), 1)[SAFE_OFFSET(0)].value AS name,
               SUM(IF(TRIM(country_cd) = '{config.REVENUE_COUNTRY}'
                      AND IFNULL(TRIM(data_source), '') NOT IN ({exclude}),
                      SAFE_CAST(REPLACE(REPLACE(TRIM(royalty_total_usd), '$', ''), ',', '')
                                AS FLOAT64), 0)) AS royalty,
               CAST(MIN({day}) AS STRING) AS first_sale, CAST(MAX({day}) AS STRING) AS last_sale
        FROM `{_raw(t)}`
        WHERE {_latest(t)} AND NULLIF(TRIM(imdb_title_id), '') IS NULL
        GROUP BY 1
        HAVING MIN({day}) >= DATE_SUB(DATE '{config.MMM_FIRST_RELEASE_DATE}',
                                      INTERVAL {UNTAGGED_DAYS_BEFORE_RELEASE} DAY)
    """
    rows = [r for r in _rows(client, sql) if r["product_id"]
            and ("revenue_product_id", None, r["product_id"]) not in aliases]
    linked = {a["imdb_id"] for (kind, _g, _v), a in aliases.items()
              if kind == "revenue_product_id" and a.get("imdb_id")}
    return match_untagged(rows, titles, linked)


def gather(client=None) -> dict:
    """Read everything the review needs and build the list."""
    client = client or bq_client()
    titles, aliases = _read_tables(client)
    codes = _read_unmapped_codes(client, aliases)
    tags = {t for c in codes for t in c["tags"]}
    enrichment = _read_enrichment(client, titles, tags)
    chtt = _read_chtt(client, titles)
    dates = date_differences(titles, enrichment, chtt)
    pids = _read_unmapped_pids(client, titles, aliases) + _read_untagged_pids(client, titles,
                                                                               aliases)
    amazon, missing, amazon_note = _read_amazon(client, titles)
    items = build_items(codes, titles, enrichment, dates, pids, _read_rejections(client),
                        genres=genre_differences(titles, enrichment), amazon=amazon,
                        missing_weeks=missing,
                        distributors=distributor_differences(titles, enrichment),
                        imdb_genres=imdb_genre_differences(titles, _imdb_genre_proposals()))
    return {"titles": titles, "aliases": aliases, "enrichment": enrichment, "chtt": chtt,
            "items": items, "amazon_note": amazon_note}


def _json_rows(rows, schema):
    names = [f.name for f in schema]
    return [{k: (r.get(k).isoformat() if isinstance(r.get(k), (datetime, date)) else r.get(k))
             for k in names} for r in rows]


def _apply(client, plan: Plan) -> int:
    who, now = current_user(), datetime.now(timezone.utc)
    reason_by = {}
    for c in plan.changes:
        reason_by.setdefault((c["table"], c["key"]), c["reason"])
    touched_titles = {k for t, k in reason_by if t == config.TITLE_TABLE}
    touched_aliases = {k for t, k in reason_by if t == config.TITLE_ALIAS_TABLE}
    for imdb in touched_titles:
        plan.titles[imdb].update(updated_at=now, updated_by=who)
    for key, row in plan.aliases.items():
        rkey = "|".join(str(x or "") for x in key)
        if rkey in touched_aliases:
            row.update(updated_at=now, updated_by=who,
                       reason=reason_by[(config.TITLE_ALIAS_TABLE, rkey)])
    history = [{"change_id": uuid.uuid4().hex, "table_name": c["table"], "record_key": c["key"],
                "field": c["field"],
                "old_value": None if c["old"] is None else str(c["old"]),
                "new_value": None if c["new"] is None else str(c["new"]),
                "reason": c["reason"], "changed_by": who, "changed_at": now}
               for c in plan.changes]
    if touched_titles:
        T._write(client, config.TITLE_TABLE, T.TITLE_SCHEMA,
                 _json_rows(plan.titles.values(), T.TITLE_SCHEMA))
    if touched_aliases:
        T._write(client, config.TITLE_ALIAS_TABLE, T.ALIAS_SCHEMA,
                 _json_rows(plan.aliases.values(), T.ALIAS_SCHEMA))
    job = client.load_table_from_json(
        _json_rows(history, T.HISTORY_SCHEMA), _core(config.TITLE_HISTORY_TABLE),
        job_config=bigquery.LoadJobConfig(
            schema=T.HISTORY_SCHEMA, write_disposition=bigquery.WriteDisposition.WRITE_APPEND))
    job.result()
    return len(history)


def section_counts(items: list[dict]) -> dict[int, int]:
    return {s: sum(i["section"] == s for i in items) for s in SECTION_NAMES}


def review_folders(root: str | Path | None = None) -> dict[str, Path]:
    """issued / returned / imported folders in the Domino dataset (created when missing)."""
    root = Path(root or config.REVIEW_DIR)
    out = {name: root / name for name in ("issued", "returned", "imported")}
    for p in out.values():
        p.mkdir(parents=True, exist_ok=True)
    return out


def returned_workbooks(folder: Path) -> list[Path]:
    """Workbooks waiting in the returned folder, oldest first (Excel lock files ignored)."""
    files = [p for p in folder.glob("*.xlsx") if not p.name.startswith("~$")]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def move_to_imported(path: Path, imported: Path) -> Path:
    """Move a used workbook out of 'returned'; never overwrite an earlier one."""
    target = imported / path.name
    n = 2
    while target.exists():
        target = imported / f"{path.stem}_{n}{path.suffix}"
        n += 1
    path.rename(target)
    return target


def run(dry_run: bool = True, root: str | Path | None = None) -> dict:
    """
    Nothing in 'returned' -> issue a new review workbook into 'issued'.
    A workbook in 'returned' -> read its decisions (oldest first) and check them; when
    dry_run is False, save them and move the workbook to 'imported'.
    """
    from us_hment_mmm.reference import review_workbook as W

    folders = review_folders(root)
    client = bq_client()
    g = gather(client)
    waiting = returned_workbooks(folders["returned"])
    result = {"counts": section_counts(g["items"]), "amazon_note": g["amazon_note"],
              "changes": [], "problems": [],
              "skipped": [], "history_rows": 0, "workbook": None, "moved_to": None,
              "waiting": [p.name for p in waiting[1:]], "folders": {k: str(v) for k, v in
                                                                   folders.items()}}
    if not waiting:
        plan = Plan(g["titles"], g["aliases"], g["enrichment"], g["chtt"])
        result["auto_films"] = apply_release_rule(plan)
        result["auto_list"] = plan.auto_films
        result["changes"] = plan.changes
        if plan.changes and not dry_run:
            result["history_rows"] = _apply(client, plan)
            result["auto_saved"] = True
            g = gather(client)
            result["counts"] = section_counts(g["items"])
        result["workbook"] = W.write_workbook(g["items"], g["titles"], folders["issued"],
                                              g["aliases"]).name
        result["status"] = "ISSUED"
        return result
    path = waiting[0]
    result["workbook"] = path.name
    data, read_problems = W.read_workbook(path)
    plan = plan_from_workbook(data, g["items"], g["titles"], g["aliases"], g["enrichment"],
                              g["chtt"])
    result["auto_films"] = apply_release_rule(plan)
    result["auto_list"] = plan.auto_films
    result.update(changes=plan.changes, problems=read_problems + plan.problems,
                  skipped=plan.skipped)
    if result["problems"]:
        result["status"] = "PROBLEMS"
    elif dry_run:
        result["status"] = "READY" if plan.changes else "NOTHING"
    else:
        if plan.changes:
            result["history_rows"] = _apply(client, plan)
            result["counts"] = section_counts(gather(client)["items"])   # the list after saving
        result["moved_to"] = move_to_imported(path, folders["imported"]).name
        result["status"] = "APPLIED" if plan.changes else "IMPORTED_NOTHING"
    return result


# =============================================================================
# Notebook tables
# =============================================================================

def changes_table(changes: list[dict]):
    import pandas as pd

    from us_hment_mmm.display import table_or_note

    names = {config.TITLE_TABLE: "film", config.TITLE_ALIAS_TABLE: "mapping",
             REVIEW_TABLE: "rejected suggestion"}
    rows = [{"where": c["where"], "what": names.get(c["table"], c["table"]), "key": c["key"],
             "field": c["field"], "old": c["old"], "new": c["new"], "reason": c["reason"]}
            for c in changes if c["where"] != RELEASE_RULE_WHERE]
    df = pd.DataFrame(rows, columns=["where", "what", "key", "field", "old", "new", "reason"])
    df = df.astype(object).where(df.notna(), "(empty)")
    return table_or_note(df, "No changes.",
                         labels={"where": "Workbook row", "what": "Change", "key": "Film or code",
                                 "field": "Field", "old": "Old value", "new": "New value",
                                 "reason": "Reason"})


def auto_rule_table(result: dict):
    """One row per film changed by the automatic release rule (shown under Step 3)."""
    import pandas as pd
    from IPython.display import Markdown

    from us_hment_mmm.display import styled

    films = result.get("auto_list") or []
    if not films:
        return Markdown("ℹ️ Automatic rule: no films to change.")
    year = config.MMM_FIRST_RELEASE_DATE[:4]
    saved = "were set" if result.get("auto_saved") or result.get("status") == "APPLIED" \
        else "will be set"
    df = pd.DataFrame([{"Film": f["title"], "IMDb ID": f["imdb_id"], "Released": f["released"],
                        "In MMM before": {True: "yes", False: "no"}.get(f["was"], "(not set)")}
                       for f in sorted(films, key=lambda f: (f["released"], f["title"] or ""))])
    from IPython.display import display
    display(Markdown(f"**Automatic rule:** these {len(films)} film(s) released before {year} "
                     f"{saved} to In MMM = no."))
    return styled(df)


def where_to_find(folder: str, root: str | Path | None = None) -> str:
    """Plain directions to a review folder, both in the Domino web page and as a path."""
    root = Path(root or config.REVIEW_DIR)
    parts = root.parts
    dataset = parts[4] if len(parts) > 4 and parts[1:4] == ("domino", "datasets", "local") \
        else root.name
    inside = " → ".join(parts[5:] + (folder,)) if len(parts) > 5 else folder
    return (f"In Domino, open this project's **Data** page → **Datasets** → **{dataset}**, then "
            f"click through **{inside}**.  \n(Folder path: `{root / folder}`)")


def status_markdown(result: dict):
    """The Step 2 message for the review notebook (kept here so wording changes never
    require replacing the notebook)."""
    from IPython.display import Markdown

    c = result["counts"]
    counts = (f"On the review list now: **{c[1]}** product codes, **{c[2]}** review notes, "
              f"**{c[3]}** dates, **{c[4]}** product IDs, **{c.get(5, 0)}** NBC genres, "
              f"**{c.get(6, 0)}** distributors, **{c.get(7, 0)}** IMDb genre lists.")
    if result.get("amazon_note"):
        counts += f"  \nℹ️ {result['amazon_note']}"
    n, status, name = len(result["changes"]), result["status"], result["workbook"]
    upload = ("**Where to upload it:** " + where_to_find("returned")
              + "  \nUse the **Upload files** button on that page. If a copy with the same name "
              "is already there, replace it.")
    auto = result.get("auto_films") or 0
    year = config.MMM_FIRST_RELEASE_DATE[:4]
    if auto and result.get("auto_saved"):
        counts += (f"  \n✅ Saved the automatic rule: **{auto}** film(s) released before {year} "
                   "are now In MMM = no (listed under Step 3).")
    elif auto:
        counts += (f"  \nℹ️ Automatic rule: **{auto}** film(s) released before {year} will be set "
                   "to In MMM = no (listed under Step 3). They are saved the next time you run with "
                   "`DRY_RUN = False`.")
    if status == "ISSUED" and not any(c.values()):
        head = "### ✅ Nothing needs a decision"
        body = ("Every section of the review list is empty, so there is nothing to decide.\n\n"
                f"A workbook was still made (`{name}`). It is only needed if you want to add or "
                "change a film by hand (tabs Add films, Change films, Map codes, Map product IDs). "
                "Otherwise you can ignore it.\n\n"
                f"**Where to find it:** {where_to_find('issued')}")
    elif status == "ISSUED":
        head = "### ✅ New workbook issued"
        body = (f"**File:** `{name}`\n\n**Where to find it:** {where_to_find('issued')}  \n"
                "Click the file name to download it.\n\n"
                "**What to do in Excel:** work through the numbered tabs (1 · Product codes, "
                "2 · Flagged films, 3 · Dates, 4 · Revenue product IDs, 5 · NBC genre, "
                "6 · Distributor, 7 · IMDb genres). Each tab "
                "starts with a "
                "blue box that says what the problem is and what each choice does. Fill in only "
                "the yellow columns, with a **Reason** for every row you decide. Save it as .xlsx "
                "(keep the name).\n\n"
                f"{upload}\n\n**Then:** come back here and **Run All** (still a check only).")
    elif status == "PROBLEMS":
        head = f"### 🛑 STOP — please fix these rows in `{name}`"
        body = ("\n".join(f"- {p}" for p in result["problems"])
                + "\n\nNothing was changed. Fix the rows in Excel, upload the file again, and "
                f"**Run All**.\n\n{upload}")
    elif status == "NOTHING":
        head = f"### ℹ️ `{name}` has no decisions that change anything"
        body = ("Check that Decision and Reason are filled in (see Step 4 for rows not applied). "
                "To file it away anyway, set `DRY_RUN = False` and **Run All**; it moves to the "
                "`imported` folder.")
    elif status == "READY":
        head = f"### ✅ PASS — `{name}`: {n} change(s) ready"
        body = ("Check every line in **Step 3** and anything in **Step 4**. If they are right, "
                "set `DRY_RUN = False` in Step 1 and **Run All** again.")
    elif status == "IMPORTED_NOTHING":
        head = f"### ℹ️ `{name}` filed with no changes"
        body = f"It was moved to the `imported` folder as `{result['moved_to']}`."
    else:
        head = f"### ✅ SAVED — {n} change(s) from `{name}`"
        body = (f"Saved with {result['history_rows']} history rows. The workbook moved to the "
                f"`imported` folder as `{result['moved_to']}`.\n\n**Next:** set "
                "`DRY_RUN = True` again in Step 1.")
    if result.get("waiting"):
        body += ("\n\n⏳ Also waiting in `returned` (handled one at a time, oldest first): "
                 + ", ".join(f"`{w}`" for w in result["waiting"]))
    return Markdown(f"{head}\n\n{body}\n\n{counts}")


def notes_markdown(lines: list[str], empty: str):
    from IPython.display import Markdown

    return Markdown("\n".join(f"- {x}" for x in lines) if lines else f"ℹ️ {empty}")
