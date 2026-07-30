"""InverterAdapter — discovers + controls one battery inverter.

Only SigenergyAdapter implements control writes (mode switching, charge/
discharge limits) — that's the one real inverter this project has ever
been run against, and this whole system exists specifically to avoid
mis-stating a real inverter's control mode (bypass/EEPROM wear). GivTCP
and Solis get full read-only telemetry discovery (their entity naming is
public/stable) but raise on any control write rather than guess at
service-call semantics nobody has verified against real hardware —
see ReadOnlyInverterAdapter's docstring.
"""

from abc import ABC, abstractmethod

from . import dedup


def _float_or_none(app, entity_id):
    if not entity_id:
        return None
    try:
        v = app.get_state(entity_id)
        return float(v) if v not in (None, "unknown", "unavailable") else None
    except (ValueError, TypeError):
        return None


class InverterAdapter(ABC):
    supports_control = True

    @abstractmethod
    def read_state(self, app):
        """-> dict of current values, keyed the same as plan_writes'
        desired_state (e.g. {"mode": ..., "disch_kw": ..., "charge_kw": ...})."""

    @abstractmethod
    def plan_writes(self, current_state, desired_state):
        """-> list[dedup.Write] — only entities that actually need writing."""

    def execute(self, app, writes):
        for w in writes:
            app.call_service(w.service, target={"entity_id": w.entity_id}, **w.payload)


class SigenergyAdapter(InverterAdapter):
    MODE_CHARGE = "Command Charging (Grid First)"
    MODE_DISCHARGE = "Command Discharging (PV First)"
    MODE_ECO = "Maximum Self Consumption"
    MODE_BYPASS = "Unknown"  # Sigenergy's own documented pass-through state

    def __init__(self, ent_mode, ent_disch_limit, ent_charge_limit,
                 ent_discharge_cutoff=None,
                 mode_charge=None, mode_discharge=None, mode_eco=None):
        self.ent_mode = ent_mode
        self.ent_disch_limit = ent_disch_limit
        self.ent_charge_limit = ent_charge_limit
        self.ent_discharge_cutoff = ent_discharge_cutoff
        self.mode_charge = mode_charge or self.MODE_CHARGE
        self.mode_discharge = mode_discharge or self.MODE_DISCHARGE
        self.mode_eco = mode_eco or self.MODE_ECO

    def read_state(self, app):
        state = {
            "mode": app.get_state(self.ent_mode),
            "disch_kw": _float_or_none(app, self.ent_disch_limit),
            "charge_kw": _float_or_none(app, self.ent_charge_limit),
        }
        if self.ent_discharge_cutoff:
            state["discharge_cutoff"] = _float_or_none(app, self.ent_discharge_cutoff)
        return state

    def _services(self):
        services = {
            "mode": (self.ent_mode, "select/select_option", "option"),
            "disch_kw": (self.ent_disch_limit, "number/set_value", "value"),
            "charge_kw": (self.ent_charge_limit, "number/set_value", "value"),
        }
        if self.ent_discharge_cutoff:
            services["discharge_cutoff"] = (self.ent_discharge_cutoff, "number/set_value", "value")
        return services

    def plan_writes(self, current_state, desired_state):
        return dedup.plan_writes(current_state, desired_state, self._services())


class ReadOnlyInverterAdapter(InverterAdapter):
    supports_control = False
    brand = "unknown"

    def read_state(self, app):
        return {}

    def plan_writes(self, current_state, desired_state):
        raise NotImplementedError(
            f"{self.brand} control mapping not implemented — GridLock can discover "
            f"{self.brand} telemetry (SoC, capacity, power) but has no verified "
            "control service-call mapping for it yet, and won't guess at one for a "
            "real battery inverter. PRs welcome — see InverterAdapter in "
            "core/inverter.py.")


class GivTCPAdapter(ReadOnlyInverterAdapter):
    brand = "GivTCP"


class SolisAdapter(ReadOnlyInverterAdapter):
    brand = "Solis"
