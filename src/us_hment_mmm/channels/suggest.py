"""
Channel-label suggestions for Prisma placements, learned from the placements already labelled.

A small text model reads the site and the placement name (no large language model). It is
retrained from scratch every time the channel review runs, so every confirmed label is used.
Each suggestion gets a "How sure" group:
  likely     the site has always had one label, it is the suggested one, and the client
             already has confirmed labels
  closer     the site has been labelled more than one way (e.g. YouTube: CTV, programmatic or
             streaming), or the client has no confirmed labels yet
  new_site   the model has never seen this site
Suggestions are never used until a person confirms them.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import numpy as np

MIN_TRAINING_ROWS = 20


def clean_name(s) -> str:
    s = re.sub(r"^P[0-9A-Z]{6}_", "", str(s or "").upper())
    s = re.sub(r"\d{2}-\d{2}-\d{4}", " ", s)
    return s.replace("_", " ").replace("-", " ")


SITE_NOISE = re.compile(r"\b(INC|LLC|LTD|CORP|CO|COMPANY|THE)\b")


def site_key(s) -> str:
    """Site name compared loosely: "Peacock.com" = "PEACOCK", "TIKTOK INC" = "TIKTOK",
    "SNAP INC FK SNAPCHAT" = "SNAPCHAT" (the name after FK / FKA, formerly known as)."""
    s = str(s or "").upper()
    s = re.split(r"\bFKA?\b", s)[-1]
    s = re.sub(r"\.(COM|NET|ORG|TV|CO)\b", " ", s)
    s = re.sub(r"[^0-9A-Z]+", " ", s)
    return re.sub(r"\s+", " ", SITE_NOISE.sub(" ", s)).strip()


def text_of(site, name) -> str:
    return f"{site or ''} | {clean_name(name)}"


def _pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import make_pipeline, make_union
    from sklearn.svm import LinearSVC

    feats = make_union(TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True),
                       TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True))
    return make_pipeline(feats, LinearSVC(C=1))


def _fit(texts, labels):
    if len(set(labels)) < 2:
        return None
    return _pipeline().fit(texts, labels)


def train(rows: list[dict]) -> dict | None:
    """rows: {site, name, mediaplan_label, mmm_label, client}. Returns the model, or None."""
    rows = [r for r in rows if r.get("mmm_label")]
    if len(rows) < MIN_TRAINING_ROWS:
        return None
    texts = [text_of(r["site"], r["name"]) for r in rows]
    seen = defaultdict(Counter)
    for r in rows:
        seen[site_key(r["site"])][r["mmm_label"]] += 1
    return {"mmm": _fit(texts, [r["mmm_label"] for r in rows]),
            "plan": _fit(texts, [r.get("mediaplan_label") or r["mmm_label"] for r in rows]),
            "seen": seen, "clients": {r["client"] for r in rows if r.get("client")},
            "size": len(rows)}


def group_for(seen: dict, clients: set, site, client, mmm) -> str:
    labels = seen.get(site_key(site))
    if not labels:
        return "new_site"
    if len(labels) > 1 or (clients and client and client not in clients):
        return "closer"
    return "likely" if mmm in labels else "closer"


def suggest(model: dict | None, items: list[dict]) -> list[dict]:
    """Adds suggested_mmm, suggested_plan and group (likely / closer / new_site) to each item."""
    if not items:
        return items
    if model is None or model["mmm"] is None:
        for it in items:
            it.update(suggested_mmm=None, suggested_plan=None, group="new_site")
        return items
    texts = [text_of(it.get("site"), it.get("name")) for it in items]
    mmm = model["mmm"].predict(texts)
    plan = model["plan"].predict(texts) if model["plan"] is not None else mmm
    for it, m, p in zip(items, mmm, plan):
        it.update(suggested_mmm=str(m), suggested_plan=str(p),
                  group=group_for(model["seen"], model["clients"], it.get("site"),
                                  it.get("client"), str(m)))
    return items


def evaluate(rows: list[dict], parent_of) -> dict | None:
    """
    Accuracy on films the model has not seen (each film is tested by a model trained without
    it). rows need site, name, mmm_label and film (an IMDb ID or any group key).
    """
    from sklearn.model_selection import GroupKFold

    rows = [r for r in rows if r.get("mmm_label")]
    groups = np.array([r.get("film") or r.get("key") or str(i) for i, r in enumerate(rows)])
    n_groups = len(set(groups))
    if len(rows) < MIN_TRAINING_ROWS or n_groups < 2:
        return None
    texts = np.array([text_of(r["site"], r["name"]) for r in rows])
    y = np.array([r["mmm_label"] for r in rows])
    sites = [site_key(r["site"]) for r in rows]
    pred = np.empty(len(y), dtype=object)
    group = np.empty(len(y), dtype=object)
    for tr, te in GroupKFold(min(5, n_groups)).split(texts, y, groups):
        model = _fit(texts[tr], y[tr])
        pred[te] = model.predict(texts[te]) if model is not None else y[tr][0]
        seen = defaultdict(Counter)
        for i in tr:
            seen[sites[i]][y[i]] += 1
        for i in te:
            group[i] = group_for(seen, set(), sites[i], None, pred[i])
    ok = pred == y
    par = np.array([parent_of(p) == parent_of(t) for p, t in zip(pred, y)])

    def acc(g):
        m = group == g
        return float(ok[m].mean()) if m.any() else None

    return {"training_rows": len(rows), "exact": float(ok.mean()), "parent": float(par.mean()),
            "likely_share": float((group == "likely").mean()), "likely_acc": acc("likely"),
            "closer_acc": acc("closer"), "new_acc": acc("new_site")}
