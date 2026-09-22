"""EV Charge Assist — decides whether to actively charge whichever Tesla is
currently plugged into the shared home charger.

This exists because the supplier's own smart charging (Octopus's
Intelligent Octopus Go here) is linked to the Hypervolt charger itself,
not to either specific vehicle — confirmed live: its own data shows
`provider: HYPERVOLT` and no vehicle field at all, and the Hypervolt
can't read a Tesla's real state of charge over its own protocol, so
Octopus's own charge-target logic runs blind (its own state of charge
sensor reads "unknown"). There's no HA service to feed it a real
reading either (checked the full Octopus Energy service list — nothing
exists for this). Rather than work around that gap, GridLock reads each
vehicle's REAL battery %/target directly from Tesla's own Fleet API
data (accurate, no charger involved) and takes over active charge
control itself for whichever car is actually connected right now.
"""


def should_charge(battery_pct, charge_limit_pct, in_cheap_window):
    """True if the connected vehicle still genuinely needs charge and
    now is a cheap-rate/dispatch slot. battery_pct/charge_limit_pct of
    None (e.g. a freshly-paired vehicle HA hasn't polled yet) means
    "don't act" rather than guessing — the whole point of this feature
    is only ever charging off a real reading, never a blind default."""
    if battery_pct is None or charge_limit_pct is None:
        return False
    if battery_pct >= charge_limit_pct:
        return False
    return bool(in_cheap_window)
