"""Interceptor: ARP helpers, netfilter rules, inverter discovery and lifecycle."""
import asyncio
import os
import socket
import unittest
from unittest import mock

import support  # noqa: F401  (puts the add-on sources on sys.path)
from support import wait_until

import hypon_intercept as hi

INV_IP, GW_IP, OUR_IP = "192.168.1.87", "192.168.1.254", "192.168.1.56"
INV_MAC = bytes.fromhex("aabbccddeeff")
GW_MAC = bytes.fromhex("112233445566")
OUR_MAC = bytes.fromhex("02da55bf4b95")


class FakeSock:
    """Stands in for the raw AF_PACKET socket."""

    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = list(incoming)
        self.fail_send = False
        self.closed = False

    def settimeout(self, _t):
        pass

    def send(self, frame):
        if self.fail_send:
            raise OSError("network is down")
        self.sent.append(frame)

    def recv(self, _n):
        if self.incoming:
            return self.incoming.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True


def arp_reply(sender_mac, sender_ip, target_mac=OUR_MAC, target_ip=OUR_IP):
    return hi.build_arp(2, sender_mac, sender_ip, target_mac, target_ip, target_mac)


class ArpFrameTests(unittest.TestCase):
    def test_build_arp_layout(self):
        frame = hi.build_arp(2, OUR_MAC, GW_IP, INV_MAC, INV_IP, INV_MAC)
        self.assertEqual(len(frame), 42)
        self.assertEqual(frame[0:6], INV_MAC)                      # ethernet dst
        self.assertEqual(frame[6:12], OUR_MAC)                     # ethernet src
        self.assertEqual(frame[12:14], b"\x08\x06")                # ARP
        self.assertEqual(frame[20:22], b"\x00\x02")                # opcode: reply
        self.assertEqual(frame[22:28], OUR_MAC)                    # sender MAC
        self.assertEqual(frame[28:32], socket.inet_aton(GW_IP))    # sender IP
        self.assertEqual(frame[32:38], INV_MAC)                    # target MAC
        self.assertEqual(frame[38:42], socket.inet_aton(INV_IP))   # target IP

    def test_fmt_mac(self):
        self.assertEqual(hi.fmt_mac(OUR_MAC), "02:da:55:bf:4b:95")


class ResolveMacTests(unittest.TestCase):
    def resolve(self, sock, tries=2):
        return hi.resolve_mac(sock, OUR_MAC, OUR_IP, INV_IP, tries=tries, timeout=0.01)

    def test_returns_the_mac_of_the_answering_host(self):
        sock = FakeSock([arp_reply(INV_MAC, INV_IP)])
        self.assertEqual(self.resolve(sock), INV_MAC)
        request = sock.sent[0]
        self.assertEqual(request[0:6], hi.BROADCAST)
        self.assertEqual(request[20:22], b"\x00\x01")             # who-has
        self.assertEqual(request[38:42], socket.inet_aton(INV_IP))

    def test_ignores_replies_from_other_hosts_requests_and_junk(self):
        sock = FakeSock([
            arp_reply(GW_MAC, GW_IP),                                         # another host
            hi.build_arp(1, INV_MAC, INV_IP, hi.ZERO_MAC, OUR_IP, hi.BROADCAST),  # a request
            b"\x00" * 10,                                                     # too short
            b"\xff" * 6 + b"\x00" * 6 + b"\x08\x00" + b"\x00" * 40,           # not ARP
            arp_reply(INV_MAC, INV_IP),
        ])
        self.assertEqual(self.resolve(sock), INV_MAC)

    def test_returns_none_and_retries_when_nobody_answers(self):
        sock = FakeSock()
        self.assertIsNone(self.resolve(sock, tries=3))
        self.assertEqual(len(sock.sent), 3)


