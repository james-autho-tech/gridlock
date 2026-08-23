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
        # Real per-slot totals accumulated tick by tick (see sample()) —
        # in-memory only, deliberately not persisted: losing at most one
        # in-progress slot's accumulation on a restart is a rounding
        # error next to what it fixes (see sample()'s own docstring).
        self._accum_slot_idx = None
        self._accum_house_kwh = 0.0
        self._accum_circuit_kwh = {}

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
        """Each call covers ~5 minutes of real time (this provider is
        sampled on GridLock's own 5-minute tick) — one 30-minute slot is
        six calls. Real bug, confirmed against a live install: this used
        to extrapolate EACH 5-minute reading as if that power level were
        sustained for the FULL 30 minutes, then blend that extrapolated
        figure into the learned average — SIX separate times per slot,
        once per tick. A brief appliance spike (kettle, oven) during just
        one of those six ticks got recorded as "that power draw for the
        whole half hour", a ~6x overstatement for that tick, repeated
        every time something briefly spikes around the same time of day
        — a persistent, structural bias that more data doesn't correct on
        its own, since the input observations themselves are wrong, not
        just noisy. Fixed by accumulating each tick's own real 5-minute
        contribution (kw * 5/60, no extrapolation) across the whole slot,
        and blending the true slot total into the EMA once the slot
        actually completes, not on every tick within it."""
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

        if self._accum_slot_idx is not None and self._accum_slot_idx != slot_idx:
            self._blend(self.house_profile, self._accum_slot_idx, self._accum_house_kwh)
            for eid, kwh in self._accum_circuit_kwh.items():
                self._blend(self.circuit_profiles.setdefault(eid, {}), self._accum_slot_idx, kwh)
            self._accum_house_kwh = 0.0
            self._accum_circuit_kwh = {}
            self._save()
        self._accum_slot_idx = slot_idx

        for eid in self.circuit_power_entities:
            circuit_kw = _get_float(self.app, eid, 0.0) or 0.0
            kw = max(0.0, kw - circuit_kw)
            self._accum_circuit_kwh[eid] = self._accum_circuit_kwh.get(eid, 0.0) + circuit_kw * (5 / 60)
        self._accum_house_kwh += kw * (5 / 60)

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
