"""Notebook display for the core build steps (06_Build_Core)."""

from __future__ import annotations

import pandas as pd
from IPython.display import Markdown, display

from us_hment_mmm.display import styled

ICONS = {"CHECKED": "🔎", "PASS": "✅", "NOT_WRITTEN": "⚠️", "MISMATCH": "⚠️"}
HEADINGS = {"CHECKED": "CHECK ONLY — nothing was written",
            "PASS": "SAVED and checked",
            "NOT_WRITTEN": "NOT SAVED — needs a fix first",
            "MISMATCH": "SAVED, but the totals do not match"}


def _table(t: dict):
    df = pd.DataFrame(t["rows"], columns=t["columns"])
    st = styled(df, money=t.get("money", []), counts=t.get("counts", []))
    st = st.set_properties(subset=[t["columns"][0]], **{"text-align": "left", "white-space": "pre"})
    numbers = [c for c in t["columns"][1:]]
    if numbers:
        st = st.set_properties(subset=numbers, **{"text-align": "right"})
    st = st.set_table_styles([{"selector": "th", "props": "text-align: left; padding: 4px 12px;"},
                              {"selector": "td", "props": "padding: 4px 12px;"}])
    if t.get("bold_last") and len(df):
        last = len(df) - 1
        st = st.apply(lambda r: ["font-weight: bold" if r.name == last else "" for _ in r], axis=1)
    return st


def show_result(result: dict) -> None:
    """Status banner, key facts, tables and notes for one build step."""
    status = result["status"]
    display(Markdown(f"### {ICONS.get(status, '')} {HEADINGS.get(status, status)}\n\n"
                     f"{result['message']}\n\n{result.get('facts', '')}"))
    for t in result.get("tables", []):
        display(Markdown(f"**{t['title']}**"))
        if t["rows"]:
            display(_table(t))
        else:
            display(Markdown("*(nothing)*"))
    if result.get("notes"):
        display(Markdown("**Notes**\n\n" + "\n".join(f"- {n}" for n in result["notes"])))
