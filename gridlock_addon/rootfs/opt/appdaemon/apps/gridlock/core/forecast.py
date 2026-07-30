"""ForecastProvider — PV curve (Solcast) and learned house-load profile.

LearnedLoadForecastProvider adds one thing the old code didn't do:
EV load separation. It already discovered both the EV-charging switch
and an EV power sensor, but never combined them — a plug-in event could
distort the learned per-half-hour house-load baseline for days after
(the EMA blend has no way to tell "the house got hungrier" from "the car
started charging"). Subtracting live EV draw from the sample before
blending it in keeps the two apart.
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
                 ev_power_entity=None, daily_house_kwh=12.0, load_hourly_weights=None):
        self.app = app
        self.path = path
        self.load_power_entity = load_power_entity
        self.ev_entity = ev_entity
        self.ev_power_entity = ev_power_entity
        self.daily_house_kwh = daily_house_kwh
        self.load_hourly_weights = load_hourly_weights
        self.profile = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.profile, f)
        except OSError:
            pass

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
        observed_slot_kwh = kw * (5 / 60) * 6  # this tick covers ~5 min of a 30-min slot
        alpha = 0.05
        prev = self.profile.get(slot_idx)
        self.profile[slot_idx] = (observed_slot_kwh if prev is None
                                   else prev * (1 - alpha) + observed_slot_kwh * alpha)
        self._save()

    def load_kwh(self, slot_start):
        slot_idx = str(slot_start.hour * 2 + (1 if slot_start.minute >= 30 else 0))
        learned = self.profile.get(slot_idx)
        if learned is not None:
            return learned
        if self.load_hourly_weights and len(self.load_hourly_weights) == 24:
            w = float(self.load_hourly_weights[slot_start.hour])
            total = sum(float(x) for x in self.load_hourly_weights)
            return self.daily_house_kwh * (w / total) / 2.0
        return self.daily_house_kwh / 48.0

    def learned_series(self):
        return [{"x": f"{i // 2:02d}:{'30' if i % 2 else '00'}", "y": round(kwh, 3)}
                for i, kwh in sorted(((int(k), v) for k, v in self.profile.items()),
                                     key=lambda p: p[0])]
