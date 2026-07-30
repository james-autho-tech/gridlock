"""Pure-Python optimisation core for GridLock — no AppDaemon/HA imports here.

Everything in this package takes plain data in (slot dicts, timestamps,
floats) and returns plain data out, specifically so it can be unit tested
(see tests/) without a live Home Assistant/AppDaemon runtime. gridlock.py
(the hass.Hass subclass) is the only thing that talks to HA; it gathers
state, calls into this package, and writes back whatever the result says
to write.
"""
