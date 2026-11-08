"""Bridge logic: Home Assistant discovery, availability/offline handling, topic handling."""
import asyncio
import json
import struct
import time
import unittest

import support
from support import SN, hb, make_config, wait_until


class BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mqtt = support.RecordingMqtt()
        self.cfg = make_config(HYPON_PUBLISH_RAW="off")
        self.cfg.offline_grace_s = 0.05
        self.bridge = hb.Bridge(self.cfg, self.mqtt)

    async def asyncTearDown(self):
        for task in list(self.bridge.offline_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    async def frame(self, **kwargs):
        await self.bridge.on_device_publish("invmonitor", support.build_monitor(**kwargs), "test")

    def state(self):
        return json.loads(self.mqtt.last(f"hypon/{SN}/state")[0])

    def config_of(self, kind, key):
        return json.loads(self.mqtt.last(f"homeassistant/{kind}/hypon_{SN}/{key}/config")[0])


class DiscoveryTests(BridgeTestCase):
    async def test_first_frame_publishes_retained_state_and_connection(self):
        await self.frame(power=123, e_total=50.5)
        payload, retain = self.mqtt.last(f"hypon/{SN}/state")
        self.assertTrue(retain)
        self.assertEqual(json.loads(payload)["power"], 123)
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))

    async def test_one_discovery_config_per_sensor_plus_connection(self):
        await self.frame()
        for key, *_ in hb.SENSORS:
            self.assertIsNotNone(self.mqtt.last(f"homeassistant/sensor/hypon_{SN}/{key}/config"), key)
        self.assertIsNotNone(self.mqtt.last(f"homeassistant/binary_sensor/hypon_{SN}/connection/config"))
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), len(hb.SENSORS) + 1)
        retained = [r for t, _p, r in self.mqtt.published if t.startswith("homeassistant/")]
        self.assertTrue(all(retained))

    async def test_unique_and_object_ids_are_stable(self):
        # Changing these would orphan existing entities, history and dashboards in Home Assistant.
        await self.frame()
        for key, name, unit, dev_class, state_class in hb.SENSORS:
            cfg = self.config_of("sensor", key)
            self.assertEqual(cfg["unique_id"], f"hypon_{SN}_{key}")
            self.assertEqual(cfg["object_id"], f"hypon_{key}")
            self.assertEqual(cfg["name"], name)
            self.assertEqual(cfg["unit_of_measurement"], unit)
            self.assertEqual(cfg["device_class"], dev_class)
            self.assertEqual(cfg["state_class"], state_class)
            self.assertEqual(cfg["state_topic"], f"hypon/{SN}/state")
            self.assertEqual(cfg["value_template"], "{{ value_json.%s }}" % key)
        conn = self.config_of("binary_sensor", "connection")
        self.assertEqual(conn["unique_id"], f"hypon_{SN}_connection")
        self.assertEqual(conn["device_class"], "connectivity")
        self.assertEqual(conn["entity_category"], "diagnostic")
        self.assertEqual((conn["payload_on"], conn["payload_off"]), ("ON", "OFF"))
        self.assertEqual(conn["state_topic"], f"hypon/{SN}/connection")

    async def test_entities_depend_on_the_bridge_not_on_the_inverter(self):
        await self.frame()
        configs = [self.config_of("sensor", k) for k, *_ in hb.SENSORS]
        configs.append(self.config_of("binary_sensor", "connection"))
        for cfg in configs:
            self.assertEqual(cfg["availability_topic"], "hypon/bridge/status")
            self.assertNotIn("availability", cfg)

    async def test_all_entities_share_one_device(self):
        await self.frame()
        devices = {json.dumps(self.config_of("sensor", k)["device"], sort_keys=True) for k, *_ in hb.SENSORS}
        devices.add(json.dumps(self.config_of("binary_sensor", "connection")["device"], sort_keys=True))
        self.assertEqual(len(devices), 1)
        device = json.loads(devices.pop())
        self.assertEqual(device["identifiers"], [f"hypon_{SN}"])
        self.assertEqual(device["manufacturer"], "Hypontech")
        self.assertEqual(device["serial_number"], SN)
        self.assertEqual(device["model"], "HMS-800W-C")  # default until `invinfo` says otherwise
        self.assertNotIn("sw_version", device)

    async def test_legacy_availability_topic_is_cleared(self):
        await self.frame()
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/availability"), ("", True))
        self.assertNotIn("offline", [p for t, p, _r in self.mqtt.published if t.endswith("/availability")])

    async def test_discovery_is_published_once_per_local_connection(self):
        await self.frame()
        first = self.mqtt.count(prefix="homeassistant/")
        await self.frame(power=2)
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), first)  # no repeat
        await self.bridge._on_local_connect()          # broker (re)connected
        await self.frame(power=3)
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), 2 * first)

    async def test_invinfo_enriches_the_device_and_triggers_a_new_announce(self):
        await self.frame()
        before = self.mqtt.count(prefix="homeassistant/")
        await self.bridge.on_device_publish("invinfo", support.build_invinfo(), "test")
        await self.frame()
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), 2 * before)
        device = self.config_of("sensor", "power")["device"]
        self.assertEqual(device["sw_version"], "H123-45678-90")
        self.assertEqual(device["model"], "HMS-800W-2T")

    async def test_custom_base_topic_and_discovery_prefix(self):
        cfg = make_config(HYPON_BASE_TOPIC="solar", HA_DISCOVERY_PREFIX="ha", HYPON_PUBLISH_RAW="off")
        self.assertEqual(cfg.status_topic, "solar/bridge/status")
        bridge = hb.Bridge(cfg, self.mqtt)
        await bridge.on_device_publish("invmonitor", support.build_monitor(), "test")
        self.assertIsNotNone(self.mqtt.last(f"solar/{SN}/state"))
        self.assertIsNotNone(self.mqtt.last(f"ha/sensor/hypon_{SN}/power/config"))
        self.assertEqual(json.loads(self.mqtt.last(f"ha/sensor/hypon_{SN}/power/config")[0])
                         ["availability_topic"], "solar/bridge/status")


