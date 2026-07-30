"""TariffProvider — rate/dispatch windows for the slot builder. Ported
from the old GridLock's _rate_windows/_dispatch_windows/_mpan_stem/
_find_sibling logic (BottlecapDave's Octopus Energy integration
conventions)."""

from abc import ABC, abstractmethod
from datetime import datetime


def _iso(dt_str):
    if isinstance(dt_str, datetime):
        return dt_str
    return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))


class TariffProvider(ABC):
    @abstractmethod
    def import_windows(self):
        """-> [(start, end, £/kWh), ...]"""

    @abstractmethod
    def export_windows(self):
        """-> [(start, end, £/kWh), ...]"""

    @abstractmethod
    def dispatch_windows(self):
        """-> [(start, end, kwh), ...] — off-peak EV dispatch windows."""


class OctopusTariffProvider(TariffProvider):
    def __init__(self, app, ent_rates, ent_export_rates, ent_dispatch):
        self.app = app
        self.ent_rates = ent_rates or []
        self.ent_export_rates = ent_export_rates or []
        self.ent_dispatch = ent_dispatch

    def _attr_list(self, entity, attr):
        if not entity or not self.app.entity_exists(entity):
            return []
        v = self.app.get_state(entity, attribute=attr)
        return v if isinstance(v, list) else []

    def _rate_windows(self, entities):
        wins = []
        for e in entities:
            for r in self._attr_list(e, "rates"):
                try:
                    wins.append((_iso(r["start"]), _iso(r["end"]), float(r["value_inc_vat"])))
                except (KeyError, ValueError, TypeError):
                    continue
        return wins

    def import_windows(self):
        return self._rate_windows(self.ent_rates)

    def export_windows(self):
        return self._rate_windows(self.ent_export_rates)

    def dispatch_windows(self):
        """Octopus reports charge_in_kwh as negative (energy flowing to
        the car) — normalised to positive here."""
        wins = []
        for d in self._attr_list(self.ent_dispatch, "planned_dispatches"):
            try:
                kwh = abs(float(d.get("charge_in_kwh", 0.0)))
                wins.append((_iso(d["start"]), _iso(d["end"]), kwh))
            except (KeyError, ValueError, TypeError):
                continue
        return wins

    def ev_dispatch_totals(self):
        def total(attr):
            s = 0.0
            for d in self._attr_list(self.ent_dispatch, attr):
                try:
                    s += abs(float(d.get("charge_in_kwh", 0.0)))
                except (TypeError, ValueError):
                    continue
            return round(s, 2)
        return total("planned_dispatches"), total("completed_dispatches")
