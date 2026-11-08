"""End-to-end connection handling: cloud relay, local fallback, hand-back to the cloud."""
import asyncio
import json
import struct
import unittest

import support
from support import SN, hb, make_config, wait_until


class RelayTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = support.RecordingMqtt()
        self.cloud = None
        self.listener = None
        self.clients = []

    async def asyncTearDown(self):
        for writer in self.clients:
            writer.close()
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        if self.cloud:
            await self.cloud.close()
        if hasattr(self, "bridge"):
            for task in list(self.bridge.offline_tasks.values()):
                task.cancel()

    async def start_cloud(self, port=None):
        async def answer(data, writer):
            if data[0] >> 4 == 1:
                writer.write(support.CONNACK_OK)
                await writer.drain()
        self.cloud = support.FakeServer(answer)
        if port is None:
            await self.cloud.start()
        else:
            self.cloud._server = await asyncio.start_server(self.cloud._client, "127.0.0.1", port)
        return self.cloud

    async def start_bridge(self, upstream_port, **env):
        env = {"HYPON_UPSTREAM_HOST": "127.0.0.1", "HYPON_UPSTREAM_PORT": str(upstream_port),
               "HYPON_PUBLISH_RAW": "off", **env}
        self.cfg = make_config(**env)
        self.cfg.offline_grace_s = 0.05
        self.bridge = hb.Bridge(self.cfg, self.mqtt)
        self.listener = await asyncio.start_server(self.bridge.handle_client, "127.0.0.1", 0)
        return self.listener.sockets[0].getsockname()[1]

    async def collector(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.clients.append(writer)
        return reader, writer

    @staticmethod
    async def read_exactly(reader, n):
        return await asyncio.wait_for(reader.readexactly(n), 3)

    def state(self):
        return json.loads(self.mqtt.last(f"hypon/{SN}/state")[0])


class CloudRelayTests(RelayTestCase):
    async def test_traffic_is_relayed_byte_for_byte_and_decoded(self):
        cloud = await self.start_cloud()
        port = await self.start_bridge(cloud.port)
        reader, writer = await self.collector(port)
        stream = (support.connect_packet()
                  + support.publish_packet("invmonitor", support.build_monitor(power=250), qos=1, pid=3)
                  + support.publish_packet("invmonitor", support.build_monitor(power=260), qos=1, pid=4))
        writer.write(stream)
        await wait_until(lambda: len(cloud.received) == len(stream))
        self.assertEqual(bytes(cloud.received), stream)                  # untouched
        self.assertEqual(await self.read_exactly(reader, 4), support.CONNACK_OK)  # cloud -> collector
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/state") and self.state()["power"] == 260)
        self.assertEqual(self.mqtt.last(f"hypon/{SN}/connection"), ("ON", True))

    async def test_cloud_to_collector_data_is_forwarded_untouched(self):
        cloud = await self.start_cloud()
        port = await self.start_bridge(cloud.port)
        reader, writer = await self.collector(port)
        writer.write(support.connect_packet())
        await wait_until(lambda: cloud.connections == 1 and cloud.received)
        self.assertEqual(await self.read_exactly(reader, 4), support.CONNACK_OK)  # cloud's answer
        command = support.publish_packet("down/cmd", b"\x01\x02\x03")
        cloud.writers[0].write(command)
        self.assertEqual(await self.read_exactly(reader, len(command)), command)

    async def test_garbage_does_not_break_the_relay(self):
        cloud = await self.start_cloud()
        port = await self.start_bridge(cloud.port)
        _reader, writer = await self.collector(port)
        garbage = b"\xff\xff\xff\xff\xff\xff\xff"
        writer.write(garbage)
        await wait_until(lambda: bytes(cloud.received) == garbage)       # still forwarded
        valid = support.publish_packet("invmonitor", support.build_monitor(power=11))
        writer.write(valid)
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/state") is not None)
        self.assertEqual(self.state()["power"], 11)                      # parser recovered

    async def test_collector_disconnect_goes_offline_after_the_grace_period(self):
        cloud = await self.start_cloud()
        port = await self.start_bridge(cloud.port)
        _reader, writer = await self.collector(port)
        writer.write(support.publish_packet("invmonitor", support.build_monitor(power=9, e_total=12.5)))
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/state") is not None)
        writer.close()
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/connection") == ("OFF", True))
        self.assertEqual(self.state()["power"], 0)
        self.assertAlmostEqual(self.state()["energy_total"], 12.5, places=2)

    async def test_cloud_closing_the_connection_closes_the_collector_side(self):
        cloud = await self.start_cloud()
        port = await self.start_bridge(cloud.port)
        reader, writer = await self.collector(port)
        writer.write(support.connect_packet())
        await wait_until(lambda: cloud.writers)
        cloud.writers[0].close()
        self.assertEqual(await asyncio.wait_for(reader.read(100), 3), support.CONNACK_OK)
        self.assertEqual(await asyncio.wait_for(reader.read(100), 3), b"")


class LocalFallbackTests(RelayTestCase):
    async def test_cloud_unreachable_bridge_answers_the_collector_itself(self):
        port = await self.start_bridge(support.free_port())              # nothing listens upstream
        reader, writer = await self.collector(port)
        writer.write(support.connect_packet())
        self.assertEqual(await self.read_exactly(reader, 4), support.CONNACK_OK)
        writer.write(support.publish_packet("invmonitor", support.build_monitor(power=321),
                                            qos=1, pid=0x0A0B))
        self.assertEqual(await self.read_exactly(reader, 4), b"\x40\x02\x0a\x0b")   # PUBACK
        await wait_until(lambda: self.mqtt.last(f"hypon/{SN}/state") is not None)
        self.assertEqual(self.state()["power"], 321)
        writer.write(hb.mqtt_packet(0xC0))                                          # PINGREQ
        self.assertEqual(await self.read_exactly(reader, 2), b"\xd0\x00")
        writer.write(hb.mqtt_packet(0x82, struct.pack(">H", 1) + hb.enc_str("down/#") + b"\x00"))
        self.assertEqual(await self.read_exactly(reader, 5), b"\x90\x03\x00\x01\x00")
        writer.write(hb.mqtt_packet(0xE0))                                          # DISCONNECT
        self.assertEqual(await asyncio.wait_for(reader.read(100), 3), b"")

    async def test_relay_disabled_never_contacts_the_cloud(self):
        cloud = await self.start_cloud()
        port = await self.start_bridge(cloud.port, HYPON_UPSTREAM="off")
        reader, writer = await self.collector(port)
        writer.write(support.connect_packet())
        self.assertEqual(await self.read_exactly(reader, 4), support.CONNACK_OK)
        self.assertEqual(cloud.connections, 0)

    async def test_collector_is_handed_back_to_the_cloud_once_it_is_reachable_again(self):
        upstream_port = support.free_port()
        port = await self.start_bridge(upstream_port, HYPON_CLOUD_RETRY_S="1")
        self.cfg.cloud_retry_s = 0.05
        reader, writer = await self.collector(port)
        writer.write(support.connect_packet())
        self.assertEqual(await self.read_exactly(reader, 4), support.CONNACK_OK)   # served locally
        await self.start_cloud(upstream_port)                                      # cloud is back
        # the bridge drops the collector so that it reconnects through the relay
        self.assertEqual(await asyncio.wait_for(reader.read(100), 3), b"")
        reader2, writer2 = await self.collector(port)
        writer2.write(support.connect_packet())
        # (the watchdog's own reachability probe also connects to the cloud, but sends nothing)
        await wait_until(lambda: bytes(self.cloud.received) == support.connect_packet())
        self.assertEqual(await self.read_exactly(reader2, 4), support.CONNACK_OK)  # from the cloud


if __name__ == "__main__":
    unittest.main()
