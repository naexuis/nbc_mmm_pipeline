"""
QA / leadership summary (notebook 08_QA_Summary). Read-only: nothing is written.

Everything comes from the core tables and the channel labels as they are now, so it always
reflects the newest 06_Build_Core build and the newest saved channel review (07):
  title, revenue_film_daily, revenue_premium_daily (revenue) and media_daily (media with labels).

Scope: titles released on or after config.MMM_FIRST_RELEASE_DATE (the MMM's titles), media spend
that has run (spend_status = 'actual') and is linked to a film.
"""

from __future__ import annotations

import pandas as pd

from us_hment_mmm import config
from us_hment_mmm.gcp import bq_client

# Colours (reference data-viz palette, light mode). Identity colours are used in fixed order;
# "no channel yet" is always the muted grey so it never looks like a channel.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
MUTED, INK, INK_2, GRID, AXIS, SURFACE = ("#898781", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7",
                                          "#fcfcfb")
SOURCES = ("Prisma", "NetPak", "SpotPak")               # fixed colour order for media sources
TOP_FILMS = 25
# Scatter: each distributor gets its own colour AND its own shape, so dots can be told apart
# even when two colours look alike. Most distinct colours go to the distributors with most titles.
SCATTER_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#eda100", "#008300", "#e87ba4",
                  "#e34948")
SYMBOLS = ("circle", "square", "diamond", "triangle-up", "triangle-down", "star", "cross", "x")
CHANNEL_NAMES = {"DISPLAY_VIDEO": "Display video", "CTVOTT": "CTV / OTT", "MVPD": "MVPD",
                 "OOH": "Out of home", "SPOTTV": "Spot TV", "FEE": "Fees"}


def _channel_name(code: str) -> str:
    if code == config.CHANNEL_NEEDS_LABEL:
        return "No channel yet"
    return CHANNEL_NAMES.get(code, str(code).replace("_", " ").title())


def _core(name: str) -> str:
    return config.table_id(config.DATASET_CORE, name)


def _df(client, sql: str) -> pd.DataFrame:
    """Run a query. BigQuery NUMERIC money comes back as Decimal; turn it into plain numbers."""
    from decimal import Decimal

    df = client.query(sql).result().to_dataframe()
    for c in df.columns:
        if df[c].dtype == object and df[c].map(lambda v: isinstance(v, Decimal)).any():
            df[c] = df[c].astype(float)
    return df


class Chart:
    """A finished chart. The notebook shows it as a picture (works in Jupyter and VS Code)."""

    def __init__(self, fig):
        self.fig = fig
        self.axes = fig.axes

    def _repr_png_(self):
        from io import BytesIO

        buf = BytesIO()
        self.fig.savefig(buf, format="png", dpi=110, facecolor=SURFACE)
        return buf.getvalue()

    def savefig(self, *args, **kwargs):
        self.fig.savefig(*args, **kwargs)


