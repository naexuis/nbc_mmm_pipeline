"""Dashboard views: the rules every page relies on are in the SQL."""

from us_hment_mmm import config
from us_hment_mmm.dashboard import views as V


def test_media_rules():
    sql = V.media_daily_sql()
    assert "d.media_owner = 'home_ent'" in sql and "d.spend_status = 'actual'" in sql
    assert f"d.channel_parent != '{config.CHANNEL_REMOVED}'" in sql
    assert "WHEN 'SEARCH' THEN 'Programmatic'" in sql and "'FEE'" not in sql
    assert "model_channel IS NOT NULL" in sql                      # fees drop out
    assert f"release_date >= DATE '{config.MMM_FIRST_RELEASE_DATE}'" in sql


def test_comp_mix_leaves_unlabelled_spend_out_of_the_mix_and_warns():
    sql = V.comp_mix_sql()
    grid = sql[sql.index("UNNEST(["):sql.index("]) AS c")]
    for c in V.model_channels()[:-1]:
        assert f"'{c}'" in grid
    assert "'No channel yet'" not in grid                          # not a row in the mix
    assert V.model_channels()[-1] == "No channel yet" and "Linear" in V.model_channels()
    for col in ("title_total", "labelled_total", "no_channel_spend", "no_channel_share",
                "channel_check"):
        assert col in sql
    assert "has no channel yet" in sql and "All Home Ent media has a channel" in sql


def test_names():
    assert set(V.VIEWS) == {"title", "media_daily", "revenue_daily", "comp_mix", "recon"}
    assert V.view_name("title").endswith(".dash_title")


def test_notebook_message():
    lines = ["SAVED prj.us_hment_mmm_core.dash_title  (14 columns)", "Saved."]
    md = V.markdown(lines, dry_run=False).data
    assert "SAVED" in md and "`dash_title` 14 columns" in md


def test_recon_uses_the_qa_query_and_rules():
    from us_hment_mmm.qa import summary as Q

    sql = V.recon_sql()
    assert Q.chtt_sql() in sql                                     # one source of truth
    for status in (Q.RECON_OK, Q.RECON_CHECK, Q.RECON_OPEN, Q.RECON_NO_FINANCE, Q.RECON_NO_POS):
        assert f"'{status}'" in sql
    assert f"BETWEEN {Q.RECON_UNDER} AND {Q.RECON_OVER}" in sql
    assert "DATE '2000-01-01'" in sql                              # CHTT placeholder dates
    assert "No US premium window: ours is Canada only" in sql


def test_revenue_sources_uses_the_same_window_as_ours():
    sql = V.revenue_sources_sql()
    assert "'US'" in sql and "'Canada'" in sql and "UNION ALL" in sql
    assert "t.est_date IS NULL OR s.day < t.est_date" in sql          # up to EST - 1
    assert "s.country = 'Canada' OR t.us_premium_window IS NOT FALSE" in sql
    assert "reported_product_name" in sql and "data_source" in sql
    assert set(V.TABLES) == {"revenue_sources"}
    assert "AS std_chtt" in sql and "dash_recon" in sql
    assert "dash_recon" in V.revenue_sources_check_sql()
