"""Bridge logic: Home Assistant discovery, availability statuses, persistence, topic handling."""
import asyncio
import json
import struct
import unittest
from datetime import datetime, timezone
from unittest import mock

import support
from support import SN, hb, make_config

import hypon_status as hs
from hypon_store import StateStore

START = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
GRACE = 300
IP, NEW_IP, MAC = "192.168.1.87", "192.168.1.140", "aa:bb:cc:dd:ee:ff"
ABSENT = hs.Link(mac=MAC, checked=True)                 # the network was searched: nothing there
ALIVE = hs.Link(mac=MAC, alive=True, checked=True)      # it answers the network
MOVED = hs.Link(mac=MAC, moved_to=NEW_IP, checked=True)  # it is at another address
PRODUCTION = dict(power=87, pv1=(30.0, 2.9), pv2=(28.0, 2.9), grid_v=229.5, grid_a=0.4,
                  freq=50.1, temp=18.5, e_total=933.99, e_pv1=500.0, e_pv2=433.99)
DEFAULT = object()


class BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mqtt = support.RecordingMqtt()
        self.cfg = make_config(HYPON_PUBLISH_RAW="off")
        self.cfg.offline_grace_s = GRACE
        self.clock = support.FakeClock(START)
        self.links = {IP: ABSENT}        # by default the network was searched and shows nothing
        self.store = StateStore()
        self.bridge = self.make_bridge()
        # The add-on has been running for a while: the start-up hold is not what is tested here.
        self.clock.advance(hb.STARTUP_HOLD_S + 1)

    def make_bridge(self, mqtt=None, store=None, cfg=None):
        return hb.Bridge(cfg or self.cfg, mqtt or self.mqtt, store or self.store, self.links,
                         mono=self.clock.mono, wall=self.clock.wall)

    async def frame(self, sn=SN, session=DEFAULT, **kwargs):
        """A live frame, by default coming from the inverter at IP (`session=None`: unknown)."""
        if session is DEFAULT:
            session = hb.Session(IP)
        await self.bridge.on_device_publish(
            "invmonitor", support.build_monitor(sn=sn, **kwargs), "test", session)

    async def silence(self, seconds):
        """Let `seconds` go by without any frame, then run the watchdog once."""
        self.clock.advance(seconds)
        await self.bridge.refresh()

    def state(self, sn=SN):
        return json.loads(self.mqtt.last(f"hypon/{sn}/state")[0])

    def status(self, sn=SN):
        return self.mqtt.last(f"hypon/{sn}/status")[0]

    def availability(self, sn=SN):
        return self.mqtt.last(f"hypon/{sn}/availability")[0]

    def network(self, sn=SN):
        return json.loads(self.mqtt.last(f"hypon/{sn}/network")[0])

    def config_of(self, kind, key, sn=SN):
        return json.loads(self.mqtt.last(f"homeassistant/{kind}/hypon_{sn}/{key}/config")[0])


DIAGNOSTICS = (("binary_sensor", "connection"), ("sensor", "status"), ("sensor", "last_seen"),
               ("sensor", "ip_address"), ("sensor", "configured_ip"), ("sensor", "mac_address"))