def load(client=None) -> dict[str, pd.DataFrame]:
    """Read everything the notebook shows (a handful of small queries)."""
    client = client or bq_client()
    first = config.MMM_FIRST_RELEASE_DATE
    media, title = _core(config.MEDIA_DAILY_VIEW), _core(config.TITLE_TABLE)
    in_scope = (f"JOIN `{title}` t USING (imdb_id) WHERE d.link = 'film' "
                f"AND d.spend_status = 'actual' AND t.release_date >= DATE '{first}'")
    return {
        "titles": _df(client, f"""
            SELECT COUNT(*) AS films, COUNTIF(release_date >= DATE '{first}') AS mmm_era,
                   COUNTIF(release_date >= DATE '{first}' AND in_mmm IS TRUE) AS in_mmm_yes,
                   COUNTIF(release_date >= DATE '{first}' AND in_mmm IS FALSE) AS in_mmm_no,
                   COUNTIF(release_date >= DATE '{first}' AND in_mmm IS NULL) AS in_mmm_unset
            FROM `{title}`"""),
        "dates": _df(client, f"""
            SELECT (SELECT MAX(snapshot_date) FROM `{media}`) AS media_snapshot,
                   (SELECT MAX(delivery_date) FROM `{_core(config.REVENUE_CORE_TABLE)}`)
                     AS revenue_delivery"""),
        "films": _df(client, f"""
            WITH m AS (
              SELECT d.imdb_id, SUM(IF(d.media_source = 'Prisma', d.spend_usd, 0)) AS prisma,
                     SUM(IF(d.media_source != 'Prisma', d.spend_usd, 0)) AS tv,
                     SUM(IF(d.channel_parent = '{config.CHANNEL_NEEDS_LABEL}', d.spend_usd, 0))
                       AS no_channel,
                     SUM(IF(d.channel_parent = '{config.CHANNEL_REMOVED}', d.spend_usd, 0))
                       AS removed,
                     STRING_AGG(DISTINCT d.business_code, ', ' ORDER BY d.business_code)
                       AS clients,
                     MIN(d.spend_date) AS first_spend, MAX(d.spend_date) AS last_spend
              FROM `{media}` d {in_scope} GROUP BY 1),
            r AS (
              SELECT imdb_id, SUM(royalty_usd) AS royalty_all,
                     SUM(IF(revenue_window = 'after_est', royalty_usd, 0)) AS after_est
              FROM `{_core(config.REVENUE_CORE_TABLE)}` GROUP BY 1),
            p AS (SELECT imdb_id, SUM(royalty_usd) AS premium
                  FROM `{_core(config.REVENUE_PREMIUM_VIEW)}` GROUP BY 1)
            SELECT t.imdb_id, t.title_name, t.release_date, t.distributor, t.in_mmm,
                   t.pvod_date, t.est_date, t.nbc_genre_1, m.* EXCEPT (imdb_id),
                   r.* EXCEPT (imdb_id), p.premium
            FROM `{title}` t LEFT JOIN m USING (imdb_id) LEFT JOIN r USING (imdb_id)
            LEFT JOIN p USING (imdb_id)
            WHERE t.release_date >= DATE '{first}'
              AND (m.imdb_id IS NOT NULL OR r.imdb_id IS NOT NULL)"""),
        "channels": _df(client, f"""
            SELECT d.channel_parent, d.media_source, SUM(d.spend_usd) AS spend
            FROM `{media}` d {in_scope} GROUP BY 1, 2"""),
        "monthly": _df(client, f"""
            SELECT DATE_TRUNC(d.spend_date, MONTH) AS month, d.media_source,
                   SUM(d.spend_usd) AS spend
            FROM `{media}` d {in_scope} GROUP BY 1, 2 ORDER BY 1"""),
        "monthly_revenue": _df(client, f"""
            SELECT DATE_TRUNC(p.revenue_date, MONTH) AS month, SUM(p.royalty_usd) AS royalty
            FROM `{_core(config.REVENUE_PREMIUM_VIEW)}` p JOIN `{title}` t USING (imdb_id)
            WHERE t.release_date >= DATE '{first}' GROUP BY 1 ORDER BY 1"""),
        "retailers": _df(client, f"""
            SELECT r.retailer, SUM(r.royalty_usd) AS royalty
            FROM `{_core(config.REVENUE_CORE_TABLE)}` r JOIN `{title}` t USING (imdb_id)
            WHERE r.revenue_window IN ('before_pvod', 'premium')
              AND t.release_date >= DATE '{first}'
            GROUP BY 1 ORDER BY 2 DESC"""),
        "scheduled": _df(client, f"""
            SELECT SUM(d.spend_usd) AS spend FROM `{media}` d JOIN `{title}` t USING (imdb_id)
            WHERE d.link = 'film' AND d.spend_status = 'scheduled'
              AND t.release_date >= DATE '{first}'"""),
    }