class HostDiscoveryTests(unittest.TestCase):
    ROUTES = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
              "enp0s18\t00000000\tFE01A8C0\t0003\t0\t0\t100\t00000000\n"
              "enp0s18\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n")

    def test_default_route_parsing(self):
        with mock.patch.object(hi, "open", mock.mock_open(read_data=self.ROUTES), create=True):
            self.assertEqual(hi.default_route(), ("enp0s18", "192.168.1.254"))

    def test_default_route_absent(self):
        only_lan = self.ROUTES.splitlines()[0] + "\n" + self.ROUTES.splitlines()[2] + "\n"
        with mock.patch.object(hi, "open", mock.mock_open(read_data=only_lan), create=True):
            self.assertEqual(hi.default_route(), (None, None))

    def test_default_route_unreadable(self):
        with mock.patch.object(hi, "open", side_effect=OSError("nope"), create=True):
            self.assertEqual(hi.default_route(), (None, None))

    def test_iface_mac(self):
        with mock.patch.object(hi, "open", mock.mock_open(read_data="02:da:55:bf:4b:95\n"), create=True):
            self.assertEqual(hi.iface_mac("enp0s18"), OUR_MAC)

    def test_our_ip_towards_uses_the_kernel_routing_table(self):
        self.assertEqual(hi.our_ip_towards("127.0.0.1"), "127.0.0.1")


class NetRulesTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(hi, "run_ipt", return_value=True)
        self.ipt = patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self):
        return [c.args[0] for c in self.ipt.call_args_list]

    def apply_with_ip_forward(self, value):
        handle = mock.mock_open(read_data=value)
        rules = hi.NetRules(INV_IP, 18883)
        with mock.patch.object(hi, "open", handle, create=True):
            rules.apply()
        return rules, handle

    def test_apply_inserts_the_redirect_and_forward_rules(self):
        self.apply_with_ip_forward("1\n")
        self.assertEqual(self.calls(), [
            ["-t", "filter", "-I", "FORWARD", "-s", INV_IP, "-j", "ACCEPT"],
            ["-t", "filter", "-I", "FORWARD", "-d", INV_IP, "-j", "ACCEPT"],
            ["-t", "nat", "-I", "PREROUTING", "-s", INV_IP, "-p", "tcp", "--dport", "1883",
             "-j", "REDIRECT", "--to-ports", "18883"],
        ])

    def test_every_rule_is_scoped_to_the_inverter_ip(self):
        rules, _ = self.apply_with_ip_forward("1\n")
        rules.revert()
        for args in self.calls():
            self.assertIn(INV_IP, args)

    def test_revert_deletes_exactly_what_apply_inserted(self):
        rules, _ = self.apply_with_ip_forward("1\n")
        rules.revert()
        applied, removed = self.calls()[:3], self.calls()[3:]
        self.assertEqual(len(removed), 3)
        self.assertEqual(sorted(a[:2] + ["-D"] + a[3:] for a in applied), sorted(removed))

    def test_ip_forward_already_enabled_is_left_alone(self):
        rules, handle = self.apply_with_ip_forward("1\n")
        handle().write.assert_not_called()
        with mock.patch.object(hi, "open", handle, create=True):
            rules.revert()
        handle().write.assert_not_called()

    def test_ip_forward_enabled_then_restored_on_revert(self):
        rules, handle = self.apply_with_ip_forward("0\n")
        self.assertEqual([c.args[0] for c in handle().write.call_args_list], ["1\n"])
        with mock.patch.object(hi, "open", handle, create=True):
            rules.revert()
        self.assertEqual([c.args[0] for c in handle().write.call_args_list], ["1\n", "0\n"])

    def test_unwritable_ip_forward_only_warns(self):
        rules = hi.NetRules(INV_IP, 18883)
        with mock.patch.object(hi, "open", side_effect=OSError("read-only"), create=True):
            rules.apply()   # must not raise
            rules.revert()
        self.assertEqual(len(self.calls()), 6)