class DiscoveryTests(BridgeTestCase):
    async def test_first_frame_publishes_retained_state_status_and_connection(self):
        await self.frame(power=123, e_total=50.5)
        payload, retain = self.mqtt.last(f"hypon/{SN}/state")
        self.assertTrue(retain)
        self.assertEqual(json.loads(payload)["power"], 123)
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/status"), ("online", True))
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/availability"), ("online", True))

    async def test_one_discovery_config_per_sensor_plus_the_diagnostics(self):
        await self.frame()
        for key, *_ in hb.SENSORS:
            self.assertIsNotNone(self.mqtt.last(f"homeassistant/sensor/hypon_{SN}/{key}/config"), key)
        for kind, key in DIAGNOSTICS:
            self.assertIsNotNone(self.mqtt.last(f"homeassistant/{kind}/hypon_{SN}/{key}/config"), key)
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), len(hb.SENSORS) + len(DIAGNOSTICS))
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
        self.assertEqual((conn["payload_on"], conn["payload_off"]), ("ON", "OFF"))
        self.assertEqual(conn["state_topic"], f"hypon/{SN}/connection")
        for kind, key in DIAGNOSTICS:
            cfg = self.config_of(kind, key)
            self.assertEqual(cfg["unique_id"], f"hypon_{SN}_{key}")
            self.assertEqual(cfg["object_id"], f"hypon_{key}")

    async def test_measurements_need_the_bridge_and_a_trustworthy_inverter(self):
        await self.frame()
        for key, *_ in hb.SENSORS:
            cfg = self.config_of("sensor", key)
            self.assertEqual(cfg["availability"], [{"topic": "hypon/bridge/status"},
                                                   {"topic": f"hypon/{SN}/availability"}])
            self.assertEqual(cfg["availability_mode"], "all")
            self.assertNotIn("availability_topic", cfg)

    async def test_diagnostics_only_need_the_bridge_so_they_can_explain_a_problem(self):
        await self.frame()
        for kind, key in DIAGNOSTICS:
            cfg = self.config_of(kind, key)
            self.assertEqual(cfg["availability_topic"], "hypon/bridge/status", key)
            self.assertNotIn("availability", cfg)
            self.assertEqual(cfg["entity_category"], "diagnostic", key)

    async def test_status_is_an_enum_listing_every_status(self):
        await self.frame()
        cfg = self.config_of("sensor", "status")
        self.assertEqual(cfg["device_class"], "enum")
        self.assertEqual(cfg["options"], ["online", "sleeping", "unreachable", "ip_changed"])
        self.assertEqual(cfg["state_topic"], f"hypon/{SN}/status")

    async def test_last_seen_is_a_timestamp_and_the_network_entities_share_one_topic(self):
        await self.frame()
        last_seen = self.config_of("sensor", "last_seen")
        self.assertEqual(last_seen["device_class"], "timestamp")
        self.assertEqual(last_seen["state_topic"], f"hypon/{SN}/state")
        ip, configured, mac = (self.config_of("sensor", key)
                               for key in ("ip_address", "configured_ip", "mac_address"))
        self.assertEqual({c["state_topic"] for c in (ip, configured, mac)}, {f"hypon/{SN}/network"})
        self.assertEqual(ip["value_template"], "{{ value_json.ip or none }}")
        self.assertEqual(configured["value_template"], "{{ value_json.configured_ip or none }}")
        self.assertNotIn("enabled_by_default", ip)
        self.assertNotIn("enabled_by_default", configured)   # meant for automations: on by default
        self.assertIs(mac["enabled_by_default"], False)      # useful when needed, noise otherwise

    async def test_all_entities_share_one_device(self):
        await self.frame()
        configs = [self.config_of("sensor", k) for k, *_ in hb.SENSORS]
        configs += [self.config_of(kind, key) for kind, key in DIAGNOSTICS]
        devices = {json.dumps(cfg["device"], sort_keys=True) for cfg in configs}
        self.assertEqual(len(devices), 1)
        device = json.loads(devices.pop())
        self.assertEqual(device["identifiers"], [f"hypon_{SN}"])
        self.assertEqual(device["manufacturer"], "Hypontech")
        self.assertEqual(device["serial_number"], SN)
        self.assertEqual(device["model"], "HMS-800W-C")  # default until `invinfo` says otherwise
        self.assertNotIn("sw_version", device)

    async def test_discovery_is_published_once_per_local_connection(self):
        await self.frame()
        first = self.mqtt.count(prefix="homeassistant/")
        await self.frame(power=2)
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), first)  # no repeat
        await self.bridge._on_local_connect()          # broker (re)connected
        await self.frame(power=3)
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), 2 * first)

    async def test_everything_is_published_again_when_the_broker_reconnects(self):
        await self.frame(power=77)
        self.mqtt.published.clear()
        await self.bridge._on_local_connect()
        for leaf in ("state", "status", "connection", "availability", "network"):
            self.assertIsNotNone(self.mqtt.last(f"hypon/{SN}/{leaf}"), leaf)
        self.assertEqual(self.state()["power"], 77)

    async def test_invinfo_enriches_the_device_and_triggers_a_new_announce(self):
        await self.frame()
        before = self.mqtt.count(prefix="homeassistant/")
        await self.bridge.on_device_publish("invinfo", support.build_invinfo(), "test")
        await self.frame()
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"), 2 * before)
        device = self.config_of("sensor", "power")["device"]
        self.assertEqual(device["sw_version"], "H123-45678-90")
        self.assertEqual(device["model"], "HMS-800W-2T")

    async def test_invinfo_alone_announces_nothing(self):
        await self.bridge.on_device_publish("invinfo", support.build_invinfo(), "test")
        self.assertEqual(self.mqtt.published, [])

    async def test_custom_base_topic_and_discovery_prefix(self):
        cfg = make_config(HYPON_BASE_TOPIC="solar", HA_DISCOVERY_PREFIX="ha", HYPON_PUBLISH_RAW="off")
        self.assertEqual(cfg.status_topic, "solar/bridge/status")
        bridge = hb.Bridge(cfg, self.mqtt)
        await bridge.on_device_publish("invmonitor", support.build_monitor(), "test")
        self.assertIsNotNone(self.mqtt.last(f"solar/{SN}/state"))
        self.assertIsNotNone(self.mqtt.last(f"solar/{SN}/status"))
        power = json.loads(self.mqtt.last(f"ha/sensor/hypon_{SN}/power/config")[0])
        self.assertEqual(power["availability"][0], {"topic": "solar/bridge/status"})
        self.assertEqual(power["availability"][1], {"topic": f"solar/{SN}/availability"})