# =============================================================================
# Headline numbers
# =============================================================================

def _m(x) -> str:
    x = float(x or 0)
    sign, x = ("−" if x < 0 else ""), abs(x)
    return f"{sign}${x / 1e6:,.1f}M" if x >= 1e6 else f"{sign}${x:,.0f}"


def headline(data: dict) -> dict:
    f, t, d = data["films"], data["titles"].iloc[0], data["dates"].iloc[0]
    media = float(f[["prisma", "tv"]].sum().sum())
    no_channel, removed = float(f["no_channel"].sum()), float(f["removed"].sum())
    return {
        "media_snapshot": d["media_snapshot"], "revenue_delivery": d["revenue_delivery"],
        "films_all": int(t["films"]), "films_era": int(t["mmm_era"]),
        "in_mmm_yes": int(t["in_mmm_yes"]), "in_mmm_no": int(t["in_mmm_no"]),
        "in_mmm_unset": int(t["in_mmm_unset"]),
        "films_with_media": int((f[["prisma", "tv"]].fillna(0).sum(axis=1) != 0).sum()),
        "films_with_premium": int((f["premium"].fillna(0) != 0).sum()),
        "media": media, "prisma": float(f["prisma"].sum()), "tv": float(f["tv"].sum()),
        "scheduled": float(data["scheduled"]["spend"].fillna(0).sum()),
        "with_channel": media - no_channel - removed, "no_channel": no_channel,
        "removed": removed, "premium": float(f["premium"].sum()),
        "royalty_all": float(f["royalty_all"].sum()),
    }


def headline_markdown(h: dict):
    from IPython.display import Markdown

    year = config.MMM_FIRST_RELEASE_DATE[:4]
    share = h["with_channel"] / h["media"] if h["media"] else 0
    rows = [
        ("Titles in the title table", f"{h['films_all']:,}"),
        (f"Titles released {year} or later (the MMM's titles)",
         f"{h['films_era']:,}  (In MMM: yes {h['in_mmm_yes']:,} · no {h['in_mmm_no']:,} · "
         f"not reviewed {h['in_mmm_unset']:,})"),
        ("… with media spend / with premium revenue",
         f"{h['films_with_media']:,} / {h['films_with_premium']:,}"),
        ("Media spend that has run", f"**{_m(h['media'])}**  (Prisma {_m(h['prisma'])} · "
                                     f"TV, audio and MVPD {_m(h['tv'])})"),
        ("… booked but not run yet (scheduled)", _m(h["scheduled"])),
        ("… with a channel label", f"{_m(h['with_channel'])}  ({share:.0%})"),
        ("… still waiting for a channel (07_Review_Channels)", _m(h["no_channel"])),
        ("… removed in the channel review (not media we model)", _m(h["removed"])),
        ("Premium revenue used by the MMM (PVOD to EST − 1, US royalty)",
         f"**{_m(h['premium'])}**"),
        ("All US royalty linked to these titles", _m(h["royalty_all"])),
    ]
    table = "\n".join(f"| {a} | {b} |" for a, b in rows)
    return Markdown(f"Media snapshot **{h['media_snapshot']}** · revenue delivery "
                    f"**{h['revenue_delivery']}**\n\n| | |\n|---|---|\n{table}")


# =============================================================================
# Charts (matplotlib; one idea per chart)
# =============================================================================

def _style(ax, title: str, xlabel: str | None = None, ylabel: str | None = None):
    import matplotlib.ticker as mt

    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12, fontweight="bold")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK_2, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    return mt


def _figure(size):
    """A figure that is not tied to pyplot, so the notebook shows each chart exactly once."""
    from matplotlib.figure import Figure

    fig = Figure(figsize=size)
    return fig, fig.subplots()


def _money_axis(axis):
    import matplotlib.ticker as mt

    axis.set_major_formatter(mt.FuncFormatter(lambda v, _p: _m(v)))


