"""Small helpers that make tables readable in analyst notebooks."""

from __future__ import annotations

import pandas as pd

from us_hment_mmm import config


def styled(df: pd.DataFrame, *, counts: list[str] = (), money: list[str] = (),
           times: list[str] = (), decimals: dict[str, int] | None = None,
           labels: dict[str, str] | None = None):
    """
    Return a pandas Styler with thousands separators, dollar amounts, local times,
    friendly column names, and no index column.
    """
    out = df.copy()
    for c in times:
        if c in out:
            ts = pd.to_datetime(out[c], utc=True).dt.tz_convert(config.DISPLAY_TIMEZONE)
            out[c] = ts.dt.strftime("%Y-%m-%d %H:%M")
    fmt = {}
    for c in counts:
        if c in out:
            fmt[c] = lambda v: "" if pd.isna(v) else f"{int(v):,}"
    for c in money:
        if c in out:
            fmt[c] = lambda v: "" if pd.isna(v) else f"${float(v):,.0f}"
    for c, places in (decimals or {}).items():
        if c in out:
            fmt[c] = lambda v, p=places: "" if pd.isna(v) else f"{float(v):,.{p}f}"
    if labels:
        fmt = {labels.get(k, k): v for k, v in fmt.items()}
        out = out.rename(columns=labels)
    return out.style.format(fmt).hide(axis="index")