class DiscoverTests(unittest.TestCase):
    def interceptor(self, **env):
        base = {"HYPON_INV_IP": INV_IP}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return hi.Interceptor()

    def discover(self, interceptor, route=("enp0s18", GW_IP), resolve=None):
        sock = FakeSock()
        resolver = resolve or mock.Mock(return_value=GW_MAC)
        with (mock.patch.object(hi, "default_route", return_value=route),
              mock.patch.object(hi, "iface_mac", return_value=OUR_MAC),
              mock.patch.object(hi, "our_ip_towards", return_value=OUR_IP),
              mock.patch.object(hi, "open_arp_socket", return_value=sock),
              mock.patch.object(hi, "resolve_mac", resolver)):
            interceptor.discover()
        return sock, resolver

    def test_missing_inverter_ip(self):
        with self.assertRaisesRegex(SystemExit, "HYPON_INV_IP"):
            self.discover(self.interceptor(HYPON_INV_IP=""))

    def test_invalid_ips_are_rejected_early(self):
        with self.assertRaisesRegex(SystemExit, "inverter_ip.*not a valid IPv4"):
            self.discover(self.interceptor(HYPON_INV_IP="192.168.1."))
        with self.assertRaisesRegex(SystemExit, "gateway_ip.*not a valid IPv4"):
            self.discover(self.interceptor(HYPON_GW_IP="router"))

    def test_undetectable_interface_or_gateway_is_fatal(self):
        with self.assertRaisesRegex(SystemExit, "LAN interface"):
            self.discover(self.interceptor(), route=(None, None))
        with self.assertRaisesRegex(SystemExit, "gateway IP"):
            self.discover(self.interceptor(), route=("enp0s18", None))

    def test_values_are_detected_from_the_default_route(self):
        it = self.interceptor()
        self.discover(it)
        self.assertEqual((it.iface, it.gw_ip, it.our_ip, it.our_mac), ("enp0s18", GW_IP, OUR_IP, OUR_MAC))

    def test_explicit_interface_and_gateway_win_over_the_route(self):
        it = self.interceptor(HYPON_IFACE="eth9", HYPON_GW_IP="192.168.1.1")
        self.discover(it)
        self.assertEqual((it.iface, it.gw_ip), ("eth9", "192.168.1.1"))

    def test_a_silent_inverter_is_not_an_error_at_discovery(self):
        # Regression: at night the inverter does not answer ARP and the add-on must keep running.
        it = self.interceptor()
        resolver = mock.Mock(return_value=GW_MAC)
        self.discover(it, resolve=resolver)
        self.assertIsNone(it.inv_mac)
        self.assertEqual(it.gw_mac, GW_MAC)
        self.assertEqual([c.args[3] for c in resolver.call_args_list], [GW_IP])   # gateway only


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sock = FakeSock()
        with mock.patch.dict(os.environ, {"HYPON_INV_IP": INV_IP}, clear=True):
            self.it = hi.Interceptor()
        self.it.sock, self.it.our_mac, self.it.our_ip = self.sock, OUR_MAC, OUR_IP
        self.it.gw_ip, self.it.gw_mac = GW_IP, GW_MAC
        for name, value in (("INVERTER_RETRY_S", 0.01), ("ARP_INTERVAL_S", 0.01)):
            patcher = mock.patch.object(hi, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rules = []

        class FakeRules:
            def __init__(inner, *args):
                inner.args, inner.applied, inner.reverted = args, False, False
                self.rules.append(inner)

            def apply(inner):
                inner.applied = True

            def revert(inner):
                inner.reverted = True
        patcher = mock.patch.object(hi, "NetRules", FakeRules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def inverter_wakes_up_after(self, failures):
        results = [None] * failures + [INV_MAC]
        return mock.patch.object(hi, "resolve_mac", side_effect=lambda *a, **k: results.pop(0) if results
                                 else INV_MAC)

    async def test_waits_until_the_inverter_answers_then_spoofs_and_redirects(self):
        with self.inverter_wakes_up_after(3):
            task = asyncio.create_task(self.it._intercept())
            await wait_until(lambda: self.it.inv_mac == INV_MAC)
            await wait_until(lambda: len(self.sock.sent) >= 2)
        self.assertEqual(len(self.rules), 1)
        self.assertTrue(self.rules[0].applied)
        self.assertEqual(self.rules[0].args, (INV_IP, 18883))
        self.it.request_stop()
        await asyncio.wait_for(task, 3)

    async def test_nothing_is_spoofed_or_redirected_while_the_inverter_is_silent(self):
        with mock.patch.object(hi, "resolve_mac", return_value=None) as resolver:
            task = asyncio.create_task(self.it._intercept())
            await wait_until(lambda: resolver.call_count >= 3)
            self.assertFalse(task.done())
            self.assertEqual((self.sock.sent, self.rules), ([], []))
            self.it.request_stop()
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.rules, [])

    async def test_spoof_frame_says_the_gateway_is_us(self):
        self.it.inv_mac = INV_MAC
        frame = self.it._spoof_frame()
        self.assertEqual(frame[0:6], INV_MAC)
        self.assertEqual(frame[22:28], OUR_MAC)
        self.assertEqual(frame[28:32], socket.inet_aton(GW_IP))
        self.assertEqual(frame[38:42], socket.inet_aton(INV_IP))

    async def test_heal_frame_restores_the_real_gateway_mac(self):
        self.it.inv_mac = INV_MAC
        frame = self.it._heal_frame()
        self.assertEqual(frame[22:28], GW_MAC)
        self.assertEqual(frame[28:32], socket.inet_aton(GW_IP))
        self.it.heal()
        self.assertEqual(len(self.sock.sent), 5)
        self.assertTrue(all(f == frame for f in self.sock.sent))

    async def test_heal_is_a_noop_without_known_macs(self):
        self.it.heal()                          # inverter MAC unknown (asleep all along)
        self.it.inv_mac, self.it.gw_mac = INV_MAC, None
        self.it.heal()                          # gateway MAC unknown
        self.assertEqual(self.sock.sent, [])

    async def test_arp_loop_keeps_going_when_a_send_fails_and_stops_on_request(self):
        self.it.inv_mac = INV_MAC
        self.sock.fail_send = True
        task = asyncio.create_task(self.it.arp_loop())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())           # OSError did not kill the loop
        self.sock.fail_send = False
        await wait_until(lambda: len(self.sock.sent) >= 2)
        self.it.request_stop()
        await asyncio.wait_for(task, 3)

    def patch_run_environment(self):
        """Make Interceptor.run() testable: no real discovery, no real bridge."""
        self.bridge_started = asyncio.Event()

        async def fake_bridge(_cfg):
            self.bridge_started.set()
            await asyncio.Event().wait()
        for patcher in (mock.patch.dict(os.environ, {"HYPON_INV_IP": INV_IP}, clear=True),
                        mock.patch.object(hi.Interceptor, "discover", lambda _self: None),
                        mock.patch.object(hi.hypon_bridge, "main_async", fake_bridge)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.it.listen_port = 18883

    async def test_run_starts_the_bridge_even_if_the_inverter_is_asleep(self):
        # Regression: the add-on used to exit when the inverter did not answer ARP at startup.
        self.patch_run_environment()
        with mock.patch.object(hi, "resolve_mac", return_value=None):
            task = asyncio.create_task(self.it.run())
            await asyncio.wait_for(self.bridge_started.wait(), 3)
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.rules, [])        # nothing to revert
        self.assertEqual(self.sock.sent, [])    # nothing to heal
        self.assertTrue(self.sock.closed)

    async def test_run_reverts_rules_heals_and_closes_on_shutdown(self):
        self.patch_run_environment()
        with mock.patch.object(hi, "resolve_mac", return_value=INV_MAC):
            task = asyncio.create_task(self.it.run())
            await wait_until(lambda: self.rules and self.rules[0].applied and self.sock.sent)
            task.cancel()
            await asyncio.wait_for(task, 3)
        self.assertTrue(self.rules[0].reverted)
        self.assertGreaterEqual(len([f for f in self.sock.sent if f[22:28] == GW_MAC]), 5)  # healed
        self.assertTrue(self.sock.closed)


if __name__ == "__main__":
    unittest.main()