class OffTests(BridgeTestCase):
    """Silent and nowhere on the network: no light on the panels. Its production is really 0 W,
    whether it is night or a storm, and Home Assistant must say so rather than "unavailable"."""

    async def test_silence_with_the_inverter_absent_reads_zero_watts_and_stays_available(self):
        await self.frame(**PRODUCTION)
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(self.availability(), "online")          # never "unavailable" for this
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("OFF", True))
        state = self.state()
        for key in hb.PRODUCTION_KEYS:
            self.assertEqual(state[key], 0, key)
        self.assertTrue(self.mqtt.last(f"hypon/{SN}/state")[1])  # retained, survives HA restarts

    async def test_it_does_not_matter_whether_it_is_day_or_night(self):
        # The same silence at noon (a storm) and at midnight gives the same answer: 0 W.
        for hour in (0, 12):
            with self.subTest(hour=hour):
                self.clock = support.FakeClock(datetime(2026, 6, 21, hour, 0, tzinfo=timezone.utc))
                self.mqtt = support.RecordingMqtt()
                self.bridge = self.make_bridge()
                self.clock.advance(hb.STARTUP_HOLD_S + 1)
                await self.frame(**PRODUCTION)
                await self.silence(GRACE + 1)
                self.assertEqual((self.status(), self.availability(), self.state()["power"]),
                                 ("sleeping", "online", 0))

    async def test_totals_and_the_other_last_values_are_kept(self):
        await self.frame(**PRODUCTION)
        await self.silence(GRACE + 1)
        state = self.state()
        self.assertAlmostEqual(state["energy_total"], 933.99, places=2)
        self.assertAlmostEqual(state["energy_pv1"], 500.0, places=2)
        self.assertAlmostEqual(state["energy_pv2"], 433.99, places=2)
        self.assertEqual(state["temperature"], 18.5)
        self.assertEqual(state["grid_voltage"], 229.5)
        self.assertEqual(state["grid_frequency"], 50.1)
        self.assertEqual(state["device_time"], "2026-11-08 17:00:00")

    async def test_the_last_frame_time_is_kept(self):
        await self.frame()
        seen = self.state()["last_seen"]
        await self.silence(GRACE + 1)
        self.assertEqual(self.state()["last_seen"], seen)

    async def test_the_collector_reconnecting_within_the_delay_changes_nothing(self):
        # Dusk flapping: the collector drops and comes back several times.
        await self.frame(power=5)
        for _ in range(4):
            await self.silence(GRACE - 10)
            await self.frame(power=6)
        self.assertEqual(self.mqtt.count(f"hypon/{SN}/status"), 1)          # online, once
        self.assertNotIn("OFF", [p for t, p, _r in self.mqtt.published if t.endswith("/connection")])
        self.assertEqual(self.state()["power"], 6)

    async def test_the_first_frame_next_morning_brings_it_back(self):
        await self.frame(power=5)
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "sleeping")
        await self.frame(power=42)
        self.assertEqual(self.status(), "online")
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))
        self.assertEqual(self.availability(), "online")
        self.assertEqual(self.state()["power"], 42)

    async def test_nothing_is_published_again_while_it_stays_off(self):
        await self.frame()
        await self.silence(GRACE + 1)
        published = len(self.mqtt.published)
        for _ in range(5):
            await self.silence(600)
        self.assertEqual(len(self.mqtt.published), published)

    async def test_zero_delay_declares_it_silent_at_once(self):
        self.cfg.offline_grace_s = 0
        await self.frame()
        await self.silence(1)
        self.assertEqual(self.status(), "sleeping")


