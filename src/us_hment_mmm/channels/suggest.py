"""
Channel-label suggestions for Prisma placements, learned from the placements already labelled.

A small text model reads the site and the placement name (no large language model). It is
retrained from scratch every time the channel review runs, so every confirmed label is used.
Each suggestion is put in one of two groups:
  likely   the suggested MMM label has already been used on that site
  closer   anything else (a new site, or a site used for another channel before)
Suggestions are never used until a person confirms them.
"""

from __future__ import annotations

import re
from collections import defaultdict

import numpy as np

MIN_TRAINING_ROWS = 20


def clean_name(s) -> str:
    s = re.sub(r"^P[0-9A-Z]{6}_", "", str(s or "").upper())
    s = re.sub(r"\d{2}-\d{2}-\d{4}", " ", s)
    return s.replace("_", " ").replace("-", " ")


def site_key(s) -> str:
    return re.sub(r"[^0-9A-Z]+", " ", str(s or "").upper()).strip()


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
    """rows: {site, name, mediaplan_label, mmm_label}. Returns the model, or None if too few."""
    rows = [r for r in rows if r.get("mmm_label")]
    if len(rows) < MIN_TRAINING_ROWS:
        return None
    texts = [text_of(r["site"], r["name"]) for r in rows]
    seen = defaultdict(set)
    for r in rows:
        seen[site_key(r["site"])].add(r["mmm_label"])
    return {"mmm": _fit(texts, [r["mmm_label"] for r in rows]),
            "plan": _fit(texts, [r.get("mediaplan_label") or r["mmm_label"] for r in rows]),
            "seen": seen, "size": len(rows)}


def suggest(model: dict | None, items: list[dict]) -> list[dict]:
    """Adds suggested_mmm, suggested_plan and group ('likely' / 'closer') to each item."""
    if not items:
        return items
    if model is None or model["mmm"] is None:
        for it in items:
            it.update(suggested_mmm=None, suggested_plan=None, group="closer")
        return items
    texts = [text_of(it.get("site"), it.get("name")) for it in items]
    mmm = model["mmm"].predict(texts)
    plan = model["plan"].predict(texts) if model["plan"] is not None else mmm
    for it, m, p in zip(items, mmm, plan):
        likely = m in model["seen"].get(site_key(it.get("site")), set())
        it.update(suggested_mmm=str(m), suggested_plan=str(p), group="likely" if likely else "closer")
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
    likely = np.zeros(len(y), dtype=bool)
    for tr, te in GroupKFold(min(5, n_groups)).split(texts, y, groups):
        model = _fit(texts[tr], y[tr])
        if model is None:
            pred[te] = y[tr][0]
        else:
            pred[te] = model.predict(texts[te])
        seen = defaultdict(set)
        for i in tr:
            seen[sites[i]].add(y[i])
        for i in te:
            likely[i] = pred[i] in seen[sites[i]]
    ok = pred == y
    par = np.array([parent_of(p) == parent_of(t) for p, t in zip(pred, y)])
    return {"training_rows": len(rows), "exact": float(ok.mean()), "parent": float(par.mean()),
            "likely_share": float(likely.mean()),
            "likely_acc": float(ok[likely].mean()) if likely.any() else None,
            "closer_acc": float(ok[~likely].mean()) if (~likely).any() else None}
