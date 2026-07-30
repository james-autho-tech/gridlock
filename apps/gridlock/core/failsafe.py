"""Deadman switch — if the HA link or the Solcast forecast has been
unreachable/stale for more than 15 minutes, drop to a safe local default
(ECO/self-consumption) rather than keep planning against data that may
no longer reflect reality. Deliberately keyed off "continuously
unavailable", not "forecast is N minutes old" — Solcast only refreshes
every 30-60 minutes in normal operation, so an age-based threshold would
false-trigger constantly.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

FAILSAFE_THRESHOLD_MINUTES = 15


class FailsafeState(str, Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class FailsafeResult:
    state: FailsafeState
    reason: str = ""


def check(now, *, ha_last_live, solcast_last_live,
          threshold_minutes=FAILSAFE_THRESHOLD_MINUTES):
    """ha_last_live / solcast_last_live: the last time each source was
    confirmed live (a non-unavailable/unknown read), or None if it has
    never been seen live. Either being None or older than
    threshold_minutes trips DEGRADED."""
    threshold = timedelta(minutes=threshold_minutes)

    if ha_last_live is None:
        return FailsafeResult(FailsafeState.DEGRADED, "HA link never confirmed live")
    if now - ha_last_live > threshold:
        return FailsafeResult(
            FailsafeState.DEGRADED,
            f"HA link stale for {(now - ha_last_live)} (> {threshold_minutes}m)")

    if solcast_last_live is None:
        return FailsafeResult(FailsafeState.DEGRADED, "Solcast link never confirmed live")
    if now - solcast_last_live > threshold:
        return FailsafeResult(
            FailsafeState.DEGRADED,
            f"Solcast link stale for {(now - solcast_last_live)} (> {threshold_minutes}m)")

    return FailsafeResult(FailsafeState.NORMAL)
