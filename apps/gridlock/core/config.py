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


# Default £/kWh degradation cost per mode — the optimiser's only real
# lever against cycling the battery for wafer-thin arbitrage margins.
# eco needs a bigger price spread before it'll discharge at all (shallower,
# less frequent cycling); balanced sits in the spec's 3.5-5.0p range;
# max_profit always overrides this to 0 regardless of what's configured
# here (see optimizer.solve). Not a real degradation-vs-cycle-depth model
# — there's no solid Sigenergy SoH-vs-cycling data to build one from —
# just a reasoned, documented lever.
RISK_PROFILES = {
    Mode.ECO: 0.09,
    Mode.BALANCED: 0.05,
    Mode.MAX_PROFIT: 0.01,
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
    target_daily_net_cost: float = None  # None disables the balanced-mode cutoff

    storm_target_soc: float = 100.0

    horizon_slots: int = HORIZON_SLOTS
    slot_min: int = SLOT_MIN

    def __post_init__(self):
        if self.export_rate_kw is None:
            self.export_rate_kw = self.discharge_kw
        if self.degradation is None:
            self.degradation = RISK_PROFILES[self.mode]
