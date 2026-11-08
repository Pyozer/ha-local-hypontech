#!/usr/bin/env python3
"""Local MQTT bridge for the Hypon COM-WF Wi-Fi collector (HMS-800W-C micro-inverter).

The collector talks MQTT 3.1.1 in clear text to hk.hypon.cloud:1883. Point that name to the
machine running this bridge (DNS rewrite) and the bridge will:

* relay the connection byte-for-byte to the real Hypon cloud (the vendor app keeps working);
* decode the `invmonitor` frames and publish them to a local MQTT broker (Home Assistant),
  with MQTT Discovery so the sensors appear automatically;
* answer the collector itself (CONNACK/SUBACK/PUBACK/PINGRESP) when the cloud is unreachable,
  so local data keeps flowing without Internet, and hand back to the cloud once it is back.

Standard library only. Configuration comes from environment variables, optionally loaded
from a KEY=VALUE file given with --env. See hypon_bridge.env.example.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import struct
import sys
import time

LOG = logging.getLogger("hypon")

DEFAULT_UPSTREAM = "gateway-be8b19e7047b780f.elb.eu-central-1.amazonaws.com"


# --------------------------------------------------------------------------- configuration

def load_env_file(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self):
        e = os.environ.get
        self.listen_host = e("HYPON_LISTEN_HOST", "0.0.0.0")
        self.listen_port = int(e("HYPON_LISTEN_PORT", "1883"))
        self.upstream_enabled = e("HYPON_UPSTREAM", "on").lower() not in ("off", "0", "false", "no")
        self.upstream_host = e("HYPON_UPSTREAM_HOST", DEFAULT_UPSTREAM)
        self.upstream_port = int(e("HYPON_UPSTREAM_PORT", "1883"))
        self.cloud_retry_s = int(e("HYPON_CLOUD_RETRY_S", "300"))
        self.base_topic = e("HYPON_BASE_TOPIC", "hypon")
        # Availability of the bridge itself (MQTT Last Will): what the sensors depend on.
        self.status_topic = f"{self.base_topic}/bridge/status"
        # Seconds without the collector before it is declared offline (absorbs dusk flapping).
        self.offline_grace_s = int(e("HYPON_OFFLINE_GRACE_S", "300"))
        self.publish_raw = e("HYPON_PUBLISH_RAW", "on").lower() not in ("off", "0", "false", "no")
        self.ha_host = e("HA_MQTT_HOST")
        self.ha_port = int(e("HA_MQTT_PORT", "1883"))
        self.ha_user = e("HA_MQTT_USER")
        self.ha_password = e("HA_MQTT_PASSWORD")
        self.discovery_prefix = e("HA_DISCOVERY_PREFIX", "homeassistant")


# --------------------------------------------------------------------------- MQTT codec

def enc_varint(n):
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def enc_str(s):
    if isinstance(s, str):
        s = s.encode()
    return struct.pack(">H", len(s)) + s


def mqtt_packet(first_byte, body=b""):
    return bytes([first_byte]) + enc_varint(len(body)) + body


class MqttStreamParser:
    """Incremental MQTT framing: feed() raw TCP bytes, get back complete (first_byte, body)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        packets = []
        while len(self.buf) >= 2:
            remaining, mult, pos, complete = 0, 1, 1, False
            while pos < len(self.buf) and pos <= 4:
                byte = self.buf[pos]
                remaining += (byte & 0x7F) * mult
                mult *= 128
                pos += 1
                if not byte & 0x80:
                    complete = True
                    break
            if not complete:
                if pos > 4:
                    raise ValueError("malformed MQTT remaining length")
                break
            if len(self.buf) < pos + remaining:
                break
            packets.append((self.buf[0], bytes(self.buf[pos:pos + remaining])))
            del self.buf[:pos + remaining]
        return packets


def parse_publish(first_byte, body):
    qos = (first_byte >> 1) & 3
    tlen = struct.unpack_from(">H", body, 0)[0]
    topic = body[2:2 + tlen].decode("utf-8", "replace")
    pos = 2 + tlen
    pid = None
    if qos:
        pid = struct.unpack_from(">H", body, pos)[0]
        pos += 2
    return topic, qos, pid, body[pos:]


def parse_connect(body):
    """Return (client_id, username) from a CONNECT body. The password is never returned."""
    pos = 0

    def read_str():
        nonlocal pos
        n = struct.unpack_from(">H", body, pos)[0]
        value = body[pos + 2:pos + 2 + n]
        pos += 2 + n
        return value

    read_str()  # protocol name
    flags = body[pos + 1]
    pos += 4  # level, flags, keepalive
    client_id = read_str().decode("utf-8", "replace")
    if flags & 0x04:
        read_str()
        read_str()
    username = read_str().decode("utf-8", "replace") if flags & 0x80 else None
    return client_id, username


