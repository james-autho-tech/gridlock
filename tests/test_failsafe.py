from datetime import datetime, timedelta, timezone

from core import failsafe

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def test_normal_when_both_links_fresh():
    result = failsafe.check(NOW, ha_last_live=NOW - timedelta(minutes=1),
                              solcast_last_live=NOW - timedelta(minutes=5))
    assert result.state == failsafe.FailsafeState.NORMAL


def test_degraded_when_ha_link_stale_over_15_minutes():
    result = failsafe.check(NOW, ha_last_live=NOW - timedelta(minutes=16),
                              solcast_last_live=NOW - timedelta(minutes=5))
    assert result.state == failsafe.FailsafeState.DEGRADED
    assert "HA link" in result.reason


def test_degraded_when_solcast_link_stale_over_15_minutes():
    result = failsafe.check(NOW, ha_last_live=NOW - timedelta(minutes=1),
                              solcast_last_live=NOW - timedelta(minutes=20))
    assert result.state == failsafe.FailsafeState.DEGRADED
    assert "Solcast" in result.reason


def test_degraded_when_never_seen_live():
    result = failsafe.check(NOW, ha_last_live=None, solcast_last_live=None)
    assert result.state == failsafe.FailsafeState.DEGRADED


def test_exactly_at_threshold_is_still_normal():
    result = failsafe.check(NOW, ha_last_live=NOW - timedelta(minutes=15),
                              solcast_last_live=NOW - timedelta(minutes=15))
    assert result.state == failsafe.FailsafeState.NORMAL