class OfflineHandlingTests(BridgeTestCase):
    async def test_reconnect_within_grace_never_declares_offline(self):
        await self.frame(power=5, e_total=900.0)
        await self.bridge.on_device_gone()
        await asyncio.sleep(0.01)
        await self.frame(power=6, e_total=900.0)       # collector is back (dusk flapping)
        await asyncio.sleep(self.cfg.offline_grace_s * 4)
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))
        self.assertNotIn("OFF", [p for t, p, _r in self.mqtt.published if t.endswith("/connection")])
        self.assertEqual(self.state()["power"], 6)
        self.assertEqual(self.bridge.offline_tasks, {})

    async def test_silence_zeroes_production_and_keeps_totals(self):
        await self.frame(power=5, pv1=(30.0, 0.2), pv2=(28.0, 0.1), grid_v=229.5, grid_a=0.1,
                         freq=50.1, temp=18.5, e_total=933.99, e_pv1=500.0, e_pv2=433.99)
        await self.bridge.on_device_gone()
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/connection") == ("OFF", True))
        state = self.state()
        for key in hb.ZERO_WHEN_OFFLINE:
            self.assertEqual(state[key], 0, key)
        self.assertAlmostEqual(state["energy_total"], 933.99, places=2)
        self.assertAlmostEqual(state["energy_pv1"], 500.0, places=2)
        self.assertEqual(state["temperature"], 18.5)
        self.assertEqual(state["grid_voltage"], 229.5)
        self.assertEqual(state["grid_frequency"], 50.1)
        self.assertEqual(state["device_time"], "2026-11-08 17:00:00")
        self.assertTrue(self.mqtt.last(f"hypon/{SN}/state")[1])  # retained, survives HA restarts
        self.assertNotIn(SN, self.bridge.connected)

    async def test_sensors_never_become_unavailable_through_the_inverter(self):
        await self.frame()
        await self.bridge.on_device_gone()
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/connection") == ("OFF", True))
        published_availability = [(t, p) for t, p, _r in self.mqtt.published if "availability" in t]
        self.assertEqual(published_availability, [(f"hypon/{SN}/availability", "")])
        self.assertFalse([t for t in self.mqtt.topics() if t == "hypon/bridge/status"])

    async def test_next_morning_frame_restores_online(self):
        await self.frame(power=5)
        await self.bridge.on_device_gone()
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/connection") == ("OFF", True))
        await self.frame(power=42)
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))
        self.assertEqual(self.state()["power"], 42)
        self.assertIn(SN, self.bridge.connected)

    async def test_disconnect_of_an_unknown_inverter_publishes_nothing(self):
        await self.bridge.on_device_gone()
        await asyncio.sleep(self.cfg.offline_grace_s * 3)
        self.assertEqual(self.mqtt.published, [])

    async def test_repeated_disconnects_keep_a_single_pending_task(self):
        await self.frame()
        await self.bridge.on_device_gone()
        await self.bridge.on_device_gone()
        self.assertEqual(len(self.bridge.offline_tasks), 1)
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/connection") == ("OFF", True))
        self.assertEqual(self.mqtt.count(f"hypon/{SN}/connection"), 2)  # ON then a single OFF

    async def test_zero_grace_goes_offline_immediately(self):
        self.cfg.offline_grace_s = 0
        await self.frame()
        await self.bridge.on_device_gone()
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/connection") == ("OFF", True))

    async def test_offline_without_any_known_state_only_flips_the_connection(self):
        self.bridge.connected.add(SN)
        await self.bridge._mark_offline(SN)
        self.assertEqual(self.mqtt.published, [(f"hypon/{SN}/connection", "OFF", True)])

    async def test_a_frame_arriving_during_the_wait_wins_even_if_not_cancelled(self):
        await self.frame(power=9)
        # simulate the race: a frame is seen after the disconnect timestamp, task not cancelled
        self.bridge.last_seen[SN] = time.monotonic() + 1
        await self.bridge._offline_after_grace(SN, since=time.monotonic())
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))
        self.assertIn(SN, self.bridge.connected)