def chart_channels(data: dict):
    """Media spend by parent channel (spend that has run), largest first."""
    c = data["channels"]
    c = c[c["channel_parent"] != config.CHANNEL_REMOVED]
    s = c.groupby("channel_parent")["spend"].sum().sort_values()
    fig, ax = _figure((9, 0.38 * len(s) + 1.4))
    colors = [MUTED if k == config.CHANNEL_NEEDS_LABEL else SERIES[0] for k in s.index]
    ax.barh([_channel_name(k) for k in s.index], s.values, color=colors, height=0.7)
    for y, v in enumerate(s.values):
        ax.text(v, y, f"  {_m(v)}", va="center", fontsize=8, color=INK_2)
    _style(ax, "Media spend by channel (titles 2023+, spend that has run)")
    _money_axis(ax.xaxis)
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.15)
    fig.tight_layout()
    return Chart(fig)


def chart_monthly(data: dict):
    """
    Total media spend and premium revenue by month, from the MMM's first year.
    Interactive (plotly): hover a month to see both amounts.
    """
    import plotly.graph_objects as go

    start = pd.Timestamp(config.MMM_FIRST_RELEASE_DATE)
    m = data["monthly"].copy()
    m["month"] = pd.to_datetime(m["month"])
    media = m.groupby("month")["spend"].sum()
    r = data["monthly_revenue"].copy()
    r["month"] = pd.to_datetime(r["month"])
    revenue = r.groupby("month")["royalty"].sum()
    media, revenue = media[media.index >= start], revenue[revenue.index >= start]
    fig = go.Figure()
    for i, (name, series) in enumerate((("Media spend that has run", media),
                                        ("Premium revenue", revenue))):
        fig.add_trace(go.Scatter(
            x=series.index, y=series.values, mode="lines+markers",
            name=f"{name} ({_m(series.sum())} in total)",
            line={"color": SERIES[i], "width": 2}, marker={"size": 5, "color": SERIES[i]},
            customdata=[_m(v) for v in series.values],
            hovertemplate=f"{name}: %{{customdata}}<extra></extra>"))
    axis = {"gridcolor": GRID, "linecolor": AXIS, "zeroline": False,
            "tickfont": {"color": INK_2, "size": 11}}
    fig.update_layout(
        title={"text": f"<b>Media spend and premium revenue by month (titles {start.year}+)</b>",
               "x": 0.01, "font": {"color": INK, "size": 15}},
        xaxis={**axis, "hoverformat": "%B %Y"},
        yaxis={**axis, "tickprefix": "$", "tickformat": "~s", "rangemode": "tozero"},
        hovermode="x unified",
        legend={"orientation": "h", "x": 0, "y": 1.08, "font": {"color": INK_2, "size": 11}},
        plot_bgcolor=SURFACE, paper_bgcolor=SURFACE, width=1000, height=450,
        margin={"l": 70, "r": 20, "t": 90, "b": 50},
        hoverlabel={"bgcolor": "white", "font": {"color": INK, "size": 12}})
    return fig


