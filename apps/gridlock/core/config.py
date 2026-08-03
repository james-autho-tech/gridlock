"""SiteConfig — one typed, validated bundle of the tunable parameters a
single GridLock instance needs, built once in gridlock.py's initialize()
from apps.yaml + auto-discovery + the addon Configuration-tab overrides.
Kept as a plain dataclass (not tied to AppDaemon's self.args dict) so the
optimizer/tests can construct one directly without any HA machinery.
"""

from dataclasses import dataclass, field
from enum import Enum


class Mode(str, Enum):
    """The three operational strategies. Values match the existing
    battery_risk_profile config values so no apps.yaml key renames are
    needed — eco/balanced/max_profit now select real behavioural
    differences (export gating, degradation weighting), not just a
    degradation-cost scalar under one shared behaviour."""
    ECO = "eco"
    BALANCED = "balanced"
    MAX_PROFIT = "max_profit"

    @classmethod
    def from_str(cls, value, default="balanced"):
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls(default)


# Default £/kWh degradation cost per mode, applied to battery
# self-consumption (batt_to_load — using stored charge for your own
# load instead of importing). The optimiser's only real lever against
# cycling the battery for wafer-thin savings. Not a real
# degradation-vs-cycle-depth model — there's no solid Sigenergy
# SoH-vs-cycling data to build one from — just a reasoned, documented
# lever. max_profit's self-consumption cost is 0 deliberately (no
# hesitation to use the battery for your own load, at any spread) —
# unlike degradation on the *export* side (see EXPORT_DEGRADATION_
# OVERRIDES below), this isn't hard-forced in optimizer.py, just
# defaulted to 0 here, so an explicit override still applies if set.
#
# balanced was originally 0.05 (the spec's 3.5-5.0p range) but that
# turned out to be far too low against real Octopus Agile/IOG spreads:
# checked directly against a real day's plan (3.5p cheap import, mostly
# 10-24p export) and every single export slot in it cleared a 5p
# degradation cost — balanced was, in practice, behaving almost
# identically to max_profit (forecast comparison showed it within ~6%
# of max_profit's total, nowhere near eco's). Raised to 0.15 — against
# that same real data, only the genuinely best few slots per day (the
# 20-24p range) still clear that; the routine 10-16p "sells nearly
# every day" pattern doesn't. This is a real trade-off (less
# forecasted profit, less battery wear), chosen deliberately over
# "double it" (0.10), which checked out to barely change behaviour —
# most of the real export slots above were still well above that
# breakeven.
RISK_PROFILES = {
    Mode.ECO: 0.09,
    Mode.BALANCED: 0.15,
    Mode.MAX_PROFIT: 0.0,
}

# Separate £/kWh cost specifically for battery *export* (selling to the
# grid), applied via SiteConfig.export_degradation — deliberately not
# the same number as RISK_PROFILES above. Export used to be handled as
# a special case per mode (eco hard-blocked it outright regardless of
# price; max_profit hard-forced its degradation to 0 regardless of what
# was configured) rather than a genuine price-gated decision — both
# replaced by this, so every mode now uses the same soft mechanism, just
# at a different bar:
#   eco (0.25) — the battery can still export, but only on a genuinely
#     exceptional day; at typical Octopus Agile/IOG spreads this bar
#     sits above nearly everything, so in practice it stays close to
#     "never", just without hard-blocking the rare real outlier.
#   max_profit (0.03) — "go ham", but with a small real floor rather
#     than the old hard-0 (which sold at literally any positive margin,
#     including a fraction of a penny) — a tiny buffer against
#     pointless micro-cycling for near-zero gain, not a meaningful cap
#     on real arbitrage.
#   balanced isn't listed here — it falls through to the same value as
#     self-consumption (RISK_PROFILES[BALANCED]), unchanged from before
#     this split (already tuned above; not something this change should
#     silently re-diverge).
EXPORT_DEGRADATION_OVERRIDES = {
    Mode.ECO: 0.25,
    Mode.MAX_PROFIT: 0.03,
}

SLOT_MIN = 30
HORIZON_SLOTS = 96  # 48h — spec's explicit ask; a strict superset of the
# previous 28h horizon (see gridlock.py's own history for why 24h wasn't
# enough), so nothing about the old reasoning is lost by extending it.


@dataclass
class SiteConfig:
    """One site's tunable parameters. Field defaults mirror apps.yaml's
    existing defaults exactly — installing this refactor changes no
    behaviour for an existing config that doesn't set new keys."""
    site_id: str = "default"

    battery_kwh: float = 10.0
    daily_house_kwh: float = 12.0
    load_hourly_weights: list = None
    efficiency: float = 0.90
    floor_soc: float = 20.0
    charge_kw: float = 10.0
    discharge_kw: float = 10.0
    export_rate_kw: float = None  # None -> defaults to discharge_kw
    ev_concurrent_charge_kw: float = 5.0
    cheap_rate: float = 0.10
    min_export_pct: float = 5.0
    conserve_battery: bool = False
    default_import: float = 0.2839
    default_export: float = 0.15
    export_margin: float = 0.02

    mode: Mode = Mode.BALANCED
    degradation: float = None  # None -> RISK_PROFILES[mode]
    export_degradation: float = None  # None -> EXPORT_DEGRADATION_OVERRIDES[mode], or degradation if mode isn't in it
    target_daily_net_cost: float = None  # None disables the balanced-mode cutoff

    storm_target_soc: float = 100.0

    # Extra slack on top of the bare forecasted load when reserving
    # charge for an on-peak stretch — e.g. 0.15 reserves for 115% of the
    # predicted deficit, not exactly 100% of it. Exists specifically
    # because the plan re-solves every 5 minutes: if the learned load
    # forecast for a later slot drifts upward *after* an earlier slot
    # has already exported/discharged against the old, lower estimate,
    # that energy is already gone — a later solve can't claw it back,
    # only ration what's left. A point-estimate reserve with zero margin
    # cuts exactly to the wire against its own forecast being right;
    # real house load isn't that predictable slot to slot.
    reserve_margin_pct: float = 0.15

    horizon_slots: int = HORIZON_SLOTS
    slot_min: int = SLOT_MIN

    def __post_init__(self):
        if self.export_rate_kw is None:
            self.export_rate_kw = self.discharge_kw
        if self.degradation is None:
            self.degradation = RISK_PROFILES[self.mode]
        if self.export_degradation is None:
            self.export_degradation = EXPORT_DEGRADATION_OVERRIDES.get(self.mode, self.degradation)
