"""HASensorRegistry — entity auto-discovery by naming convention, so a
site's apps.yaml only needs to name the handful of things discovery
can't guess (or override where it guesses wrong). Ported near-verbatim
from the entity-discovery methods that used to live directly on the
hass.Hass subclass, generalised so brand-specific lookups take a list of
inverter naming prefixes (sigen_/givtcp_/solis_) instead of being
hard-coded to Sigenergy, plus two genuinely new auto-reads that weren't
done before: nominal battery capacity, and a hardware-declared max
charge/discharge rate clamp.

Takes any object with get_state()/entity_exists()/log() — normally the
hass.Hass app itself — so this stays testable with a small stub instead
of a real AppDaemon runtime.
"""

DEFAULT_INVERTER_PREFIXES = ("sigen", "givtcp", "solis")


class HASensorRegistry:
    def __init__(self, app, inverter_prefixes=DEFAULT_INVERTER_PREFIXES):
        self.app = app
        self.prefixes = tuple(inverter_prefixes)

    # -- plumbing -----------------------------------------------------
    def _flat(self):
        try:
            states = self.app.get_state() or {}
        except Exception:  # noqa: BLE001 — discovery is best-effort
            return {}
        flat = {}
        for key, value in states.items():
            if isinstance(value, dict) and "state" not in value:
                flat.update(value)  # {domain: {entity_id: {...}}}
            else:
                flat[key] = value  # already flat {entity_id: {...}}
        return flat

    @staticmethod
    def _is_live(state_obj):
        state = str((state_obj or {}).get("state", "")).lower()
        return state not in ("", "unavailable", "unknown")

    def _is_brand(self, eid):
        return any(p in eid for p in self.prefixes)

    def _warn(self, msg):
        try:
            self.app.log(msg, level="WARNING")
        except Exception:  # noqa: BLE001
            pass

    # -- generic lookup -------------------------------------------------
    def find(self, *, domain=None, prefix=None, suffix=None, contains=None,
              avoid=None, brand_only=False):
        """Single entity_id by prefix/suffix/substring — the common case
        (e.g. Octopus's account/MPAN-in-entity_id naming). Prefers a
        live match over a dead/restored duplicate."""
        flat = self._flat()
        matches = [eid for eid in flat
                   if (not domain or eid.startswith(f"{domain}."))
                   and (not prefix or eid.startswith(prefix))
                   and (not suffix or eid.endswith(suffix))
                   and (not contains or contains in eid)
                   and (not avoid or avoid not in eid)
                   and (not brand_only or self._is_brand(eid))]
        if not matches:
            return None
        live = [eid for eid in matches if self._is_live(flat.get(eid))]
        pool = live or matches
        if len(pool) > 1:
            self._warn(f"Multiple entities match (domain={domain!r} prefix={prefix!r} "
                       f"suffix={suffix!r} contains={contains!r}): {pool} — set it "
                       "explicitly to disambiguate.")
        return pool[0]

    def find_sibling(self, stem, domain, suffixes):
        """Sibling entity sharing `stem` (same account/MPAN) in `domain`,
        trying each suffix in turn — Octopus's naming has changed
        between integration versions, so this searches rather than
        blindly constructing one exact name."""
        if not stem:
            return None
        flat = self._flat()
        matches = [eid for eid in flat
                   if eid.startswith(f"{domain}.") and stem in eid
                   and any(eid.endswith(suf) for suf in suffixes)]
        if not matches:
            return None
        live = [eid for eid in matches if self._is_live(flat.get(eid))]
        pool = live or matches
        if len(pool) > 1:
            self._warn(f"Multiple sibling entities for stem={stem!r} "
                       f"suffixes={suffixes}: {pool} — set it explicitly if "
                       "the wrong one gets picked.")
        return pool[0]

    @staticmethod
    def mpan_stem(base_entity, suffix):
        if not base_entity or not base_entity.endswith(suffix):
            return None
        return base_entity.split(".", 1)[1][: -len(suffix)]

    # -- EV charger -----------------------------------------------------
    def find_hypervolt_charging(self):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("switch.") and "hypervolt" in eid]
        charging = [eid for eid in candidates if "charging" in eid]
        pool = charging or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        if not pool:
            return None
        if len(pool) > 1:
            self._warn(f"Multiple Hypervolt charging switches found {pool} — set "
                       "ev_charging explicitly in apps.yaml.")
            return pool[0]
        if not charging:
            self._warn(f"No 'charging' switch found among Hypervolt entities; using "
                       f"{pool[0]} as a best guess for ev_charging — verify this is correct.")
        return pool[0]

    def find_hypervolt_ev_power(self):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and "hypervolt" in eid
                      and "power" in eid and "ev" in eid]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        if len(pool) > 1:
            self._warn(f"Multiple candidate EV power entities found {pool} — set "
                       "ev_power_entity explicitly in apps.yaml.")
        return pool[0] if pool else None

    # -- power circuits (Shelly relays etc.) -----------------------------
    def find_shelly_power_entities(self):
        """Every Shelly relay's own live power sensor, by naming
        convention alone — Shelly's various generations/firmwares name
        theirs differently ("*_power", "*_switch_0_power", etc.), but
        all end in "_power" (as opposed to the paired cumulative
        "*_energy" sensor, or the binary_sensor.* diagnostic entities
        like Overcurrent/Overheating). A fallback ALONGSIDE (not
        instead of) the label-based discovery in ha_support.yaml — that
        one works for any brand of power sensor, this one works for
        Shelly specifically with zero setup, same "brand-specific
        naming shortcut alongside a generic mechanism" shape as
        find_hypervolt_ev_power() next to the generic find_power()."""
        flat = self._flat()
        return [eid for eid in flat
                if eid.startswith("sensor.") and "shelly" in eid.lower()
                and eid.endswith("_power")]

    # -- house load -------------------------------------------------------
    def find_load_entity(self):
        """A house-load power sensor, brand-agnostic across inverter_prefixes
        — prefers an instantaneous power reading over a cumulative energy
        total when both kinds show up."""
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and self._is_brand(eid)
                      and ("load" in eid or "consumption" in eid)]
        power_candidates = [eid for eid in candidates if "power" in eid]
        pool_source = power_candidates or candidates
        live = [eid for eid in pool_source if self._is_live(flat.get(eid))]
        pool = live or pool_source
        if not pool:
            return None
        if len(pool) > 1:
            self._warn(f"Multiple candidate load-power entities found {pool} — set "
                       "load_power_entity explicitly in apps.yaml.")
        return pool[0]

    # -- inverter telemetry (brand-agnostic) --------------------------------
    def find_power(self, keyword):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and self._is_brand(eid)
                      and "power" in eid and keyword in eid]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        return pool[0] if pool else None

    def find_pv_power(self):
        """PV power entities to sum — an aggregate (plant/total) is
        preferred over per-string sensors where both exist, to avoid
        double-counting; falls back to summing per-string ones."""
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and self._is_brand(eid)
                      and "pv" in eid and "power" in eid]
        import re
        aggregates = [eid for eid in candidates if not re.search(r"pv\d_power", eid)]
        if not aggregates:
            return candidates
        plant = [eid for eid in aggregates if "plant" in eid or "total" in eid]
        pool = plant or aggregates
        if len(pool) > 1:
            self._warn(f"Multiple aggregate PV power entities found {pool} — using "
                       f"{pool[0]}; set pv_power_entities explicitly if wrong.")
        return [pool[0]]

    def find_temp(self, keyword, exclude=None):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and self._is_brand(eid)
                      and "temperature" in eid and keyword in eid
                      and not any(x in eid for x in (exclude or []))]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        return pool[0] if pool else None

    def find_soh(self):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and self._is_brand(eid)
                      and "state_of_health" in eid]
        plant = [eid for eid in candidates if "plant" in eid]
        pool = plant or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        return pool[0] if pool else None

    def find_discharge_cutoff(self):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("number.") and self._is_brand(eid)
                      and "discharge" in eid and ("cut_off" in eid or "cutoff" in eid)]
        plant = [eid for eid in candidates if "plant" in eid]
        pool = plant or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        return pool[0] if pool else None

    def find_binary(self, keyword):
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("binary_sensor.") and self._is_brand(eid)
                      and keyword in eid]
        live = [eid for eid in candidates if self._is_live(flat.get(eid))]
        pool = live or candidates
        return pool[0] if pool else None

    # -- new: capacity + hardware-rate auto-read ----------------------------
    def find_capacity_kwh(self):
        """Nominal battery capacity — a brand-named capacity/energy-storage
        sensor first, else a generic device_class fallback, else None (the
        caller keeps its own apps.yaml/default value in that case). An
        explicit apps.yaml battery_capacity_kwh always wins over this."""
        flat = self._flat()
        candidates = [eid for eid in flat
                      if eid.startswith("sensor.") and self._is_brand(eid)
                      and "capacity" in eid
                      and ("battery" in eid or "ess" in eid or "storage" in eid)]
        if not candidates:
            candidates = [
                eid for eid in flat
                if eid.startswith("sensor.") and "capacity" in eid
                and str((flat[eid] or {}).get("attributes", {}).get("device_class", "")).lower()
                in ("energy_storage", "energy")]
        plant = [eid for eid in candidates if "plant" in eid or "total" in eid]
        pool = plant or candidates
        live = [eid for eid in pool if self._is_live(flat.get(eid))]
        pool = live or pool
        if not pool:
            return None
        try:
            return float(flat[pool[0]].get("state"))
        except (TypeError, ValueError):
            return None

    def hardware_max_kw(self, number_entity):
        """The number.* entity's own declared max (min/max/step are
        standard HA number-entity attributes) — used to clamp a
        misconfigured apps.yaml charge/discharge rate to what the
        inverter itself says it actually supports."""
        if not number_entity:
            return None
        attrs = (self._flat().get(number_entity) or {}).get("attributes", {}) or {}
        for key in ("max", "native_max_value", "max_value"):
            if key in attrs:
                try:
                    return float(attrs[key])
                except (TypeError, ValueError):
                    continue
        return None
