"""Availability model of a micro-inverter: what its silence means.

A micro-inverter that sends nothing is either off (no light on the panels, at night or under a
storm: its production really is 0 W) or lost (changed IP address, interception broken: its data
cannot be trusted). Two things tell them apart, never the sun:

* what it was doing when it fell silent: an inverter that was producing next to nothing (the
  evening fade, the morning start, a storm) simply ran out of light, while one that vanished
  in the middle of a real production did not;
* the network: an inverter that is powered answers ARP, at its configured IP or, if its address
  changed, at another one. Beware that its Wi-Fi collector often stays on the network, answering
  ARP, for a long time after the inverter stopped sending (the dusk of 22 November: hours),
  so an answer alone proves nothing.

`decide` turns that evidence into four statuses:

* ``online``      frames are arriving;
* ``sleeping``    silent and it had faded, or it was nowhere to be seen: off, production is 0,
                  entities stay available;
* ``unreachable`` it was producing, then vanished although it is on the network (or there is no
                  way to look): unavailable;
* ``ip_changed``  silent, and its MAC address was seen under another IP: unavailable.

Standard library only; no I/O here, so the rules are easy to test.
"""
import statistics
from collections import deque
from dataclasses import dataclass

ONLINE = "online"
SLEEPING = "sleeping"
UNREACHABLE = "unreachable"
IP_CHANGED = "ip_changed"
STATUSES = (ONLINE, SLEEPING, UNREACHABLE, IP_CHANGED)
# Statuses in which the measurements cannot be trusted: the entities become unavailable.
UNAVAILABLE_STATUSES = (UNREACHABLE, IP_CHANGED)

# An inverter whose last frame showed at most this production (W) fell silent because it ran out
# of light. The error is bounded: a lost inverter that was producing less than this reads 0 W.
FADED_POWER_W = 20
STALE_FACTOR = 3        # silence tolerated, in multiples of the usual time between two frames
CADENCE_SAMPLES = 20    # how many recent gaps between frames are remembered
CADENCE_MAX_GAP_S = 1800  # a longer gap is a night or an outage, not the reporting rhythm


@dataclass(frozen=True)
class Link:
    """What the network says about an inverter (filled in by the interceptor)."""

    mac: str | None = None        # hardware address of the inverter, once known
    alive: bool = False           # it recently answered ARP at its configured IP
    moved_to: str | None = None   # IP address its MAC was recently seen at instead
    checked: bool = False         # the network was looked at recently: "not seen" means absent


NO_LINK = Link()


def decide(silent_for, threshold, link, faded=False):
    """Status of an inverter that has been silent for `silent_for` seconds.

    `threshold` is the silence tolerated before anything is concluded (seconds), `link` the
    network evidence (see Link), `faded` whether its last frame showed (almost) no production."""
    if silent_for <= threshold:
        return ONLINE
    if link.moved_to:
        return IP_CHANGED       # positive evidence, whatever it was doing
    if faded:
        return SLEEPING         # it ran out of light; its collector may well still be on the network
    if link.alive:
        return UNREACHABLE      # it was producing and is on the network, yet it does not talk to us
    if not link.checked:
        return UNREACHABLE      # we cannot look: do not claim it is simply off
    return SLEEPING             # looked everywhere and it is not there: it is off


class Cadence:
    """Remembers how often an inverter reports, to size the silence it is allowed."""

    def __init__(self):
        self._gaps = deque(maxlen=CADENCE_SAMPLES)

    def add(self, gap):
        if 0 < gap <= CADENCE_MAX_GAP_S:
            self._gaps.append(gap)

    def median(self):
        return statistics.median(self._gaps) if self._gaps else None


def stale_threshold(grace, cadence):
    """Silence (seconds) after which an inverter is no longer considered online: the configured
    grace, or a few reporting periods if the inverter reports slowly."""
    median = cadence.median()
    return grace if median is None else max(grace, STALE_FACTOR * median)
