"""LocalMqtt: the publisher that talks to Home Assistant's broker (Last Will, credentials...)."""
import asyncio
import unittest
from unittest import mock

import support
from support import hb, make_config, wait_until


class LocalMqttTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = await support.FakeBroker().start()
        self.addAsyncCleanup(self.broker.close)

    def local(self, **env):
        env = {"HA_MQTT_HOST": "127.0.0.1", "HA_MQTT_PORT": str(self.broker.port), **env}
        return hb.LocalMqtt(make_config(**env))

    async def start_session(self, local):
        """Run one session in the background and wait until the broker saw CONNECT."""
        task = asyncio.create_task(local._session())
        await wait_until(lambda: any(fb >> 4 == 1 for fb, _ in self.broker.packets))
        return task

    async def stop_session(self, task):
        for writer in self.broker.writers:   # broker drops the connection
            writer.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(task, 3)

    def connect_body(self):
        return next(body for fb, body in self.broker.packets if fb >> 4 == 1)

    async def test_connect_registers_a_retained_will_on_the_bridge_status_topic(self):
        local = self.local()
        task = await self.start_session(local)
        body = self.connect_body()
        flags = body[2 + 4 + 1]                     # after "MQTT" name and protocol level
        self.assertEqual(flags, 0x02 | 0x04 | 0x20)  # clean session + will + will retain, QoS 0
        self.assertIn(hb.enc_str("hypon/bridge/status") + hb.enc_str("offline"), body)
        self.assertEqual(hb.parse_connect(body), ("hypon-bridge", None))
        await self.stop_session(task)

    async def test_bridge_announces_itself_online_once_connected(self):
        local = self.local()
        task = await self.start_session(local)
        await wait_until(lambda: self.broker.publishes())
        self.assertEqual(self.broker.publishes()[0], ("hypon/bridge/status", "online", True))
        await self.stop_session(task)

    async def test_status_topic_follows_base_topic(self):
        local = self.local(HYPON_BASE_TOPIC="solar")
        task = await self.start_session(local)
        self.assertIn(hb.enc_str("solar/bridge/status"), self.connect_body())
        await wait_until(lambda: self.broker.publishes())
        self.assertEqual(self.broker.publishes()[0][0], "solar/bridge/status")
        await self.stop_session(task)

    async def test_credentials_follow_the_will_in_the_connect_payload(self):
        local = self.local(HA_MQTT_USER="mqtt-user", HA_MQTT_PASSWORD="mqtt-pass")
        task = await self.start_session(local)
        body = self.connect_body()
        self.assertEqual(body[2 + 4 + 1], 0xE6)     # + username + password flags
        self.assertEqual(hb.parse_connect(body), ("hypon-bridge", "mqtt-user"))
        self.assertTrue(body.endswith(hb.enc_str("mqtt-user") + hb.enc_str("mqtt-pass")))
        await self.stop_session(task)

    async def test_username_without_password(self):
        local = self.local(HA_MQTT_USER="mqtt-user")
        task = await self.start_session(local)
        body = self.connect_body()
        self.assertEqual(body[2 + 4 + 1], 0xA6)
        self.assertEqual(hb.parse_connect(body), ("hypon-bridge", "mqtt-user"))
        await self.stop_session(task)

    async def test_on_connect_callback_runs_after_connack(self):
        local = self.local()
        called = asyncio.Event()

        async def on_connect():
            called.set()
        local.on_connect = on_connect
        task = await self.start_session(local)
        await asyncio.wait_for(called.wait(), 3)
        await self.stop_session(task)

    async def test_refused_connection_raises(self):
        self.broker.connack = b"\x20\x02\x00\x05"   # not authorized
        local = self.local()
        with self.assertRaisesRegex(ConnectionError, "refused"):
            await asyncio.wait_for(local._session(), 3)
        self.assertIsNone(local.writer)

    async def test_publish_sets_the_retain_flag_and_accepts_str_and_bytes(self):
        local = self.local()
        task = await self.start_session(local)
        await wait_until(lambda: local.writer is not None)
        await local.publish("t/retained", "hello", retain=True)
        await local.publish("t/plain", b"\x01\x02")
        await wait_until(lambda: len(self.broker.publishes()) >= 3)
        packets = [(fb, hb.parse_publish(fb, body)) for fb, body in self.broker.packets if fb >> 4 == 3]
        self.assertEqual(packets[1][0], 0x31)
        self.assertEqual(packets[1][1][0::3], ("t/retained", b"hello"))
        self.assertEqual(packets[2][0], 0x30)
        self.assertEqual(packets[2][1][0::3], ("t/plain", b"\x01\x02"))
        await self.stop_session(task)

    async def test_publish_without_a_connection_is_a_noop(self):
        local = self.local()
        await local.publish("t", "x")   # writer is None: must not raise
        self.assertEqual(self.broker.packets, [])

    async def test_publish_failure_is_logged_not_raised(self):
        class BrokenWriter:
            def write(self, _data):
                raise ConnectionResetError("boom")
        local = self.local()
        local.writer = BrokenWriter()
        await local.publish("t", "x")   # must not raise

    async def test_session_closes_its_socket_when_the_broker_drops(self):
        local = self.local()
        task = await self.start_session(local)
        await wait_until(lambda: local.writer is not None)
        writer = local.writer
        await self.stop_session(task)
        self.assertTrue(writer.is_closing())

    async def test_run_reconnects_after_the_broker_drops_the_connection(self):
        real_sleep = asyncio.sleep

        async def fast_sleep(_delay):        # the real retry pause is 10 s
            await real_sleep(0.01)
        local = self.local()
        with mock.patch.object(asyncio, "sleep", fast_sleep):
            runner = asyncio.create_task(local.run())
            await wait_until(lambda: len(self.broker.publishes()) >= 1)
            for writer in self.broker.writers:
                writer.close()
            await wait_until(lambda: len(self.broker.publishes()) >= 2)   # "online" again
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        self.assertEqual(self.broker.connections, 2)


class NullMqttTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_is_a_noop_and_run_returns(self):
        null = hb.NullMqtt()
        await null.publish("t", "x", retain=True)
        await null.run()

    def test_no_broker_host_means_no_local_publisher(self):
        self.assertIsNone(make_config().ha_host)
        self.assertEqual(make_config(HA_MQTT_HOST="h", HA_MQTT_PORT="1884").ha_port, 1884)


if __name__ == "__main__":
    unittest.main()