def chart_spend_vs_revenue(data: dict):
    """
    One dot per title: media spend against premium revenue (log scales), coloured by
    distributor. Interactive (plotly): hover a dot for the title's details; click a
    distributor in the legend to hide or show it.
    """
    import plotly.graph_objects as go

    f = data["films"].copy()
    f["media"] = f[["prisma", "tv"]].fillna(0).sum(axis=1)
    f = f[(f["media"] > 0) & (f["premium"].fillna(0) > 0)].copy()
    f["distributor"] = f["distributor"].fillna("(no distributor)")
    # Colours follow the distributor, most titles first; past 7 distributors the rest share grey.
    order = f["distributor"].value_counts().index.tolist()
    keep = order if len(order) <= len(SERIES) else order[:len(SERIES) - 1]
    f["series"] = f["distributor"].where(f["distributor"].isin(keep), "Other")
    f["in_mmm_text"] = f["in_mmm"].map({True: "yes", False: "no"}).fillna("not reviewed")
    f["release_text"] = pd.to_datetime(f["release_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    hover = pd.DataFrame({
        "title": f["title_name"].astype(str).str.title(),
        "distributor": f["distributor"].astype(str).str.title(),
        "release": f["release_text"].fillna(""),
        "in_mmm": f["in_mmm_text"],
        "media": f["media"].map(_m), "prisma": f["prisma"].map(_m), "tv": f["tv"].map(_m),
        "no_channel": f["no_channel"].map(_m), "premium": f["premium"].map(_m),
        "per_dollar": (f["premium"] / f["media"]).map(lambda v: f"${v:,.2f}"),
    }, index=f.index)
    template = ("<b>%{customdata[0]}</b><br>%{customdata[1]} · released %{customdata[2]}"
                "<br>In MMM: %{customdata[3]}<br><br>Media spend: %{customdata[4]}"
                "<br>  Prisma %{customdata[5]} · TV / audio / MVPD %{customdata[6]}"
                "<br>  No channel yet: %{customdata[7]}<br>Premium revenue: %{customdata[8]}"
                "<br>Premium revenue per $1 media: %{customdata[9]}<extra></extra>")
    fig = go.Figure()
    for i, name in enumerate(keep + (["Other"] if len(keep) < len(order) else [])):
        g = f[f["series"] == name]
        fig.add_trace(go.Scatter(
            x=g["media"], y=g["premium"], mode="markers", name=f"{str(name).title()} ({len(g)})",
            customdata=hover.loc[g.index].values, hovertemplate=template,
            marker={"size": 11, "color": SCATTER_COLORS[i] if name != "Other" else MUTED,
                    "symbol": SYMBOLS[i] if name != "Other" else "circle-open",
                    "line": {"width": 1, "color": SURFACE},
                    "opacity": 0.95}))
    axis = {"type": "log", "gridcolor": GRID, "linecolor": AXIS, "zeroline": False,
            "tickprefix": "$", "tickformat": "~s", "tickfont": {"color": INK_2, "size": 11},
            "title": {"font": {"color": INK_2, "size": 12}}}
    fig.update_layout(
        title={"text": "<b>Media spend vs premium revenue, one dot per title (2023+)</b>",
               "x": 0.01, "font": {"color": INK, "size": 15}},
        xaxis={**axis, "title": {**axis["title"], "text": "Media spend that has run (log scale)"}},
        yaxis={**axis, "title": {**axis["title"], "text": "Premium revenue (log scale)"}},
        legend={"title": {"text": "Distributor (titles)<br><sup>click to hide or show</sup>"},
                "font": {"color": INK_2, "size": 11}},
        plot_bgcolor=SURFACE, paper_bgcolor=SURFACE, width=1000, height=600,
        margin={"l": 70, "r": 20, "t": 60, "b": 60},
        hoverlabel={"bgcolor": "white", "font": {"color": INK, "size": 12}})
    return fig


def chart_channel_coverage(data: dict):
    """Top titles by spend: how much of each title's spend already has a channel."""
    f = data["films"].copy()
    f["media"] = f[["prisma", "tv"]].fillna(0).sum(axis=1)
    f["labelled"] = f["media"] - f["no_channel"].fillna(0) - f["removed"].fillna(0)
    f = f.nlargest(TOP_FILMS, "media").iloc[::-1]
    fig, ax = _figure((9, 0.32 * len(f) + 1.4))
    names = [str(n).title() for n in f["title_name"]]
    ax.barh(names, f["labelled"], color=SERIES[0], height=0.7, label="With a channel",
            edgecolor=SURFACE, linewidth=1)
    ax.barh(names, f["no_channel"].fillna(0), left=f["labelled"], color=MUTED, height=0.7,
            label="No channel yet", edgecolor=SURFACE, linewidth=1)
    _style(ax, f"Channel labels on the {TOP_FILMS} titles with the most media spend")
    _money_axis(ax.xaxis)
    ax.grid(axis="y", visible=False)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout()
    return Chart(fig)


