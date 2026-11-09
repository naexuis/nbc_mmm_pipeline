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
    home_ent = f"d.media_owner = '{config.HOME_ENT_OWNER}'"
    in_scope = (f"JOIN `{title}` t USING (imdb_id) WHERE d.link = 'film' AND {home_ent} "
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
        "chtt": _df(client, chtt_sql()),
        "scheduled": _df(client, f"""
            SELECT SUM(d.spend_usd) AS spend FROM `{media}` d JOIN `{title}` t USING (imdb_id)
            WHERE d.link = 'film' AND d.spend_status = 'scheduled' AND {home_ent}
              AND t.release_date >= DATE '{first}'"""),
        "theatrical": _df(client, f"""
            SELECT SUM(d.spend_usd) AS spend FROM `{media}` d JOIN `{title}` t USING (imdb_id)
            WHERE d.link = 'film' AND d.spend_status = 'actual' AND NOT {home_ent}
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
        "theatrical": float(data.get("theatrical", pd.DataFrame({"spend": []}))["spend"]
                            .fillna(0).sum()),
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
        ("Home Ent media spend that has run (ZUE, ZUV)",
         f"**{_m(h['media'])}**  (Prisma {_m(h['prisma'])} · TV, audio and MVPD {_m(h['tv'])})"),
        ("… booked but not run yet (scheduled)", _m(h["scheduled"])),
        ("… with a channel label", f"{_m(h['with_channel'])}  ({share:.0%})"),
        ("… still waiting for a channel (07_Review_Channels)", _m(h["no_channel"])),
        ("… removed in the channel review (not media we model)", _m(h["removed"])),
        ("For reference: Focus theatrical media on these titles (ZFF, ZFE), not used",
         _m(h["theatrical"])),
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
    for i, (name, series) in enumerate((("Home Ent media spend", media),
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


# =============================================================================
# Revenue vs Finance (Standard CHTT)
# =============================================================================
# Ours = US + Canada royalty from the PVOD date to the day before EST, pre-sales included (the
# same window as the MMM's premium revenue, our title dates). Finance = CHTT
# total_digital_premium_revenue (PVOD + PEST), which is US + Canada. POS royalty normally lands
# 5-21% under Finance, because POS does not hold every revenue source Finance reports.
RECON_UNDER = -0.20      # flag when ours is more than 20% under Finance
RECON_OVER = 0.05        # ... or more than 5% over
RECON_OK, RECON_CHECK, RECON_OPEN = "Within range", "Check", "Window still open"
RECON_NO_FINANCE, RECON_NO_POS = "No Finance figure", "No POS revenue"


def chtt_sql() -> str:
    first = config.MMM_FIRST_RELEASE_DATE
    title = _core(config.TITLE_TABLE)
    can = config.table_id(config.DATASET_RAW, config.REVENUE_RAW_TABLE_CAN)
    chtt = config.table_id(config.DATASET_RAW, config.REFERENCE_SOURCES["std_chtt"]["table"])
    money = "SAFE_CAST(REPLACE(REPLACE(TRIM(CAST({} AS STRING)), '$', ''), ',', '') AS FLOAT64)"
    return f"""
        WITH links AS (
          SELECT DISTINCT TRIM(alias_value) AS pid, imdb_id FROM `{_core(config.TITLE_ALIAS_TABLE)}`
          WHERE alias_type = 'revenue_product_id' AND category = 'film' AND imdb_id IS NOT NULL),
        can AS (
          SELECT TRIM(product_id1) AS pid,
                 SAFE.PARSE_DATE('%Y-%m-%d', TRIM(transaction_date)) AS day,
                 {money.format('royalty_total_usd')} AS usd
          FROM `{can}` WHERE _delivery_date = (SELECT MAX(_delivery_date) FROM `{can}`)
            AND TRIM(country_cd) = 'CAN'),
        fin_dates AS (
          SELECT imdb_id,
                 ANY_VALUE(IF(d_pvod >= '2000-01-01', d_pvod, NULL)) AS f_pvod,
                 ANY_VALUE(IF(d_est >= '2000-01-01', d_est, NULL)) AS f_est
          FROM (SELECT imdb_id, SAFE.PARSE_DATE('%Y-%m-%d', TRIM(CAST(premium_release_date AS STRING)))
                         AS d_pvod,
                       SAFE.PARSE_DATE('%Y-%m-%d', TRIM(CAST(est_release_date AS STRING))) AS d_est
                FROM `{chtt}` WHERE _delivery_date = (SELECT MAX(_delivery_date) FROM `{chtt}`))
          GROUP BY 1),
        can_film AS (
          SELECT l.imdb_id, SUM(c.usd) AS can_premium
          FROM can c JOIN links l USING (pid) JOIN `{title}` t USING (imdb_id)
          WHERE t.pvod_date IS NOT NULL AND (t.est_date IS NULL OR c.day < t.est_date)
          GROUP BY 1),
        can_fin AS (       -- the same, with Finance's EST date (ours when Finance has none)
          SELECT l.imdb_id, SUM(c.usd) AS can_fin_dates
          FROM can c JOIN links l USING (pid) JOIN `{title}` t USING (imdb_id)
          LEFT JOIN fin_dates f USING (imdb_id)
          WHERE t.pvod_date IS NOT NULL
            AND (COALESCE(f.f_est, t.est_date) IS NULL OR c.day < COALESCE(f.f_est, t.est_date))
          GROUP BY 1),
        us AS (SELECT imdb_id, SUM(royalty_usd) AS us_premium
               FROM `{_core(config.REVENUE_PREMIUM_VIEW)}` GROUP BY 1),
        us_fin AS (
          SELECT r.imdb_id, SUM(r.royalty_usd) AS us_fin_dates
          FROM `{_core(config.REVENUE_CORE_TABLE)}` r JOIN `{title}` t USING (imdb_id)
          LEFT JOIN fin_dates f USING (imdb_id)
          WHERE t.pvod_date IS NOT NULL
            AND (COALESCE(f.f_est, t.est_date) IS NULL
                 OR r.transaction_date < COALESCE(f.f_est, t.est_date))
          GROUP BY 1),
        est_source AS (    -- the newest change to our EST date: who and why
          SELECT record_key AS imdb_id,
                 ARRAY_AGG(STRUCT(reason, changed_by, changed_at)
                           ORDER BY changed_at DESC LIMIT 1)[OFFSET(0)] AS h
          FROM `{_core(config.TITLE_HISTORY_TABLE)}`
          WHERE table_name = '{config.TITLE_TABLE}' AND field = 'est_date' GROUP BY 1),
        fin AS (
          SELECT imdb_id, SUM({money.format('total_digital_premium_revenue')}) AS finance,
                 ANY_VALUE(TRIM(CAST(premium_release_date AS STRING))) AS fin_pvod,
                 ANY_VALUE(TRIM(CAST(est_release_date AS STRING))) AS fin_est
          FROM `{chtt}` WHERE _delivery_date = (SELECT MAX(_delivery_date) FROM `{chtt}`)
          GROUP BY 1),
        last_sale AS (SELECT MAX(transaction_date) AS d FROM `{_core(config.REVENUE_CORE_TABLE)}`)
        SELECT t.imdb_id, t.title_name, t.release_date, t.distributor, t.in_mmm,
               t.pvod_date, t.est_date, us.us_premium, can_film.can_premium, fin.finance,
               fin.fin_pvod, fin.fin_est, (SELECT d FROM last_sale) AS last_sale,
               us_fin.us_fin_dates, can_fin.can_fin_dates,
               es.h.reason AS est_reason, es.h.changed_by AS est_by,
               CAST(DATE(es.h.changed_at) AS STRING) AS est_on
        FROM `{title}` t LEFT JOIN us USING (imdb_id) LEFT JOIN can_film USING (imdb_id)
        LEFT JOIN fin USING (imdb_id) LEFT JOIN us_fin USING (imdb_id)
        LEFT JOIN can_fin USING (imdb_id) LEFT JOIN est_source es USING (imdb_id)
        WHERE t.release_date >= DATE '{first}' AND t.pvod_date IS NOT NULL
          AND t.pvod_date <= (SELECT d FROM last_sale)
          AND (us.imdb_id IS NOT NULL OR fin.imdb_id IS NOT NULL)"""


def recon_frame(data: dict) -> pd.DataFrame:
    """One row per title: ours (US + Canada premium royalty) against Finance, with a status."""
    f = data.get("chtt", pd.DataFrame()).copy()
    if f.empty:
        return f
    for c in ("us_premium", "can_premium", "finance", "us_fin_dates", "can_fin_dates"):
        f[c] = pd.to_numeric(f.get(c), errors="coerce").fillna(0.0) if c in f else 0.0
    f["ours"] = f["us_premium"] + f["can_premium"]
    f["gap"] = f["ours"] - f["finance"]
    f["gap_pct"] = f.apply(lambda r: r["gap"] / r["finance"] if r["finance"] else None, axis=1)
    f["ours_fin_dates"] = f["us_fin_dates"] + f["can_fin_dates"]
    f["gap_pct_fin_dates"] = f.apply(lambda r: (r["ours_fin_dates"] - r["finance"]) / r["finance"]
                                     if r["finance"] else None, axis=1)
    for c in ("est_reason", "est_by", "est_on"):
        if c not in f:
            f[c] = None
    f["est_source"] = [
        "" if pd.isna(r["est_reason"]) else
        f"{str(r['est_reason'])[:90]} ({r['est_by']}, {r['est_on']})" for _, r in f.iterrows()]
    day = lambda c: pd.to_datetime(f[c], errors="coerce")  # noqa: E731
    still_open = day("est_date").isna() | (day("est_date") > day("last_sale"))

    def status(r, open_):
        if not r["finance"]:
            return RECON_NO_FINANCE
        if not r["ours"]:
            return RECON_NO_POS
        if open_:
            return RECON_OPEN
        return RECON_OK if RECON_UNDER <= r["gap_pct"] <= RECON_OVER else RECON_CHECK
    f["status"] = [status(r, o) for (_, r), o in zip(f.iterrows(), still_open)]
    notes = []
    for _, r in f.iterrows():
        n = []
        for ours, theirs, name in (("pvod_date", "fin_pvod", "PVOD"), ("est_date", "fin_est", "EST")):
            a = pd.to_datetime(r[ours], errors="coerce")
            b = pd.to_datetime(r[theirs], errors="coerce")
            if pd.notna(b) and b.year < 2000:             # CHTT placeholder (e.g. 1899-12-30)
                b = pd.NaT
            if pd.notna(b) and (pd.isna(a) or a != b):
                n.append(f"Finance {name} {b.date()} (ours {a.date() if pd.notna(a) else 'blank'})")
        notes.append("; ".join(n))
    f["date_note"] = notes
    return f


def recon_markdown(data: dict):
    from IPython.display import Markdown

    f = recon_frame(data)
    if f.empty:
        return Markdown("ℹ️ No titles to compare yet.")
    done = f[f["status"].isin([RECON_OK, RECON_CHECK])]
    counts = f["status"].value_counts()
    ours, fin = done["ours"].sum(), done["finance"].sum()
    pct = (ours - fin) / fin if fin else 0
    rows = [
        ("Titles compared (premium window over)", f"{len(done):,}"),
        ("… ours (US + Canada royalty) / Finance (CHTT premium)", f"{_m(ours)} / {_m(fin)}  "
                                                                    f"(**{pct:+.1%}**)"),
        ("… median gap per title", f"{done['gap_pct'].median():+.1%}" if len(done) else "—"),
        (f"… within range ({RECON_UNDER:.0%} to +{RECON_OVER:.0%})", f"{counts.get(RECON_OK, 0):,}"),
        ("… ⚠️ to check (outside the range)", f"{counts.get(RECON_CHECK, 0):,}"),
        ("Premium window still open (Finance is an estimate)", f"{counts.get(RECON_OPEN, 0):,}"),
        ("No Finance figure in the CHTT", f"{counts.get(RECON_NO_FINANCE, 0):,}"),
        ("Finance figure but no POS revenue linked", f"{counts.get(RECON_NO_POS, 0):,}"),
        ("Finance PVOD or EST date differs from ours", f"{(f['date_note'] != '').sum():,}"),
        ("… if we used Finance's EST dates: within range / to check",
         f"{done['gap_pct_fin_dates'].between(RECON_UNDER, RECON_OVER).sum():,} / "
         f"{(~done['gap_pct_fin_dates'].between(RECON_UNDER, RECON_OVER)).sum():,}"),
    ]
    table = "\n".join(f"| {a} | {b} |" for a, b in rows)
    return Markdown(f"| | |\n|---|---|\n{table}")


def chart_recon(data: dict):
    """Ours vs Finance per title (log scales), with the agreed range drawn as a band."""
    import plotly.graph_objects as go

    f = recon_frame(data)
    f = f[(f["ours"] > 0) & (f["finance"] > 0)].copy()
    fig = go.Figure()
    if not f.empty:
        lo, hi = f[["ours", "finance"]].min().min() * 0.8, f[["ours", "finance"]].max().max() * 1.2
        for k, name, dash in ((1, "Same as Finance", "solid"),
                              (1 + RECON_UNDER, f"{RECON_UNDER:.0%}", "dot"),
                              (1 + RECON_OVER, f"+{RECON_OVER:.0%}", "dot")):
            fig.add_trace(go.Scatter(x=[lo, hi], y=[lo * k, hi * k], mode="lines", name=name,
                                     line={"color": AXIS, "width": 1, "dash": dash},
                                     hoverinfo="skip", showlegend=False))
    hover = ("<b>%{customdata[0]}</b><br>PVOD %{customdata[1]} · EST %{customdata[2]}"
             "<br><br>Ours: %{customdata[3]} (US %{customdata[4]} · Canada %{customdata[5]})"
             "<br>Finance: %{customdata[6]}<br>Gap: %{customdata[7]}"
             "<br>%{customdata[8]}<extra></extra>")
    styles = ((RECON_OK, SERIES[0], "circle"), (RECON_CHECK, SERIES[1], "diamond"),
              (RECON_OPEN, MUTED, "circle-open"))
    for status, color, symbol in styles:
        g = f[f["status"] == status]
        if g.empty:
            continue
        cd = [[str(r["title_name"]).title(), str(r["pvod_date"])[:10], str(r["est_date"])[:10],
               _m(r["ours"]), _m(r["us_premium"]), _m(r["can_premium"]), _m(r["finance"]),
               f"{r['gap_pct']:+.1%}", r["date_note"]] for _, r in g.iterrows()]
        fig.add_trace(go.Scatter(x=g["finance"], y=g["ours"], mode="markers",
                                 name=f"{status} ({len(g)})", customdata=cd, hovertemplate=hover,
                                 marker={"size": 10, "color": color, "symbol": symbol,
                                         "line": {"width": 1, "color": SURFACE}}))
    axis = {"type": "log", "gridcolor": GRID, "linecolor": AXIS, "zeroline": False,
            "tickprefix": "$", "tickformat": "~s", "tickfont": {"color": INK_2, "size": 11}}
    fig.update_layout(
        title={"text": "<b>Premium revenue: ours (US + Canada royalty) vs Finance (CHTT)</b>",
               "x": 0.01, "font": {"color": INK, "size": 15}},
        xaxis={**axis, "title": {"text": "Finance premium revenue (log scale)"}},
        yaxis={**axis, "title": {"text": "Ours (log scale)"}},
        legend={"font": {"color": INK_2, "size": 11}},
        plot_bgcolor=SURFACE, paper_bgcolor=SURFACE, width=1000, height=600,
        margin={"l": 70, "r": 20, "t": 60, "b": 60},
        hoverlabel={"bgcolor": "white", "font": {"color": INK, "size": 12}})
    return fig


def recon_table(data: dict):
    from us_hment_mmm.display import styled

    f = recon_frame(data)
    if f.empty:
        return recon_markdown(data)
    order = {RECON_CHECK: 0, RECON_NO_POS: 1, RECON_OK: 2, RECON_OPEN: 3, RECON_NO_FINANCE: 4}
    f["_o"] = f["status"].map(order)
    f = f.sort_values(["_o", "gap_pct"])
    cols = {"title_name": "Title", "status": "Status", "pvod_date": "PVOD", "est_date": "EST",
            "us_premium": "Ours: US", "can_premium": "Ours: Canada", "ours": "Ours: total",
            "finance": "Finance (CHTT)", "gap": "Gap", "gap_pct": "Gap %",
            "ours_fin_dates": "Ours with Finance EST", "gap_pct_fin_dates": "Gap % with Finance EST",
            "date_note": "Finance dates", "est_source": "Where our EST came from"}
    out = f[list(cols)].rename(columns=cols)
    for c in ("PVOD", "EST"):
        out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    for c in ("Gap %", "Gap % with Finance EST"):
        out[c] = out[c].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "")
    return styled(out.reset_index(drop=True),
                  money=["Ours: US", "Ours: Canada", "Ours: total", "Finance (CHTT)", "Gap",
                         "Ours with Finance EST"])