class UnreachableTests(BridgeTestCase):
    """Powered and on the network, yet silent: the data cannot be trusted."""

    async def test_an_inverter_that_answers_the_network_but_not_us_is_unavailable(self):
        await self.frame(**PRODUCTION)
        self.links[IP] = ALIVE
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "unreachable")
        self.assertEqual(self.availability(), "offline")
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("OFF", True))

    async def test_the_last_production_is_never_left_standing(self):
        # The incident: 87 W shown for hours although nothing came in any more.
        await self.frame(**PRODUCTION)
        self.links[IP] = ALIVE
        await self.silence(GRACE + 1)
        state = self.state()
        for key in hb.PRODUCTION_KEYS:
            self.assertIsNone(state[key], key)    # unknown, neither 87 nor a fake 0
        self.assertAlmostEqual(state["energy_total"], 933.99, places=2)

    async def test_it_comes_back_with_the_next_frame(self):
        await self.frame(power=87)
        self.links[IP] = ALIVE
        await self.silence(GRACE + 1)
        await self.frame(power=120)
        self.assertEqual(self.status(), "online")
        self.assertEqual(self.availability(), "online")
        self.assertEqual(self.state()["power"], 120)

    async def test_when_the_network_cannot_be_looked_at_it_is_not_claimed_to_be_off(self):
        # No ARP monitor (or no known MAC): "not seen" would mean nothing, so no 0 W is invented.
        await self.frame(power=87)
        self.links[IP] = hs.Link(mac=MAC)         # checked=False
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "unreachable")

    async def test_an_inverter_with_an_unknown_address_is_never_taken_for_off(self):
        await self.frame(power=87, session=None)
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "unreachable")

    async def test_a_slow_reporter_is_given_more_time(self):
        for _ in range(4):                       # a frame every 400 s: threshold becomes 1200 s
            await self.frame(power=87)
            self.clock.advance(400)
        self.links[IP] = ALIVE
        await self.silence(700)                  # 1100 s of silence: past the 300 s grace, not 1200 s
        self.assertEqual(self.status(), "online")
        await self.silence(200)
        self.assertEqual(self.status(), "unreachable")


class FadedTests(BridgeTestCase):
    """The evening of 22 November: the production faded to 0 W, the inverter fell silent, and its
    Wi-Fi collector kept answering the network for hours. That is an inverter that ran out of
    light (0 W, entities available), not a lost one."""

    async def dusk(self, last_power, link):
        for power in (7, 5, 1, last_power):                      # the evening fade
            await self.frame(power=power, pv1=(23.0, 0.1))
            self.clock.advance(30)
        self.links[IP] = link
        await self.silence(GRACE + 1)

    async def test_a_collector_still_on_the_network_does_not_make_a_faded_inverter_unavailable(self):
        await self.dusk(0, ALIVE)
        self.assertEqual((self.status(), self.availability(), self.state()["power"]),
                         ("sleeping", "online", 0))

    async def test_nor_does_a_network_that_could_not_be_looked_at(self):
        await self.dusk(0, hs.Link(mac=MAC))                    # checked=False
        self.assertEqual((self.status(), self.availability()), ("sleeping", "online"))

    async def test_it_stays_that_way_hour_after_hour_whatever_the_collector_does(self):
        await self.dusk(0, ALIVE)
        for link in (ALIVE, ABSENT, hs.Link(mac=MAC), ALIVE, hs.Link(mac=MAC, alive=True), ABSENT):
            self.links[IP] = link
            await self.silence(1800)
            self.assertEqual((self.status(), self.availability()), ("sleeping", "online"))
        self.assertEqual(self.mqtt.count(f"hypon/{SN}/status"), 2)       # online, then sleeping, once

    async def test_the_production_must_really_have_faded(self):
        await self.dusk(hs.FADED_POWER_W, ALIVE)                  # at the limit: still faded
        self.assertEqual(self.status(), "sleeping")
        await self.frame(power=hs.FADED_POWER_W + 1)              # clearly producing, then it vanishes
        self.links[IP] = ALIVE
        await self.silence(GRACE + 1)
        self.assertEqual((self.status(), self.availability()), ("unreachable", "offline"))

    async def test_only_the_last_frame_counts(self):
        await self.frame(power=87)
        self.clock.advance(30)
        await self.frame(power=3)                                 # it had faded when it went silent
        self.links[IP] = ALIVE
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "sleeping")

    async def test_an_address_change_is_still_reported_whatever_the_production(self):
        await self.dusk(0, MOVED)
        self.assertEqual((self.status(), self.availability()), ("ip_changed", "offline"))

    async def test_the_last_production_survives_a_restart(self):
        self.store = StateStore(self.enterContext(support.temp_dir()))
        self.bridge = self.make_bridge()
        await self.dusk(0, ALIVE)
        self.store.flush(force=True)
        self.mqtt = support.RecordingMqtt()
        self.bridge = self.make_bridge(store=StateStore.open(self.store.path.rsplit("/", 1)[0]))
        await self.bridge._on_local_connect()
        await self.silence(GRACE + 1)
        self.assertEqual((self.status(), self.state()["power"]), ("sleeping", 0))


