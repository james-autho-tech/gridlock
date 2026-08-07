"""ForecastProvider — PV curve (Solcast) and learned house-load profile.

LearnedLoadForecastProvider separates out any load it can identify and
measure independently, rather than smearing it into one whole-house
average — the EMA blend on its own has no way to tell "the house got
hungrier" from "the car started charging" or "the dryer switched on".

EV: subtracted from the sample before blending (not given its own
learned forecast) — it already has a materially more accurate forecast
elsewhere (core/slots.py's dispatch-window-driven ev_slot_kwh), so
learning a second, cruder EMA for it here would just double-count it.

Labelled circuits (any HA entity tagged with the "gridlock_power" label
— see ha_support.yaml for why a label, not an apps.yaml list): also
subtracted from the sample, but — unlike EV — each one gets its OWN
learned per-half-hour profile too, since nothing else forecasts them.
load_kwh() adds each circuit's own forecast back on top of the
whole-house residual, so decomposing the total doesn't cost any
accuracy for a circuit with its own distinct pattern (e.g. a tumble
dryer that only runs some days) the way silently discarding it would.
"""

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime


def _iso(dt_str):
    if isinstance(dt_str, datetime):
        return dt_str
    return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))


def _get_float(app, entity_id, default=None):
    if not entity_id:
        return default
    try:
        v = app.get_state(entity_id)
        return float(v) if v not in (None, "unknown", "unavailable") else default
    except (ValueError, TypeError):
        return default


class ForecastProvider(ABC):
    @abstractmethod
    def pv_curve(self):
        """-> {slot_start_utc: kWh}"""


class SolcastForecastProvider(ForecastProvider):
    def __init__(self, app, entities):
        self.app = app
        self.entities = entities or []

    def pv_curve(self):
        curve = {}
        for e in self.entities:
            if not e or not self.app.entity_exists(e):
                continue
            detailed = self.app.get_state(e, attribute="detailedForecast")
            for p in (detailed if isinstance(detailed, list) else []):
                try:
                    t = _iso(p["period_start"])
                    curve[t] = float(p["pv_estimate"]) / 2.0
                except (KeyError, ValueError, TypeError):
                    continue
        return curve


class LoadForecastProvider(ABC):
    @abstractmethod
    def load_kwh(self, slot_start):
        """-> forecast kWh for the 30-min slot starting at slot_start."""

    @abstractmethod
    def sample(self, now):
        """Blend one tick's live reading into the learned profile."""


class LearnedLoadForecastProvider(LoadForecastProvider):
    def __init__(self, app, path, *, load_power_entity, ev_entity=None,
                 ev_power_entity=None, circuit_power_entities=None,
                 daily_house_kwh=12.0, load_hourly_weights=None):
        self.app = app
        self.path = path
        self.load_power_entity = load_power_entity
        self.ev_entity = ev_entity
        self.ev_power_entity = ev_power_entity
        self.circuit_power_entities = circuit_power_entities or []
        self.daily_house_kwh = daily_house_kwh
        self.load_hourly_weights = load_hourly_weights
        self.house_profile, self.circuit_profiles = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}, {}
        # Pre-circuit-tracking files are a flat {slot_idx: kwh} dict —
        # detect that shape and migrate it into "house" rather than
        # silently discarding an already-learned baseline on upgrade.
        if "house" in data or "circuits" in data:
            return data.get("house", {}), data.get("circuits", {})
        return data, {}

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump({"house": self.house_profile, "circuits": self.circuit_profiles}, f)
        except OSError:
            pass

    @staticmethod
    def _blend(profile, slot_idx, observed_slot_kwh, alpha=0.05):
        prev = profile.get(slot_idx)
        profile[slot_idx] = (observed_slot_kwh if prev is None
                              else prev * (1 - alpha) + observed_slot_kwh * alpha)

    def sample(self, now):
        if not self.load_power_entity:
            return
        kw = _get_float(self.app, self.load_power_entity, None)
        if kw is None:
            return
        ev_active = bool(self.ev_entity) and self.app.get_state(self.ev_entity) == "on"
        if ev_active and self.ev_power_entity:
            ev_kw = _get_float(self.app, self.ev_power_entity, 0.0) or 0.0
            kw = max(0.0, kw - ev_kw)
        slot_idx = str(now.hour * 2 + (1 if now.minute >= 30 else 0))
        for eid in self.circuit_power_entities:
            circuit_kw = _get_float(self.app, eid, 0.0) or 0.0
            kw = max(0.0, kw - circuit_kw)
            circuit_slot_kwh = circuit_kw * (5 / 60) * 6
            self._blend(self.circuit_profiles.setdefault(eid, {}), slot_idx, circuit_slot_kwh)
        observed_slot_kwh = kw * (5 / 60) * 6  # this tick covers ~5 min of a 30-min slot
        self._blend(self.house_profile, slot_idx, observed_slot_kwh)
        self._save()

    def load_kwh(self, slot_start):
        slot_idx = str(slot_start.hour * 2 + (1 if slot_start.minute >= 30 else 0))
        circuits_kwh = sum(profile.get(slot_idx, 0.0)
                            for profile in self.circuit_profiles.values())
        learned = self.house_profile.get(slot_idx)
        if learned is not None:
            return learned + circuits_kwh
        if self.load_hourly_weights and len(self.load_hourly_weights) == 24:
            w = float(self.load_hourly_weights[slot_start.hour])
            total = sum(float(x) for x in self.load_hourly_weights)
            return self.daily_house_kwh * (w / total) / 2.0 + circuits_kwh
        return self.daily_house_kwh / 48.0 + circuits_kwh

    def learned_series(self):
        return [{"x": f"{i // 2:02d}:{'30' if i % 2 else '00'}", "y": round(kwh, 3)}
                for i, kwh in sorted(((int(k), v) for k, v in self.house_profile.items()),
                                     key=lambda p: p[0])]
