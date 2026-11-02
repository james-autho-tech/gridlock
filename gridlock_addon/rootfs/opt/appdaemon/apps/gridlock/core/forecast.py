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
from datetime import datetime, timedelta, timezone


def _iso(dt_str):
    if isinstance(dt_str, datetime):
        return dt_str
    return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))


def _get_kw(app, entity_id, default=None):
    """A power reading in kW whatever unit the sensor reports. Most
    inverters report kW, but Shelly relays, the Hypervolt charger and
    GridLock's own heat pump sensor report W -- read as kW, a 3 W light
    idling became 3 kW of "load", and a house that uses ~30 kWh a day was
    forecast at ~100."""
    v = _get_float(app, entity_id, default)
    if v is None or v == default:
        return v
    try:
        unit = (app.get_state(entity_id, attribute="unit_of_measurement") or "kW")
    except Exception:  # noqa: BLE001 — assume kW if the attribute can't be read
        unit = "kW"
    unit = str(unit).strip().lower()
    if unit == "w":
        return v / 1000.0
    if unit == "mw":
        return v * 1000.0
    return v


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
    """Solcast's curve, scaled by how this array has actually performed
    against it.

    Solcast's median can sit well above a real roof's output for weeks
    (winter shading at low sun, a mis-entered site); one install
    generated ~65% of forecast across eight straight days, and planning
    the overnight top-up on the raw figure left the battery flat by the
    evening peak. sample() compares live PV against the forecast Solcast
    gave at the START of each day (what the overnight plan charged
    against), slot by slot over the same minutes only, so restarts or
    gaps can't skew the ratio. Learned per UTC hour (shading depends on
    sun position), shrunk toward the overall ratio where an hour has
    little data. Capped at 1.0: a good run never inflates the forecast
    past Solcast's own figure. Under-forecasting costs a little cheap
    import, over-forecasting costs peak import."""

    WINDOW_DAYS = 14
    MIN_FORECAST_KWH = 15.0   # overall forecast energy before any correction
    HOUR_PRIOR_KWH = 2.0      # shrinkage weight toward the overall ratio
    FACTOR_MIN, FACTOR_MAX = 0.3, 1.0
    MAX_SAMPLE_GAP_H = 10 / 60

    def __init__(self, app, entities, path=None):
        self.app = app
        self.entities = entities or []
        self.path = path
        self.stats, self.snapshot = self._load()
        self._last_sample = None
        self._factors = self._compute_factors()

    def _load(self):
        if not self.path:
            return {}, {}
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data.get("stats", {}), data.get("snapshot", {})
        except (OSError, ValueError):
            return {}, {}

    def _save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w") as f:
                json.dump({"stats": self.stats, "snapshot": self.snapshot}, f)
        except OSError:
            pass

    def _compute_factors(self):
        fc_tot = sum(h[0] for d in self.stats.values() for h in d.values())
        act_tot = sum(h[1] for d in self.stats.values() for h in d.values())
        if fc_tot < self.MIN_FORECAST_KWH:
            return {"overall": 1.0, "hours": {}, "forecast_kwh": fc_tot, "actual_kwh": act_tot}
        clamp = lambda x: max(self.FACTOR_MIN, min(self.FACTOR_MAX, x))  # noqa: E731
        overall = act_tot / fc_tot
        hours = {}
        for hr in range(24):
            k = str(hr)
            fc_h = sum(d.get(k, (0, 0))[0] for d in self.stats.values())
            act_h = sum(d.get(k, (0, 0))[1] for d in self.stats.values())
            if fc_h > 0:
                hours[k] = round(clamp((act_h + self.HOUR_PRIOR_KWH * overall)
                                       / (fc_h + self.HOUR_PRIOR_KWH)), 3)
        return {"overall": round(clamp(overall), 3), "hours": hours,
                "forecast_kwh": round(fc_tot, 1), "actual_kwh": round(act_tot, 1)}

    def accuracy(self):
        """{overall, hours{utc_hour: factor}, forecast_kwh, actual_kwh}."""
        return self._factors

    def factor_for(self, t):
        hr = str(t.astimezone(timezone.utc).hour)
        return self._factors["hours"].get(hr, self._factors["overall"])

    def sample(self, now, pv_kw):
        """One tick of live PV (kW) against the day-start forecast."""
        now_utc = now.astimezone(timezone.utc)
        day = now_utc.date().isoformat()
        if self.snapshot.get("day") != day:
            raw = self.raw_curve()
            self.snapshot = {"day": day, "slots": {
                t.astimezone(timezone.utc).isoformat(): kwh
                for t, kwh in raw.items() if t.astimezone(timezone.utc).date() == now_utc.date()}}
            cutoff = (now_utc - timedelta(days=self.WINDOW_DAYS)).date().isoformat()
            self.stats = {d: v for d, v in self.stats.items() if d > cutoff}
            self._factors = self._compute_factors()
            self._save()
        last, self._last_sample = self._last_sample, now_utc
        if pv_kw is None or last is None:
            return
        dt_h = (now_utc - last).total_seconds() / 3600.0
        if dt_h <= 0 or dt_h > self.MAX_SAMPLE_GAP_H:
            return
        slot = now_utc.replace(minute=(now_utc.minute // 30) * 30, second=0, microsecond=0)
        fc_slot = self.snapshot.get("slots", {}).get(slot.isoformat())
        if fc_slot is None:
            return
        fc_kwh = fc_slot / 0.5 * dt_h
        act_kwh = max(0.0, pv_kw) * dt_h
        if fc_kwh <= 0 and act_kwh <= 0:
            return
        hour = self.stats.setdefault(day, {}).setdefault(str(now_utc.hour), [0.0, 0.0])
        hour[0] += fc_kwh
        hour[1] += act_kwh
        self._save()

    def pv_curve(self):
        return {t: kwh * self.factor_for(t) for t, kwh in self.raw_curve().items()}

    def raw_curve(self):
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
        # A circuit whose future is PLANNED rather than learned -- the
        # heat pump, whose electricity GridWarm schedules slot by slot.
        # {entity_id: {slot_key: kwh}} where slot_key is slot_key();
        # for a slot with no planned figure the learned profile applies.
        self.planned_circuit_kwh = {}
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

    # 0.05 (~20-day time constant) was too slow to unlearn a genuine
    # regime change — confirmed real: a heat pump isn't split out as its
    # own labelled circuit on this install, so a run of cold days baked
    # its draw straight into house_profile for those evening slots, and
    # the plan kept forecasting that same inflated load for weeks after
    # a warm spell arrived with the heat pump plainly off (reported as
    # "5kWh in an hour" that isn't physically possible without both a
    # hot tub AND heating running at once). 0.15 (~6-7 days) still
    # smooths out one genuinely one-off day (a single cold snap, guests
    # over) without taking three weeks to catch up to an actual weather
    # swing — a heat pump circuit getting its own "gridlock_power" label
    # would let it adapt same-day instead of averaging at all, but that
    # needs a monitored circuit entity this install doesn't have.
    _BLEND_ALPHA = 0.15

    @staticmethod
    def _blend(profile, slot_idx, observed_slot_kwh, alpha=None):
        if alpha is None:
            alpha = LearnedLoadForecastProvider._BLEND_ALPHA
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
        kw = _get_kw(self.app, self.load_power_entity, None)
        if kw is None:
            return
        # Gating this on self.ev_entity's state used to require it to
        # read exactly "on" — real bug, confirmed live: a Hypervolt
        # charger's discovered "is charging" entity is a switch that
        # reflects whether charging is *enabled* (schedule armed), not
        # whether current is actually flowing, and other chargers expose
        # charging status as a string sensor ("Charging"/"Paused"/
        # "Complete") that never equals the literal string "on" at all —
        # either way this gate silently stayed false while the car
        # genuinely drew power, and that power stayed wrongly lumped
        # into house load with no way to tell from the outside. The
        # power reading itself is the actually-reliable signal: real
        # current flowing is definitionally what needs subtracting,
        # whatever any separate status entity claims — a small floor
        # (50W) just filters out standby/phantom draw a charger reports
        # even when idle, not genuine charging at any real rate.
        if self.ev_power_entity:
            ev_kw = _get_kw(self.app, self.ev_power_entity, 0.0) or 0.0
            if ev_kw > 0.05:
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
            circuit_kw = _get_kw(self.app, eid, 0.0) or 0.0
            kw = max(0.0, kw - circuit_kw)
            self._accum_circuit_kwh[eid] = self._accum_circuit_kwh.get(eid, 0.0) + circuit_kw * (5 / 60)
        self._accum_house_kwh += kw * (5 / 60)

    @staticmethod
    def slot_key(dt):
        """Timezone-proof identity of a half-hour slot: epoch half-hours."""
        return int(dt.timestamp()) // 1800

    def _circuit_kwh(self, eid, slot_idx, slot_start):
        planned = self.planned_circuit_kwh.get(eid)
        if planned:
            v = planned.get(self.slot_key(slot_start))
            if v is not None:
                return float(v)
        return self.circuit_profiles.get(eid, {}).get(slot_idx, 0.0)

    def load_kwh(self, slot_start):
        slot_idx = str(slot_start.hour * 2 + (1 if slot_start.minute >= 30 else 0))
        # Only currently-tracked circuits, not every entity ever persisted
        # in circuit_profiles — an entity unlabelled/removed from
        # circuit_power_entities (e.g. re-tagged, hardware swapped, or
        # mistakenly labelled in the first place, confirmed live: an EV
        # power sensor also carrying the gridlock_power label learned its
        # own charging pattern as a "circuit" on top of the ev_entity
        # subtraction already excluding it from house load — a real,
        # persistent double-count) would otherwise keep contributing its
        # last-learned figure to every future forecast forever, since
        # nothing else ever prunes this dict.
        circuits_kwh = sum(self._circuit_kwh(eid, slot_idx, slot_start)
                            for eid in self.circuit_power_entities)
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