class IpChangedTests(BridgeTestCase):
    async def silent_inverter(self):
        await self.frame()
        self.links[IP] = MOVED
        await self.silence(GRACE + 1)

    async def test_an_inverter_seen_under_another_ip_is_reported_as_such(self):
        await self.silent_inverter()
        self.assertEqual(self.status(), "ip_changed")
        self.assertEqual(self.availability(), "offline")
        self.assertIsNone(self.state()["power"])

    async def test_the_new_address_and_the_mac_are_published(self):
        await self.silent_inverter()
        self.assertEqual(self.network(), {"ip": NEW_IP, "configured_ip": IP, "mac": MAC})

    async def test_the_warning_tells_what_to_do(self):
        await self.frame()
        self.links[IP] = MOVED
        with support.logging_enabled(), self.assertLogs("hypon", "WARNING") as logs:
            await self.silence(GRACE + 1)
        message = logs.records[0].getMessage()
        self.assertIn(NEW_IP, message)
        self.assertIn("inverter_ips", message)

    async def test_it_goes_back_to_online_once_the_ip_is_fixed(self):
        await self.silent_inverter()
        self.links[IP] = ALIVE
        await self.frame()
        self.assertEqual(self.status(), "online")
        self.assertEqual(self.network()["ip"], IP)


class NetworkEntitiesTests(BridgeTestCase):
    async def test_the_ip_is_the_one_the_frames_come_from(self):
        await self.frame(session=hb.Session("192.168.1.99"))
        self.assertEqual(self.network()["ip"], "192.168.1.99")

    async def test_the_mac_comes_from_the_interceptor(self):
        await self.frame()
        self.assertEqual(self.network(), {"ip": IP, "configured_ip": IP, "mac": MAC})

    async def test_the_mac_is_unknown_until_the_interceptor_knows_it(self):
        self.links.clear()
        await self.frame()
        self.assertEqual(self.network(), {"ip": IP, "configured_ip": IP, "mac": None})

    async def test_the_configured_ip_is_the_address_the_add_on_expects_the_inverter_at(self):
        await self.frame(session=hb.Session("192.168.1.99"))
        self.links["192.168.1.99"] = ABSENT
        await self.frame(session=hb.Session("192.168.1.99"))
        self.assertEqual(self.network()["configured_ip"], "192.168.1.99")

    async def test_both_addresses_differ_exactly_when_the_inverter_has_moved(self):
        # What an automation compares: where it is vs where it is expected.
        await self.frame()
        self.assertEqual(self.network()["ip"], self.network()["configured_ip"])
        self.links[IP] = MOVED
        await self.silence(GRACE + 1)
        self.assertEqual((self.network()["ip"], self.network()["configured_ip"]), (NEW_IP, IP))
        self.links[IP] = ALIVE
        await self.frame()
        self.assertEqual(self.network()["ip"], self.network()["configured_ip"])

    async def test_an_address_removed_from_the_options_is_no_longer_called_configured(self):
        await self.frame(session=hb.Session("192.168.1.99"))     # (no longer in inverter_ips)
        self.assertEqual(self.network()["ip"], "192.168.1.99")   # still where it was last heard
        self.assertIsNone(self.network()["configured_ip"])

    async def test_network_is_only_published_when_it_changes(self):
        for _ in range(4):
            await self.frame()
        self.assertEqual(self.mqtt.count(f"hypon/{SN}/network"), 1)

    async def test_without_a_session_the_ip_stays_unknown(self):
        await self.frame(session=None)
        self.assertEqual(self.network(), {"ip": None, "configured_ip": None, "mac": None})