class OtherTopicsTests(BridgeTestCase):
    async def test_buffered_frames_are_published_unretained_and_do_not_touch_state(self):
        await self.bridge.on_device_publish("offinvmonitor", support.build_monitor(power=77), "test")
        payload, retain = self.mqtt.last(f"hypon/{SN}/offline")
        self.assertFalse(retain)
        self.assertEqual(json.loads(payload)["power"], 77)
        self.assertIsNone(self.mqtt.last(f"hypon/{SN}/state"))
        self.assertEqual(self.bridge.connected, set())
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), 0)

    async def test_undecodable_frame_is_swallowed(self):
        await self.bridge.on_device_publish("invmonitor", b"too short", "test")
        await self.bridge.on_device_publish("offinvmonitor", b"", "test")
        await self.bridge.on_device_publish("invinfo", b"", "test")  # empty -> no crash
        self.assertIsNone(self.mqtt.last(f"hypon/{SN}/state"))

    async def test_unknown_topic_is_ignored_without_raw_publishing(self):
        await self.bridge.on_device_publish("something/else", b"\x01\x02", "test")
        self.assertEqual(self.mqtt.published, [])

    async def test_raw_publishing_when_enabled(self):
        cfg = make_config(HYPON_PUBLISH_RAW="on")
        bridge = hb.Bridge(cfg, self.mqtt)
        await bridge.on_device_publish("something/else", b"\x01\xab", "test")
        self.assertEqual(self.mqtt.published, [("hypon/raw/something/else", "01ab", False)])

    async def test_raw_publishing_defaults_to_on_and_can_be_disabled(self):
        self.assertTrue(make_config().publish_raw)
        for value in ("off", "0", "false", "no", "OFF"):
            self.assertFalse(make_config(HYPON_PUBLISH_RAW=value).publish_raw, value)

    async def test_tap_dispatches_publish_connect_and_subscribe(self):
        pkt = support.publish_packet("invmonitor", support.build_monitor(power=3))
        _fb, body = hb.MqttStreamParser().feed(pkt)[0]
        await self.bridge.tap(pkt[0], body, "test")
        self.assertEqual(self.state()["power"], 3)
        connect = support.connect_packet()
        _fb, body = hb.MqttStreamParser().feed(connect)[0]
        await self.bridge.tap(connect[0], body, "test")           # logs only
        await self.bridge.tap(0x82, b"\x00\x01", "test")          # SUBSCRIBE: logs only

    async def test_cloud_publishes_are_mirrored_as_raw_when_enabled(self):
        cfg = make_config(HYPON_PUBLISH_RAW="on")
        bridge = hb.Bridge(cfg, self.mqtt)
        pkt = support.publish_packet("cmd", b"\xde\xad")
        _fb, body = hb.MqttStreamParser().feed(pkt)[0]
        await bridge.tap_cloud(pkt[0], body)
        await bridge.tap_cloud(0x20, b"\x00\x00")                 # CONNACK: logs only
        self.assertEqual(self.mqtt.published, [("hypon/raw/cloud/cmd", "dead", False)])


class LocalReplyTests(unittest.TestCase):
    """When the cloud is unreachable the bridge answers the collector itself."""

    reply = staticmethod(hb.Bridge._local_reply)

    def test_connect_is_accepted(self):
        self.assertEqual(self.reply(0x10, b""), b"\x20\x02\x00\x00")

    def test_publish_qos0_gets_no_reply(self):
        self.assertIsNone(self.reply(0x30, hb.enc_str("t") + b"x"))

    def test_publish_qos1_is_acked_with_its_packet_id(self):
        body = hb.enc_str("t") + struct.pack(">H", 0x0102) + b"x"
        self.assertEqual(self.reply(0x32, body), b"\x40\x02\x01\x02")

    def test_publish_qos2_gets_pubrec(self):
        body = hb.enc_str("t") + struct.pack(">H", 7) + b"x"
        self.assertEqual(self.reply(0x34, body), b"\x50\x02\x00\x07")

    def test_pubrel_gets_pubcomp(self):
        self.assertEqual(self.reply(0x62, b"\x00\x09"), b"\x70\x02\x00\x09")

    def test_subscribe_grants_at_most_qos1_per_topic(self):
        body = struct.pack(">H", 5) + hb.enc_str("a") + b"\x00" + hb.enc_str("bc") + b"\x02"
        self.assertEqual(self.reply(0x82, body), b"\x90\x04\x00\x05\x00\x01")

    def test_pingreq_gets_pingresp(self):
        self.assertEqual(self.reply(0xC0, b""), b"\xd0\x00")

    def test_disconnect_closes(self):
        self.assertEqual(self.reply(0xE0, b""), "close")

    def test_other_packets_get_no_reply(self):
        self.assertIsNone(self.reply(0x40, b"\x00\x01"))


if __name__ == "__main__":
    unittest.main()
