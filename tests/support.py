"""Shared helpers for the test-suite (standard library only).

Importing this module puts the add-on sources on ``sys.path`` so that tests can simply
``import hypon_bridge`` / ``import hypon_intercept``.
"""
import asyncio
import logging
import os
import socket
import struct
import sys
import time
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ADDON = os.path.join(ROOT, "hypon-local-addon")
APP = os.path.join(ADDON, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

import hypon_bridge as hb  # noqa: E402

logging.disable(logging.CRITICAL)  # keep the test output readable (the code logs a lot)

SN = "P08280A023B00177"            # inverter serial number (16 chars, as sent on the wire)
COLLECTOR_SN = "COLLECTOR0000001"  # collector serial number (16 chars)


# --------------------------------------------------------------------------- Hypon frames

def build_monitor(power=0, pv1=(0.0, 0.0), pv2=(0.0, 0.0), grid_v=230.0, grid_a=0.0, freq=50.0,
                  temp=25.0, e_total=0.0, e_pv1=0.0, e_pv2=0.0, sn=SN, collector=COLLECTOR_SN,
                  device_time="2026-11-08 17:00:00"):
    """Build an `invmonitor` payload using the offsets documented in hypon_bridge."""
    head = device_time.encode() + b"\xff" + collector.encode() + sn.encode()
    assert len(head) == hb.HEADER_LEN
    body = bytearray(0xE4)
    struct.pack_into(">I", body, 0x00, power)
    struct.pack_into(">H", body, 0x26, round(pv1[0] * 10))
    struct.pack_into(">H", body, 0x28, round(pv1[1] * 10))
    struct.pack_into(">H", body, 0x2A, round(pv2[0] * 10))
    struct.pack_into(">H", body, 0x2C, round(pv2[1] * 10))
    struct.pack_into(">H", body, 0x3E, round(grid_v * 10))
    struct.pack_into(">H", body, 0x40, round(grid_a * 10))
    struct.pack_into(">H", body, 0x50, round(freq * 100))
    struct.pack_into(">h", body, 0x5C, round(temp * 10))
    struct.pack_into(">f", body, 0x6C, e_total)
    struct.pack_into(">f", body, 0xD8, e_pv1)
    struct.pack_into(">f", body, 0xE0, e_pv2)
    return head + bytes(body)


def build_invinfo(sn=SN, firmware="H123-45678-90", model="HMS-800W-2T"):
    return b"X" * 20 + sn.encode() + f" {firmware} {model} ".encode()


# --------------------------------------------------------------------------- MQTT packets

def connect_packet(client_id="COMWF", flags=0x02, extra=b""):
    body = hb.enc_str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60) + hb.enc_str(client_id)
    return hb.mqtt_packet(0x10, body + extra)


def publish_packet(topic, payload, qos=0, pid=1, retain=False):
    body = hb.enc_str(topic)
    if qos:
        body += struct.pack(">H", pid)
    return hb.mqtt_packet(0x30 | (qos << 1) | int(retain), body + payload)


CONNACK_OK = b"\x20\x02\x00\x00"


# --------------------------------------------------------------------------- fakes

class RecordingMqtt:
    """Stands in for LocalMqtt/NullMqtt and records what the bridge publishes."""

    def __init__(self):
        self.published = []  # (topic, payload str, retain)
        self.on_connect = None

    async def run(self):
        return

    async def publish(self, topic, payload, retain=False):
        if isinstance(payload, bytes):
            payload = payload.decode()
        self.published.append((topic, payload, retain))

    def topics(self):
        return [t for t, _p, _r in self.published]

    def last(self, topic):
        found = [(p, r) for t, p, r in self.published if t == topic]
        return found[-1] if found else None

    def count(self, topic=None, prefix=None):
        return len([t for t in self.topics()
                    if (topic is None or t == topic) and (prefix is None or t.startswith(prefix))])


class FakeServer:
    """Tiny TCP server on an ephemeral port recording the raw bytes it receives."""

    def __init__(self, on_data=None):
        self.received = bytearray()
        self.connections = 0
        self.writers = []
        self._on_data = on_data
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self

    @property
    def port(self):
        return self._server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.connections += 1
        self.writers.append(writer)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                self.received += data
                if self._on_data:
                    await self._on_data(data, writer)
        except (ConnectionError, asyncio.CancelledError):
            return

    async def close(self):
        for w in self.writers:
            w.close()
        self._server.close()
        await self._server.wait_closed()


class FakeBroker(FakeServer):
    """Fake MQTT broker: answers CONNECT with a configurable CONNACK, records every packet."""

    def __init__(self, connack=CONNACK_OK):
        super().__init__(self._handle)
        self.connack = connack
        self.packets = []   # (first_byte, body)
        self._parser = hb.MqttStreamParser()

    async def _handle(self, data, writer):
        for first_byte, body in self._parser.feed(data):
            self.packets.append((first_byte, body))
            if first_byte >> 4 == 1:
                writer.write(self.connack)
                await writer.drain()

    def publishes(self):
        """Decoded PUBLISH packets as (topic, payload str, retain)."""
        out = []
        for first_byte, body in self.packets:
            if first_byte >> 4 == 3:
                topic, _q, _p, payload = hb.parse_publish(first_byte, body)
                out.append((topic, payload.decode(), bool(first_byte & 1)))
        return out


# --------------------------------------------------------------------------- utilities

def make_config(**env):
    """Build a hypon_bridge.Config from an explicit environment (nothing leaks in)."""
    with mock.patch.dict(os.environ, env, clear=True):
        return hb.Config()


def free_port():
    """A TCP port that nothing listens on (bound then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_until(predicate, within=3.0, interval=0.01):
    """Poll until predicate() is truthy; fail the test with a clear message after `within` s."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % within)