SN2, IP2 = "P08280A023B00178", "192.168.1.88"


class MultipleInvertersTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.links[IP2] = ABSENT

    async def frame2(self, **kwargs):
        await self.frame(SN2, session=hb.Session(IP2), **kwargs)

    async def test_each_inverter_has_its_own_topics_device_and_unique_ids(self):
        await self.frame(power=100)
        await self.frame2(power=200)
        self.assertEqual((self.state(SN)["power"], self.state(SN2)["power"]), (100, 200))
        for sn in (SN, SN2):
            cfg = self.config_of("sensor", "power", sn)
            self.assertEqual(cfg["unique_id"], f"hypon_{sn}_power")
            self.assertEqual(cfg["state_topic"], f"hypon/{sn}/state")
            self.assertEqual(cfg["device"]["identifiers"], [f"hypon_{sn}"])
        self.assertEqual(self.mqtt.count(prefix="homeassistant/"),
                         2 * (len(hb.SENSORS) + len(DIAGNOSTICS)))

    async def test_each_inverter_is_judged_on_its_own_silence_and_its_own_network_evidence(self):
        await self.frame(power=100)
        await self.frame2(power=200)
        self.links[IP] = ALIVE                   # the first one answers the network, the second not
        for _ in range(3):
            self.clock.advance(GRACE / 2)
            await self.frame2(power=210)         # only the second one keeps reporting
        await self.silence(GRACE / 2 + 1)
        self.assertEqual(self.status(SN), "unreachable")
        self.assertEqual(self.status(SN2), "online")
        self.assertEqual(self.state(SN2)["power"], 210)
        self.assertEqual(self.availability(SN2), "online")

    async def test_one_off_and_one_lost_are_told_apart(self):
        await self.frame(power=100)
        await self.frame2(power=200)
        self.links[IP] = MOVED                   # the first one is at another address
        await self.silence(GRACE + 1)            # the second one is nowhere: off
        self.assertEqual((self.status(SN), self.availability(SN)), ("ip_changed", "offline"))
        self.assertEqual((self.status(SN2), self.availability(SN2)), ("sleeping", "online"))
        self.assertEqual(self.state(SN2)["power"], 0)
        self.assertIsNone(self.state(SN)["power"])


