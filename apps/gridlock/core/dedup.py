"""Write-deduplication — never send a command to an HA/inverter entity
if the target state already matches the live state. Extracted from the
old apply()'s inline `if self.get_state(x) != y:` checks so it's
independently unit-testable (flash/EEPROM wear protection on a real
inverter is exactly the kind of thing that should have a test, not just
an inline check nobody re-verifies)."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Write:
    entity_id: str
    service: str          # e.g. "select/select_option", "number/set_value"
    payload: dict          # extra call_service kwargs, e.g. {"option": ...} / {"value": ...}


def _differs(current, desired):
    if current is None:
        return True  # unknown current state -> always safe to (re)assert desired
    if isinstance(desired, float) or isinstance(current, (int, float)):
        try:
            return abs(float(current) - float(desired)) > 1e-6
        except (TypeError, ValueError):
            return current != desired
    return current != desired


def plan_writes(current_state, desired_state, entity_services):
    """current_state / desired_state: {key: value} using the same keys
    as entity_services: {key: (entity_id, service, payload_key)}. Only
    keys present in desired_state are considered — a key desired_state
    omits (e.g. an entity that wasn't discovered) is never written.
    Returns a list[Write] of only the entities whose live value doesn't
    already match what's wanted."""
    writes = []
    for key, desired in desired_state.items():
        if key not in entity_services:
            continue
        entity_id, service, payload_key = entity_services[key]
        if not entity_id:
            continue
        current = current_state.get(key)
        if _differs(current, desired):
            writes.append(Write(entity_id, service, {payload_key: desired}))
    return writes
