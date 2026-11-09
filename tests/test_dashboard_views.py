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


def test_comp_mix_has_every_channel_and_the_title_total():
    sql = V.comp_mix_sql()
    for c in V.model_channels():
        assert f"'{c}'" in sql
    assert "title_total" in sql and "CROSS JOIN UNNEST" in sql
    assert V.model_channels()[-1] == "No channel yet" and "Linear" in V.model_channels()


def test_names():
    assert set(V.VIEWS) == {"title", "media_daily", "revenue_daily", "comp_mix"}
    assert V.view_name("title").endswith(".dash_title")
