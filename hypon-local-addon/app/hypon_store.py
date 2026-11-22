"""Tiny persistent state kept in the add-on's `/data` folder.

Without it, a restart (power cut, update, reboot) makes the add-on forget everything while the
MQTT broker still holds the last retained values: Home Assistant would keep showing a production
that nobody confirms any more. With it, the add-on knows on restart which inverters it was
serving, their last values and their MAC address, and can keep the entities truthful.

Standard library only. Writes are atomic and throttled (flash friendly); a missing, unwritable
or corrupted file never stops the add-on, it just starts from scratch.
"""
import json
import logging
import os
import time

LOG = logging.getLogger("hypon.store")

VERSION = 1
FILE_NAME = "state.json"
FLUSH_EVERY_S = 300.0  # at most one routine write every 5 minutes; changes of status are forced


class StateStore:
    def __init__(self, directory=None, clock=time.monotonic):
        self.path = os.path.join(directory, FILE_NAME) if directory else None
        self.data = {"version": VERSION, "macs": {}, "inverters": {}}
        self._clock = clock
        self._dirty = False
        self._last_flush = None

    @classmethod
    def open(cls, directory):
        """Load the state from `directory`; without a usable folder, work in memory only."""
        if directory and not os.path.isdir(directory):
            LOG.warning("persistent folder %s not found: the state will not survive a restart",
                        directory)
            directory = None
        store = cls(directory)
        store.load()
        return store

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError) as exc:
            LOG.warning("cannot read %s (%s): starting from scratch", self.path, exc)
            return
        if not isinstance(loaded, dict) or loaded.get("version") != VERSION:
            LOG.warning("%s has an unknown format: starting from scratch", self.path)
            return
        for key in ("macs", "inverters"):
            if isinstance(loaded.get(key), dict):
                self.data[key] = loaded[key]

    # ----- typed accessors

    def get_mac(self, ip):
        return self.data["macs"].get(ip)

    def set_mac(self, ip, mac):
        if self.data["macs"].get(ip) != mac:
            self.data["macs"][ip] = mac
            self.mark_dirty()

    # ----- persistence

    def mark_dirty(self):
        self._dirty = True

    def flush(self, force=False):
        """Write the state if it changed. Routine writes are throttled; `force` bypasses that."""
        if not self._dirty or not self.path:
            return False
        now = self._clock()
        if not force and self._last_flush is not None and now - self._last_flush < FLUSH_EVERY_S:
            return False
        self._last_flush = now  # also throttles retries after a failure
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("cannot write %s: %s", self.path, exc)
            return False
        self._dirty = False
        return True