# --------------------------------------------------------------------------- Hypon payloads

HEADER_LEN = 52  # "YYYY-MM-DD HH:MM:SS" + 0xFF + collector S/N (16) + inverter S/N (16)
MONITOR_MIN_LEN = HEADER_LEN + 0xE4


def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _s16(b, o):
    return struct.unpack_from(">h", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _f32(b, o):
    return struct.unpack_from(">f", b, o)[0]


def _ascii(raw):
    return raw.decode("ascii", "replace").strip(" \x00")


def decode_monitor(payload):
    """Decode an `invmonitor` / `offinvmonitor` payload. Offsets: see PROTOCOLE_HYPON.md."""
    if len(payload) < MONITOR_MIN_LEN:
        raise ValueError(f"monitor payload too short ({len(payload)} bytes)")
    b = payload[HEADER_LEN:]
    pv1_v, pv1_a = _u16(b, 0x26) / 10, _u16(b, 0x28) / 10
    pv2_v, pv2_a = _u16(b, 0x2A) / 10, _u16(b, 0x2C) / 10
    return {
        "device_time": _ascii(payload[0:19]),
        "collector_sn": _ascii(payload[20:36]),
        "inverter_sn": _ascii(payload[36:52]),
        "power": _u32(b, 0x00),
        "pv1_voltage": pv1_v,
        "pv1_current": pv1_a,
        "pv1_power": round(pv1_v * pv1_a, 1),
        "pv2_voltage": pv2_v,
        "pv2_current": pv2_a,
        "pv2_power": round(pv2_v * pv2_a, 1),
        "grid_voltage": _u16(b, 0x3E) / 10,
        "grid_current": _u16(b, 0x40) / 10,
        "grid_frequency": _u16(b, 0x50) / 100,
        "temperature": _s16(b, 0x5C) / 10,
        "energy_total": round(_f32(b, 0x6C), 2),
        "energy_pv1": round(_f32(b, 0xD8), 2),
        "energy_pv2": round(_f32(b, 0xE0), 2),
    }


def decode_invinfo(payload):
    text = payload.decode("latin-1")
    firmware = re.search(r"H\d{3}-\d{5}-\d{2}", text)
    model = re.search(r"HMS-[0-9A-Z-]+", text)
    return {
        "inverter_sn": _ascii(payload[20:36]),
        "firmware": firmware.group(0) if firmware else None,
        "model": model.group(0) if model else None,
    }


SENSORS = [
    # key, name, unit, device_class, state_class
    ("power", "Puissance", "W", "power", "measurement"),
    ("pv1_voltage", "PV1 tension", "V", "voltage", "measurement"),
    ("pv1_current", "PV1 courant", "A", "current", "measurement"),
    ("pv1_power", "PV1 puissance", "W", "power", "measurement"),
    ("pv2_voltage", "PV2 tension", "V", "voltage", "measurement"),
    ("pv2_current", "PV2 courant", "A", "current", "measurement"),
    ("pv2_power", "PV2 puissance", "W", "power", "measurement"),
    ("grid_voltage", "Réseau tension", "V", "voltage", "measurement"),
    ("grid_current", "Réseau courant", "A", "current", "measurement"),
    ("grid_frequency", "Réseau fréquence", "Hz", "frequency", "measurement"),
    ("temperature", "Température", "°C", "temperature", "measurement"),
    ("energy_total", "Énergie totale", "kWh", "energy", "total_increasing"),
    ("energy_pv1", "PV1 énergie totale", "kWh", "energy", "total_increasing"),
    ("energy_pv2", "PV2 énergie totale", "kWh", "energy", "total_increasing"),
]

# Instantaneous production values forced to 0 while the collector is offline (no sun, inverter
# asleep), like the Hypon cloud does. Totals, temperature and grid voltage/frequency keep their
# last value.
ZERO_WHEN_OFFLINE = (
    "power", "pv1_voltage", "pv1_current", "pv1_power",
    "pv2_voltage", "pv2_current", "pv2_power", "grid_current",
)


# --------------------------------------------------------------------------- local broker client

class LocalMqtt:
    """Minimal MQTT 3.1.1 publisher (QoS 0) with automatic reconnection.

    Registers a retained Last Will "offline" on the bridge status topic, so entities only become
    unavailable when the add-on itself is gone, not when the inverter sleeps.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.writer = None
        self.on_connect = None

    async def run(self):
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive, retry
                LOG.warning("local MQTT: %s, retry in 10 s", exc)
            self.writer = None
            await asyncio.sleep(10)

    async def _session(self):
        cfg = self.cfg
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg.ha_host, cfg.ha_port), 10)
        flags = 0x02 | 0x04 | 0x20  # clean session + will + retained will (QoS 0)
        payload = enc_str("hypon-bridge") + enc_str(cfg.status_topic) + enc_str("offline")
        if cfg.ha_user:
            flags |= 0x80
            payload += enc_str(cfg.ha_user)
            if cfg.ha_password:
                flags |= 0x40
                payload += enc_str(cfg.ha_password)
        body = enc_str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60) + payload
        writer.write(mqtt_packet(0x10, body))
        await writer.drain()
        connack = await asyncio.wait_for(reader.readexactly(4), 10)
        if connack[0] != 0x20 or connack[3] != 0:
            writer.close()
            raise ConnectionError(f"CONNACK refused (code {connack[3]})")
        LOG.info("local MQTT: connected to %s:%s", cfg.ha_host, cfg.ha_port)
        self.writer = writer
        await self.publish(cfg.status_topic, "online", retain=True)
        if self.on_connect:
            await self.on_connect()
        ping = asyncio.create_task(self._pinger(writer))
        try:
            while await reader.read(1024):
                pass  # PINGRESP; nothing else is expected
            raise ConnectionError("connection closed by broker")
        finally:
            ping.cancel()
            writer.close()

    async def _pinger(self, writer):
        while True:
            await asyncio.sleep(30)
            writer.write(mqtt_packet(0xC0))
            await writer.drain()

    async def publish(self, topic, payload, retain=False):
        if self.writer is None:
            return
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            self.writer.write(mqtt_packet(0x30 | (1 if retain else 0), enc_str(topic) + payload))
            await self.writer.drain()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("local MQTT publish failed: %s", exc)


class NullMqtt:
    on_connect = None

    async def run(self):
        LOG.info("local MQTT: HA_MQTT_HOST not set, console output only")

    async def publish(self, topic, payload, retain=False):
        return


# --------------------------------------------------------------------------- bridge

class Bridge:
    def __init__(self, cfg, local):
        self.cfg = cfg
        self.local = local
        self.local.on_connect = self._on_local_connect
        self.devices = {}  # inverter S/N -> info dict
        self.announced = set()
        self.connected = set()      # inverter S/N currently considered online
        self.last_state = {}        # inverter S/N -> last decoded monitor frame
        self.last_seen = {}         # inverter S/N -> time.monotonic() of the last live frame
        self.offline_tasks = {}     # inverter S/N -> pending "declare offline" task

    async def _on_local_connect(self):
        self.announced.clear()

    # ----- data handling

    async def on_device_publish(self, topic, payload, origin):
        base = self.cfg.base_topic
        try:
            if topic == "invmonitor":
                data = decode_monitor(payload)
                sn = data["inverter_sn"]
                LOG.info("%s P=%sW PV1=%sV/%sA PV2=%sV/%sA grid=%sV %sHz T=%s°C E=%skWh",
                         data["device_time"], data["power"], data["pv1_voltage"],
                         data["pv1_current"], data["pv2_voltage"], data["pv2_current"],
                         data["grid_voltage"], data["grid_frequency"], data["temperature"],
                         data["energy_total"])
                self.last_seen[sn] = time.monotonic()
                self.last_state[sn] = data
                self.connected.add(sn)
                pending = self.offline_tasks.pop(sn, None)
                if pending:
                    pending.cancel()
                await self._announce(sn)
                await self.local.publish(f"{base}/{sn}/state", json.dumps(data), retain=True)
                await self.local.publish(f"{base}/{sn}/connection", "ON", retain=True)
            elif topic == "offinvmonitor":
                data = decode_monitor(payload)
                LOG.info("buffered frame from %s: P=%sW E=%skWh",
                         data["device_time"], data["power"], data["energy_total"])
                await self.local.publish(f"{base}/{data['inverter_sn']}/offline", json.dumps(data))
            elif topic == "invinfo":
                info = decode_invinfo(payload)
                LOG.info("inverter info: %s", info)
                self.devices[info["inverter_sn"]] = info
                self.announced.discard(info["inverter_sn"])
            else:
                LOG.debug("publish %s (%d bytes, %s)", topic, len(payload), origin)
        except Exception as exc:  # noqa: BLE001 - never break the relay on a decode error
            LOG.warning("cannot decode %s (%d bytes): %s", topic, len(payload), exc)
        if self.cfg.publish_raw:
            await self.local.publish(f"{base}/raw/{topic}", payload.hex())

    async def _announce(self, sn):
        if sn in self.announced:
            return
        self.announced.add(sn)
        info = self.devices.get(sn, {})
        base, prefix = self.cfg.base_topic, self.cfg.discovery_prefix
        device = {
            "identifiers": [f"hypon_{sn}"],
            "manufacturer": "Hypontech",
            "model": info.get("model") or "HMS-800W-C",
            "name": f"Hypon {sn}",
            "serial_number": sn,
        }
        if info.get("firmware"):
            device["sw_version"] = info["firmware"]
        # Entities depend on the bridge (Last Will), NOT on the inverter: when the inverter sleeps at
        # night they keep their last/zeroed value instead of going "unavailable".
        availability_topic = self.cfg.status_topic
        for key, name, unit, dev_class, state_class in SENSORS:
            config = {
                "name": name,
                "unique_id": f"hypon_{sn}_{key}",
                "object_id": f"hypon_{key}",
                "state_topic": f"{base}/{sn}/state",
                "value_template": "{{ value_json.%s }}" % key,
                "availability_topic": availability_topic,
                "unit_of_measurement": unit,
                "device_class": dev_class,
                "state_class": state_class,
                "device": device,
            }
            await self.local.publish(f"{prefix}/sensor/hypon_{sn}/{key}/config",
                                     json.dumps(config), retain=True)
        connection = {
            "name": "Connexion",
            "unique_id": f"hypon_{sn}_connection",
            "object_id": "hypon_connection",
            "state_topic": f"{base}/{sn}/connection",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": availability_topic,
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "device": device,
        }
        await self.local.publish(f"{prefix}/binary_sensor/hypon_{sn}/connection/config",
                                 json.dumps(connection), retain=True)
        # Clear the per-inverter availability topic used by versions <= 0.2.0 (retained "offline").
        await self.local.publish(f"{base}/{sn}/availability", "", retain=True)

    async def on_device_gone(self):
        """The collector's TCP session ended. Do not flag the inverter offline right away: at dusk
        it drops and reconnects repeatedly. Wait the grace period, then check nothing came back."""
        since = time.monotonic()
        for sn in list(self.connected):
            pending = self.offline_tasks.pop(sn, None)
            if pending:
                pending.cancel()
            self.offline_tasks[sn] = asyncio.create_task(self._offline_after_grace(sn, since))

    async def _offline_after_grace(self, sn, since):
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self.cfg.offline_grace_s)
            if self.last_seen.get(sn, 0) > since:
                return  # a live frame arrived in the meantime: the collector is back
            await self._mark_offline(sn)
        finally:
            if self.offline_tasks.get(sn) is task:
                del self.offline_tasks[sn]

    async def _mark_offline(self, sn):
        self.connected.discard(sn)
        state = dict(self.last_state.get(sn, {}))
        if state:
            for key in ZERO_WHEN_OFFLINE:
                if key in state:
                    state[key] = 0
            await self.local.publish(f"{self.cfg.base_topic}/{sn}/state", json.dumps(state), retain=True)
        await self.local.publish(f"{self.cfg.base_topic}/{sn}/connection", "OFF", retain=True)
        LOG.info("inverter %s offline for %ss: production zeroed, totals kept",
                 sn, self.cfg.offline_grace_s)

    async def tap(self, first_byte, body, origin):
        ptype = first_byte >> 4
        if ptype == 1:
            client_id, username = parse_connect(body)
            LOG.info("collector CONNECT client_id=%s username=%r (%s)", client_id, username, origin)
        elif ptype == 3:
            topic, _qos, _pid, payload = parse_publish(first_byte, body)
            await self.on_device_publish(topic, payload, origin)
        elif ptype == 8:
            LOG.info("collector SUBSCRIBE (%s)", origin)

    async def tap_cloud(self, first_byte, body):
        ptype = first_byte >> 4
        if ptype == 3:
            topic, _qos, _pid, payload = parse_publish(first_byte, body)
            LOG.warning("CLOUD -> collector on %r: %s", topic, payload.hex())
            if self.cfg.publish_raw:
                await self.local.publish(f"{self.cfg.base_topic}/raw/cloud/{topic}", payload.hex())
        elif ptype == 2:
            LOG.info("cloud CONNACK code=%s", body[1] if len(body) > 1 else "?")

    # ----- connection handling

    async def handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        LOG.info("collector connected from %s", peer)
        upstream = None
        if self.cfg.upstream_enabled:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(
                    self.cfg.upstream_host, self.cfg.upstream_port), 8)
                LOG.info("relaying to cloud %s:%s", self.cfg.upstream_host, self.cfg.upstream_port)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("cloud unreachable (%s): answering locally", exc)
        try:
            if upstream:
                await self._relay(reader, writer, *upstream)
            else:
                await self._serve_locally(reader, writer)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("session error: %s", exc)
        finally:
            for w in (writer, upstream[1] if upstream else None):
                if w:
                    w.close()
            LOG.info("collector %s disconnected", peer)
            await self.on_device_gone()

    async def _relay(self, c_reader, c_writer, u_reader, u_writer):
        async def pump(src, dst, on_packet):
            parser = MqttStreamParser()
            while True:
                data = await src.read(4096)
                if not data:
                    return
                dst.write(data)  # forward first, untouched
                await dst.drain()
                try:  # observation only: a parsing problem must never break the relay
                    for first_byte, body in parser.feed(data):
                        await on_packet(first_byte, body)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("tap error (relay unaffected): %s", exc)
                    parser = MqttStreamParser()

        tasks = [
            asyncio.create_task(pump(c_reader, u_writer, lambda f, b: self.tap(f, b, "relayed"))),
            asyncio.create_task(pump(u_reader, c_writer, self.tap_cloud)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception():
                raise t.exception()

    async def _serve_locally(self, reader, writer):
        parser = MqttStreamParser()
        watchdog = asyncio.create_task(self._cloud_watchdog(writer)) if self.cfg.upstream_enabled else None
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                for first_byte, body in parser.feed(data):
                    reply = self._local_reply(first_byte, body)
                    if reply == "close":
                        return
                    if reply:
                        writer.write(reply)
                        await writer.drain()
                    await self.tap(first_byte, body, "local")
        finally:
            if watchdog:
                watchdog.cancel()

    @staticmethod
    def _local_reply(first_byte, body):
        ptype = first_byte >> 4
        if ptype == 1:                                   # CONNECT -> CONNACK accepted
            return mqtt_packet(0x20, b"\x00\x00")
        if ptype == 3:                                   # PUBLISH -> PUBACK / PUBREC
            qos = (first_byte >> 1) & 3
            _t, _q, pid, _p = parse_publish(first_byte, body)
            if qos == 1:
                return mqtt_packet(0x40, struct.pack(">H", pid))
            if qos == 2:
                return mqtt_packet(0x50, struct.pack(">H", pid))
            return None
        if ptype == 6:                                   # PUBREL -> PUBCOMP
            return mqtt_packet(0x70, body[:2])
        if ptype == 8:                                   # SUBSCRIBE -> SUBACK
            pid, pos, granted = body[:2], 2, bytearray()
            while pos < len(body):
                tlen = struct.unpack_from(">H", body, pos)[0]
                granted.append(min(body[pos + 2 + tlen], 1))
                pos += 3 + tlen
            return mqtt_packet(0x90, pid + bytes(granted))
        if ptype == 12:                                  # PINGREQ -> PINGRESP
            return mqtt_packet(0xD0)
        if ptype == 14:                                  # DISCONNECT
            return "close"
        return None

    async def _cloud_watchdog(self, writer):
        """While answering locally, check the cloud now and then; once it is reachable again,
        drop the collector so it reconnects and gets relayed (its buffered data goes up)."""
        while True:
            await asyncio.sleep(self.cfg.cloud_retry_s)
            try:
                _r, w = await asyncio.wait_for(asyncio.open_connection(
                    self.cfg.upstream_host, self.cfg.upstream_port), 8)
                w.close()
            except Exception:  # noqa: BLE001
                continue
            LOG.info("cloud reachable again: dropping collector so it reconnects through the relay")
            writer.close()
            return


async def main_async(cfg):
    local = LocalMqtt(cfg) if cfg.ha_host else NullMqtt()
    bridge = Bridge(cfg, local)
    server = await asyncio.start_server(bridge.handle_client, cfg.listen_host, cfg.listen_port)
    LOG.info("listening on %s:%s, cloud relay %s", cfg.listen_host, cfg.listen_port,
             f"{cfg.upstream_host}:{cfg.upstream_port}" if cfg.upstream_enabled else "disabled")
    async with server:
        await asyncio.gather(server.serve_forever(), local.run())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", help="KEY=VALUE configuration file")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.env:
        load_env_file(args.env)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        asyncio.run(main_async(Config()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