class PersistenceTests(BridgeTestCase):
    """A restart must not make Home Assistant trust a value nobody confirms any more."""

    def setUp(self):
        super().setUp()
        self.directory = self.enterContext(support.temp_dir())
        self.store = StateStore(self.directory)
        self.bridge = self.make_bridge()

    def restart(self):
        """The add-on stops and starts again; the broker still holds the retained values."""
        self.store.flush(force=True)
        self.mqtt = support.RecordingMqtt()
        self.store = StateStore.open(self.directory)
        self.bridge = self.make_bridge()
        return self.bridge

    async def test_the_state_is_written_with_the_first_frame(self):
        await self.frame(power=87, e_total=12.5)
        self.assertTrue(self.store.flush(force=True))
        record = StateStore.open(self.directory).data["inverters"][SN]
        self.assertEqual(record["status"], "online")
        self.assertEqual(record["state"]["power"], 87)
        self.assertEqual(record["ip"], IP)

    async def test_a_restart_republishes_what_it_served_before(self):
        await self.frame(power=87, e_total=12.5)
        await self.restart()._on_local_connect()
        self.assertEqual(self.state()["power"], 87)
        self.assertEqual(self.status(), "online")
        self.assertIsNotNone(self.mqtt.last(f"homeassistant/sensor/hypon_{SN}/power/config"))

    async def test_the_incident_the_inverter_comes_back_under_another_ip(self):
        # Power cut at 87 W; the inverter comes back under another address, so no frame arrives.
        await self.frame(power=87)
        bridge = self.restart()
        await bridge._on_local_connect()
        self.assertEqual(self.state()["power"], 87)           # right after the restart: plausible
        self.links[IP] = MOVED                                 # the monitor finds it elsewhere
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "ip_changed")
        self.assertIsNone(self.state()["power"])
        self.assertEqual(self.availability(), "offline")

    async def test_the_incident_variant_the_inverter_is_simply_not_there(self):
        await self.frame(power=87)
        bridge = self.restart()
        await bridge._on_local_connect()
        await self.silence(GRACE + 1)                          # nothing anywhere on the network
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(self.state()["power"], 0)             # a known 0 W, never the stale 87
        self.assertEqual(self.availability(), "online")

    async def test_an_inverter_that_was_online_gets_a_full_window_to_report_again(self):
        await self.frame(power=87)
        self.clock.advance(3600)                  # the add-on was down for an hour
        bridge = self.restart()
        await bridge._on_local_connect()
        self.assertEqual(self.status(), "online")
        await self.silence(GRACE - 5)
        self.assertEqual(self.status(), "online")
        await self.silence(10)
        self.assertEqual(self.status(), "sleeping")

    async def test_a_sleeping_inverter_stays_sleeping_after_a_restart(self):
        await self.frame(power=5)
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "sleeping")
        self.clock.advance(7200)
        bridge = self.restart()
        await bridge._on_local_connect()
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(self.state()["power"], 0)
        self.assertEqual(self.availability(), "online")

    async def test_after_a_start_the_network_gets_time_to_be_looked_at_before_concluding(self):
        await self.frame(power=87)
        await self.silence(GRACE + 1)
        self.assertEqual(self.status(), "sleeping")
        self.links[IP] = hs.Link(mac=MAC)         # the monitor has not looked yet
        bridge = self.restart()
        await bridge._on_local_connect()
        await self.silence(hb.STARTUP_HOLD_S - 1)
        self.assertEqual(self.status(), "sleeping")   # held: no flicker to "unavailable"
        await self.silence(2)
        self.assertEqual(self.status(), "unreachable")  # still nothing known: it cannot be vouched for

    async def test_nothing_is_restored_without_a_usable_record(self):
        self.store.data["inverters"] = {
            "A": {"ip": None, "info": {}, "state": None, "last_seen": 1.0, "status": "online"},
            "B": {"ip": None, "info": {}, "state": {"power": 1}, "last_seen": None, "status": "online"},
            "C": {"ip": None, "info": {}, "state": {"power": 1}, "last_seen": 1.0, "status": "bogus"},
        }
        self.store.mark_dirty()
        self.assertEqual(self.restart().units, {})


class WatchdogTests(BridgeTestCase):
    async def test_the_watchdog_evaluates_on_its_own_and_survives_errors(self):
        await self.frame()
        calls = []
        real_refresh = self.bridge.refresh

        async def flaky_refresh():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            await real_refresh()

        self.bridge.refresh = flaky_refresh
        with mock.patch.object(hb, "WATCH_INTERVAL_S", 0.01):
            task = asyncio.create_task(self.bridge.watch())
            await support.wait_until(lambda: len(calls) >= 3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertGreaterEqual(len(calls), 3)

    async def test_a_status_change_is_written_to_disk_at_once(self):
        directory = self.enterContext(support.temp_dir())
        self.store = StateStore(directory)
        self.bridge = self.make_bridge()
        await self.frame(power=87)
        self.links[IP] = ALIVE
        await self.silence(GRACE + 1)
        self.assertEqual(StateStore.open(directory).data["inverters"][SN]["status"], "unreachable")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = make_config()
        self.assertEqual(cfg.offline_grace_s, 300)
        self.assertEqual(cfg.data_dir, "/data")
        self.assertEqual(make_config(HYPON_DATA_DIR="/somewhere").data_dir, "/somewhere")
        self.assertEqual(make_config(HYPON_OFFLINE_GRACE_S="60").offline_grace_s, 60)


class OtherTopicsTests(BridgeTestCase):
    async def test_buffered_frames_are_published_unretained_and_do_not_touch_state(self):
        await self.bridge.on_device_publish("offinvmonitor", support.build_monitor(power=77), "test")
        payload, retain = self.mqtt.last(f"hypon/{SN}/offline")
        self.assertFalse(retain)
        self.assertEqual(json.loads(payload)["power"], 77)
        self.assertIsNone(self.mqtt.last(f"hypon/{SN}/state"))
        self.assertEqual(self.bridge.units, {})
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