def chart_retailers(data: dict):
    """Premium revenue by retailer (top 12, the rest as Other)."""
    r = data["retailers"].copy()
    top = r.head(12)
    rest = r["royalty"].iloc[12:].sum()
    if rest:
        top = pd.concat([top, pd.DataFrame([{"retailer": "Other", "royalty": rest}])])
    top = top.iloc[::-1]
    fig, ax = _figure((9, 0.38 * len(top) + 1.4))
    ax.barh(top["retailer"], top["royalty"], color=SERIES[0], height=0.7)
    for y, v in enumerate(top["royalty"]):
        ax.text(v, y, f"  {_m(v)}", va="center", fontsize=8, color=INK_2)
    _style(ax, "Premium revenue by retailer (titles 2023+, PVOD to EST − 1)")
    _money_axis(ax.xaxis)
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.15)
    fig.tight_layout()
    return Chart(fig)


# =============================================================================
# Per-title table
# =============================================================================

MONEY_COLUMNS = ["Prisma spend", "TV / audio / MVPD spend", "Media spend", "No channel yet",
                 "Premium revenue", "Revenue after EST"]


def film_frame(data: dict) -> pd.DataFrame:
    """One row per title (the Step 8 table), plain values, sorted by release date."""
    f = data["films"].copy()
    f["media"] = f[["prisma", "tv"]].fillna(0).sum(axis=1)
    f["in_mmm"] = f["in_mmm"].map({True: "yes", False: "no"}).fillna("not reviewed")
    f["clients"] = f["clients"].fillna("")                 # client codes, e.g. "ZFF, ZUE"
    f["per_dollar"] = f.apply(lambda r: r["premium"] / r["media"]
                              if r["media"] and pd.notna(r["premium"]) else None, axis=1)
    cols = {"title_name": "Title", "imdb_id": "IMDb ID", "release_date": "Release",
            "distributor": "Distributor", "clients": "Client", "in_mmm": "In MMM",
            "pvod_date": "PVOD", "est_date": "EST", "prisma": "Prisma spend",
            "tv": "TV / audio / MVPD spend", "media": "Media spend",
            "no_channel": "No channel yet", "premium": "Premium revenue",
            "per_dollar": "Premium revenue per $1 media", "after_est": "Revenue after EST"}
    out = f.sort_values(["release_date", "title_name"])[list(cols)].rename(columns=cols)
    for c in ("Release", "PVOD", "EST"):
        out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    return out.reset_index(drop=True)


def film_table(data: dict):
    from us_hment_mmm.display import styled

    out = film_frame(data).drop(columns=["IMDb ID"])
    return styled(out, money=MONEY_COLUMNS, decimals={"Premium revenue per $1 media": 2})


def save_film_csv(data: dict, folder: str | None = None):
    """Save the Step 8 table as a dated CSV in the QA export folder and say where it is."""
    from datetime import datetime
    from pathlib import Path

    from IPython.display import Markdown

    from us_hment_mmm.reference.review import where_to_find

    root = Path(folder or config.QA_EXPORT_DIR)
    root.mkdir(parents=True, exist_ok=True)
    out = film_frame(data)
    out[MONEY_COLUMNS] = out[MONEY_COLUMNS].round(2)
    out["Premium revenue per $1 media"] = out["Premium revenue per $1 media"].round(4)
    name = f"qa-titles-{datetime.now().strftime('%Y%m%dT%H%M%S')}.csv"
    out.to_csv(root / name, index=False)
    return Markdown(f"✅ Saved **{len(out):,}** titles to `{name}`.\n\n**Where to find it:** "
                    + where_to_find(root.name, root.parent)
                    + "  \nClick the file name to download it. It opens in Excel.")
