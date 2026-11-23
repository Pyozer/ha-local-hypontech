"""Interceptor: ARP helpers, netfilter rules, inverter discovery and lifecycle."""
import asyncio
import errno
import itertools
import os
import socket
import unittest
from unittest import mock

import support  # (also puts the add-on sources on sys.path)
from support import wait_until

import hypon_intercept as hi
from hypon_status import Link
from hypon_store import StateStore

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
        self.would_block = 0      # sends that fail as a full send queue does
        self.block_errno = errno.EAGAIN
        self.closed = False

    def settimeout(self, _t):
        pass

    def send(self, frame):
        if self.fail_send:
            raise OSError("network is down")
        if self.would_block:
            self.would_block -= 1
            raise OSError(self.block_errno, "send queue full")
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

    def test_parse_mac_is_the_inverse_of_fmt_mac(self):
        self.assertEqual(hi.parse_mac(hi.fmt_mac(INV_MAC)), INV_MAC)
        for bad in (None, "", "not a mac", "aa:bb:cc", "aa:bb:cc:dd:ee:ff:00"):
            with self.subTest(bad=bad):
                self.assertIsNone(hi.parse_mac(bad))

    def test_parse_arp_returns_the_sender(self):
        self.assertEqual(hi.parse_arp(arp_reply(INV_MAC, INV_IP)), (INV_MAC, INV_IP))
        request = hi.build_arp(1, INV_MAC, INV_IP, hi.ZERO_MAC, GW_IP, hi.BROADCAST)
        self.assertEqual(hi.parse_arp(request), (INV_MAC, INV_IP))

    def test_parse_arp_ignores_what_is_not_arp(self):
        self.assertIsNone(hi.parse_arp(b"\x00" * 10))
        self.assertIsNone(hi.parse_arp(b"\xff" * 6 + b"\x00" * 6 + b"\x08\x00" + b"\x00" * 40))


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


class DiscoverBase(unittest.TestCase):
    def interceptor(self, **env):
        base = {"HYPON_INV_IPS": INV_IP}
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


class DiscoverTests(DiscoverBase):
    def test_missing_inverter_ip(self):
        with self.assertRaisesRegex(SystemExit, "HYPON_INV_IPS"):
            self.discover(self.interceptor(HYPON_INV_IPS=""))

    def test_invalid_ips_are_rejected_early(self):
        with self.assertRaisesRegex(SystemExit, "inverter_ips.*not a valid IPv4"):
            self.discover(self.interceptor(HYPON_INV_IPS="192.168.1."))
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
        self.assertIsNone(it.inverters[0].mac)
        self.assertEqual(it.gw_mac, GW_MAC)
        self.assertEqual([c.args[3] for c in resolver.call_args_list], [GW_IP])   # gateway only


class ParseIpsTests(unittest.TestCase):
    def test_separators_with_or_without_spaces(self):
        expected = ["192.168.1.8", "192.168.1.9", "192.168.1.7"]
        for raw in ("192.168.1.8,192.168.1.9,192.168.1.7", "192.168.1.8, 192.168.1.9 ,192.168.1.7",
                    "  192.168.1.8 ; 192.168.1.9 ;192.168.1.7,", "192.168.1.8 192.168.1.9\t192.168.1.7"):
            with self.subTest(raw=raw):
                self.assertEqual(hi.parse_ips(raw), expected)
        self.assertEqual(hi.parse_ips("192.168.1.8 192.168.1.9\n192.168.1.7"),
                         ["192.168.1.8", "192.168.1.9", "192.168.1.7"])

    def test_empty_values_are_ignored(self):
        self.assertEqual(hi.parse_ips(""), [])
        self.assertEqual(hi.parse_ips(None), [])
        self.assertEqual(hi.parse_ips("  , "), [])


class MultipleInvertersDiscoverTests(DiscoverBase):
    INV2 = "192.168.1.88"

    def test_every_listed_ip_becomes_an_inverter_in_order(self):
        it = self.interceptor(HYPON_INV_IPS=f"{INV_IP}, {self.INV2},192.168.1.89")
        self.assertEqual([i.ip for i in it.inverters], [INV_IP, self.INV2, "192.168.1.89"])

    def test_an_invalid_ip_in_the_list_is_rejected_early(self):
        with self.assertRaisesRegex(SystemExit, "inverter_ips.*not a valid IPv4"):
            self.discover(self.interceptor(HYPON_INV_IPS=f"{INV_IP}, 192.168.1."))

    def test_the_same_ip_listed_twice_is_rejected_early(self):
        for raw in (f"{INV_IP}, {INV_IP}", f"{INV_IP},{self.INV2} ,{INV_IP}"):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    SystemExit, f"inverter_ips lists {INV_IP} more than once"):
                self.discover(self.interceptor(HYPON_INV_IPS=raw))

    def test_only_the_gateway_is_resolved_at_discovery(self):
        it = self.interceptor(HYPON_INV_IPS=f"{INV_IP}, {self.INV2}")
        resolver = mock.Mock(return_value=GW_MAC)
        self.discover(it, resolve=resolver)
        self.assertEqual([c.args[3] for c in resolver.call_args_list], [GW_IP])
        self.assertEqual([i.mac for i in it.inverters], [None, None])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sock = FakeSock()
        with mock.patch.dict(os.environ, {"HYPON_INV_IPS": INV_IP}, clear=True):
            self.it = hi.Interceptor()
        self.it.sock, self.it.our_mac, self.it.our_ip = self.sock, OUR_MAC, OUR_IP
        self.it.gw_ip, self.it.gw_mac = GW_IP, GW_MAC
        self.inv = self.it.inverters[0]
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
            await wait_until(lambda: self.inv.mac == INV_MAC)
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
        self.inv.mac = INV_MAC
        frame = self.it._spoof_frame(self.inv)
        self.assertEqual(frame[0:6], INV_MAC)
        self.assertEqual(frame[22:28], OUR_MAC)
        self.assertEqual(frame[28:32], socket.inet_aton(GW_IP))
        self.assertEqual(frame[38:42], socket.inet_aton(INV_IP))

    async def test_heal_frame_restores_the_real_gateway_mac(self):
        self.inv.mac = INV_MAC
        frame = self.it._heal_frame(self.inv)
        self.assertEqual(frame[22:28], GW_MAC)
        self.assertEqual(frame[28:32], socket.inet_aton(GW_IP))
        self.it.heal()
        self.assertEqual(len(self.sock.sent), 5)
        self.assertTrue(all(f == frame for f in self.sock.sent))

    async def test_heal_is_a_noop_without_known_macs(self):
        self.it.heal()                          # inverter MAC unknown (asleep all along)
        self.inv.mac, self.it.gw_mac = INV_MAC, None
        self.it.heal()                          # gateway MAC unknown
        self.assertEqual(self.sock.sent, [])

    async def test_arp_loop_keeps_going_when_a_send_fails_and_stops_on_request(self):
        self.inv.mac = INV_MAC
        self.sock.fail_send = True
        task = asyncio.create_task(self.it.arp_loop(self.inv))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())           # OSError did not kill the loop
        self.sock.fail_send = False
        await wait_until(lambda: len(self.sock.sent) >= 2)
        self.it.request_stop()
        await asyncio.wait_for(task, 3)

    def two_inverters(self):
        with mock.patch.dict(os.environ, {"HYPON_INV_IPS": f"{INV_IP}, 192.168.1.88"},
                             clear=True):
            self.it.inverters = hi.Interceptor().inverters
        self.inv, self.inv2 = self.it.inverters
        self.inv2_mac = bytes.fromhex("010203040506")

    def only_inv2_asleep(self, asleep_ip):
        macs = {INV_IP: INV_MAC, "192.168.1.88": self.inv2_mac}
        return mock.patch.object(hi, "resolve_mac", side_effect=lambda _s, _m, _i, ip, **k:
                                 None if ip == asleep_ip else macs[ip])

    async def test_each_inverter_gets_its_own_spoofing_and_rules(self):
        self.two_inverters()
        with self.only_inv2_asleep(None):
            task = asyncio.create_task(self.it._intercept())
            await wait_until(lambda: len(self.rules) == 2)
            await wait_until(lambda: {f[0:6] for f in self.sock.sent} == {INV_MAC, self.inv2_mac})
        self.assertEqual(sorted(r.args for r in self.rules), [(INV_IP, 18883), ("192.168.1.88", 18883)])
        self.assertTrue(all(r.applied for r in self.rules))
        self.assertEqual([i.mac for i in self.it.inverters], [INV_MAC, self.inv2_mac])
        self.it.request_stop()
        await asyncio.wait_for(task, 3)

    async def test_a_sleeping_inverter_does_not_hold_back_the_others(self):
        self.two_inverters()
        with self.only_inv2_asleep("192.168.1.88"):
            task = asyncio.create_task(self.it._intercept())
            await wait_until(lambda: len(self.rules) == 1 and self.sock.sent)
            self.assertEqual(self.rules[0].args, (INV_IP, 18883))
            self.assertIsNone(self.inv2.mac)
            self.assertEqual({f[0:6] for f in self.sock.sent}, {INV_MAC})  # nothing sent to the sleeper
            self.it.request_stop()
            await asyncio.wait_for(task, 3)

    async def test_heal_restores_every_known_inverter(self):
        self.two_inverters()
        self.inv.mac, self.inv2.mac = INV_MAC, self.inv2_mac
        self.it.heal()
        self.assertEqual(len(self.sock.sent), 10)
        for inv in (self.inv, self.inv2):
            frames = [f for f in self.sock.sent if f[38:42] == socket.inet_aton(inv.ip)]
            self.assertEqual(len(frames), 5)
            self.assertEqual(frames[0][22:28], GW_MAC)

    async def test_rules_are_reverted_last_in_first_out(self):
        self.patch_run_environment()
        self.two_inverters()
        order = []
        with mock.patch.object(hi, "resolve_mac", side_effect=lambda _s, _m, _i, ip, **k:
                               INV_MAC if ip == INV_IP else self.inv2_mac):
            task = asyncio.create_task(self.it.run())
            await wait_until(lambda: len(self.rules) == 2 and all(r.applied for r in self.rules))
            for rule in self.rules:
                rule.revert = lambda rule=rule: order.append(rule.args[0])
            applied = [r.args[0] for r in self.rules]
            task.cancel()
            await asyncio.wait_for(task, 3)
        self.assertEqual(order, applied[::-1])

    def patch_run_environment(self):
        """Make Interceptor.run() testable: no real discovery, no real bridge."""
        self.bridge_started = asyncio.Event()

        async def fake_bridge(_cfg, store, links):
            self.bridge_args = (store, links)
            self.bridge_started.set()
            await asyncio.Event().wait()

        async def fake_monitor_run(_monitor, _sock, _stop):
            await asyncio.Event().wait()
        self.it.iface = "eth0"
        for patcher in (mock.patch.dict(os.environ, {"HYPON_INV_IPS": INV_IP, "HYPON_DATA_DIR": ""},
                                        clear=True),
                        mock.patch.object(hi.Interceptor, "discover", lambda _self: None),
                        mock.patch.object(hi.hypon_bridge, "main_async", fake_bridge),
                        mock.patch.object(hi, "open_arp_socket", lambda _iface: FakeSock()),
                        mock.patch.object(hi, "lan_hosts", lambda _iface, _ip: []),
                        mock.patch.object(hi.LinkMonitor, "run", fake_monitor_run)):
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


class KnownMacTests(unittest.IsolatedAsyncioTestCase):
    """The MAC of an inverter is remembered: it is how it is recognised after a restart."""

    def setUp(self):
        self.directory = self.enterContext(support.temp_dir())
        self.env = {"HYPON_INV_IPS": INV_IP, "HYPON_DATA_DIR": self.directory}
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.it = hi.Interceptor()
        self.it.sock, self.it.our_mac, self.it.our_ip = FakeSock(), OUR_MAC, OUR_IP
        self.it.gw_ip, self.it.gw_mac = GW_IP, GW_MAC
        self.inv = self.it.inverters[0]

    async def test_a_resolved_mac_is_remembered_in_the_store(self):
        self.it.store = StateStore(self.directory)
        with mock.patch.object(hi, "resolve_mac", return_value=INV_MAC):
            self.assertTrue(await self.it._wait_for_inverter(self.inv))
        self.assertEqual((self.inv.mac, self.inv.known_mac), (INV_MAC, INV_MAC))
        self.assertEqual(self.it.store.get_mac(INV_IP), "aa:bb:cc:dd:ee:ff")
        self.assertTrue(self.it.store.flush(force=True))
        self.assertEqual(StateStore.open(self.directory).get_mac(INV_IP), "aa:bb:cc:dd:ee:ff")

    async def test_without_a_store_resolving_still_works(self):
        with mock.patch.object(hi, "resolve_mac", return_value=INV_MAC):
            self.assertTrue(await self.it._wait_for_inverter(self.inv))
        self.assertEqual(self.inv.known_mac, INV_MAC)

    async def test_run_shares_the_store_and_the_links_with_the_bridge(self):
        seen = {}

        async def fake_bridge(_cfg, store, links):
            seen.update(store=store, links=links, known=self.inv.known_mac)
            await asyncio.Event().wait()

        async def idle(*_args):
            await asyncio.Event().wait()

        seed = StateStore(self.directory)
        seed.set_mac(INV_IP, "aa:bb:cc:dd:ee:ff")
        seed.flush(force=True)
        with (mock.patch.dict(os.environ, self.env, clear=True),
              mock.patch.object(hi.Interceptor, "discover", lambda _self: None),
              mock.patch.object(hi.hypon_bridge, "main_async", fake_bridge),
              mock.patch.object(hi, "resolve_mac", return_value=None),
              mock.patch.object(hi, "open_arp_socket", lambda _iface: FakeSock()),
              mock.patch.object(hi, "lan_hosts", lambda _iface, _ip: []),
              mock.patch.object(hi.LinkMonitor, "run", idle)):
            task = asyncio.create_task(self.it.run())
            await wait_until(lambda: "store" in seen)
            task.cancel()
            await asyncio.wait_for(task, 3)
        self.assertEqual(seen["known"], INV_MAC)          # known although the inverter is asleep
        self.assertIsInstance(seen["links"], dict)
        self.assertIs(seen["store"], self.it.store)

    async def test_a_missing_raw_socket_does_not_stop_the_add_on(self):
        monitor = hi.LinkMonitor(self.it.inverters, OUR_MAC, OUR_IP, {})
        self.it.iface = "eth0"
        with mock.patch.object(hi, "open_arp_socket", side_effect=OSError("denied")):
            await self.it._monitor(monitor)               # returns, does not raise

    async def test_the_monitor_socket_is_closed_when_it_stops(self):
        sock = FakeSock()

        async def run(_sock, _stop):
            return
        monitor = hi.LinkMonitor(self.it.inverters, OUR_MAC, OUR_IP, {})
        monitor.run = run
        with mock.patch.object(hi, "open_arp_socket", return_value=sock):
            await self.it._monitor(monitor)
        self.assertTrue(sock.closed)


class LinkMonitorTests(unittest.TestCase):
    INV2_IP, INV2_MAC = "192.168.1.88", bytes.fromhex("010203040506")

    def setUp(self):
        self.now = 1000.0
        self.links = {}
        self.inv = hi.Inverter(INV_IP)
        self.inv.known_mac = INV_MAC
        self.monitor = hi.LinkMonitor([self.inv], OUR_MAC, OUR_IP, self.links, clock=lambda: self.now)

    def link(self):
        self.monitor.refresh()
        return self.links[INV_IP]

    def hears(self, mac, ip, kind="reply"):
        frame = (arp_reply(mac, ip) if kind == "reply"
                 else hi.build_arp(1, mac, ip, hi.ZERO_MAC, GW_IP, hi.BROADCAST))
        self.monitor.handle_frame(frame)

    def test_nothing_is_known_before_anything_is_heard_and_nothing_was_searched(self):
        self.assertEqual(self.link(), Link(mac="aa:bb:cc:dd:ee:ff"))

    def test_an_answer_from_the_inverter_at_its_ip_means_alive(self):
        self.hears(INV_MAC, INV_IP)
        link = self.link()
        self.assertTrue(link.alive)
        self.assertTrue(link.checked)          # it answered: the network was looked at

    def test_its_own_requests_count_too(self):
        self.hears(INV_MAC, INV_IP, kind="request")
        self.assertTrue(self.link().alive)

    def test_a_sign_of_life_expires(self):
        self.hears(INV_MAC, INV_IP)
        self.now += hi.ALIVE_TTL_S - 1
        self.assertTrue(self.link().alive)
        self.now += 2
        link = self.link()
        self.assertEqual((link.alive, link.checked), (False, False))

    def test_another_host_answering_at_that_ip_is_not_the_inverter(self):
        self.hears(GW_MAC, INV_IP)         # a different device took the address
        self.assertFalse(self.link().alive)

    def test_the_inverter_seen_under_another_ip_has_moved(self):
        self.hears(INV_MAC, "192.168.1.140", kind="request")
        link = self.link()
        self.assertEqual((link.moved_to, link.alive), ("192.168.1.140", False))

    def test_a_sighting_elsewhere_expires(self):
        self.hears(INV_MAC, "192.168.1.140")
        self.now += hi.MOVED_TTL_S + 1
        self.assertIsNone(self.link().moved_to)

    def test_coming_back_to_its_ip_cancels_the_move(self):
        self.hears(INV_MAC, "192.168.1.140")
        self.hears(INV_MAC, INV_IP)
        link = self.link()
        self.assertEqual((link.moved_to, link.alive), (None, True))

    def test_address_conflict_probes_carry_no_sender_and_are_ignored(self):
        self.hears(INV_MAC, "0.0.0.0", kind="request")     # sent by a booting device
        link = self.link()
        self.assertEqual((link.moved_to, link.alive), (None, False))

    def test_our_own_spoofed_frames_and_junk_are_ignored(self):
        self.monitor.handle_frame(hi.build_arp(2, OUR_MAC, GW_IP, INV_MAC, INV_IP, INV_MAC))
        self.monitor.handle_frame(b"\x00" * 10)
        self.assertEqual(self.link(), Link(mac="aa:bb:cc:dd:ee:ff"))

    def test_an_inverter_whose_mac_is_unknown_cannot_be_followed_nor_declared_absent(self):
        self.inv.known_mac = None
        self.hears(INV_MAC, "192.168.1.140")
        self.monitor._swept_at = self.now
        self.assertEqual(self.link(), Link())

    def test_each_inverter_is_followed_on_its_own(self):
        inv2 = hi.Inverter(self.INV2_IP)
        inv2.known_mac = self.INV2_MAC
        monitor = hi.LinkMonitor([self.inv, inv2], OUR_MAC, OUR_IP, self.links, clock=lambda: self.now)
        monitor.handle_frame(arp_reply(INV_MAC, INV_IP))
        monitor.handle_frame(arp_reply(self.INV2_MAC, "192.168.1.141"))
        monitor.refresh()
        self.assertTrue(self.links[INV_IP].alive)
        self.assertEqual(self.links[self.INV2_IP].moved_to, "192.168.1.141")
        self.assertFalse(self.links[self.INV2_IP].alive)

    def test_probe_asks_every_inverter_who_it_is(self):
        inv2 = hi.Inverter(self.INV2_IP)
        monitor = hi.LinkMonitor([self.inv, inv2], OUR_MAC, OUR_IP, self.links)
        sock = FakeSock()
        monitor.probe(sock)
        self.assertEqual([f[38:42] for f in sock.sent],
                         [socket.inet_aton(INV_IP), socket.inet_aton(self.INV2_IP)])
        self.assertTrue(all(f[0:6] == hi.BROADCAST and f[20:22] == b"\x00\x01" for f in sock.sent))

    def test_a_failing_probe_only_warns(self):
        sock = FakeSock()
        sock.fail_send = True
        self.monitor.probe(sock)                            # must not raise

    def test_drain_reads_everything_waiting_then_refreshes(self):
        sock = FakeSock([arp_reply(INV_MAC, INV_IP), arp_reply(GW_MAC, GW_IP)])
        self.monitor.drain(sock)
        self.assertTrue(self.links[INV_IP].alive)
        self.assertEqual(sock.incoming, [])


class LinkMonitorSweepTests(unittest.IsolatedAsyncioTestCase):
    """Looking for an inverter that does not answer: ask the whole LAN, then trust what came back."""

    HOSTS = [f"192.168.1.{n}" for n in range(1, 11)]

    def setUp(self):
        self.now = 1000.0
        self.links = {}
        self.inv = hi.Inverter(INV_IP)
        self.inv.known_mac = INV_MAC
        self.monitor = hi.LinkMonitor([self.inv], OUR_MAC, OUR_IP, self.links, hosts=self.HOSTS,
                                      clock=lambda: self.now)
        for name in ("SWEEP_REPLY_WAIT_S", "SWEEP_PAUSE_S"):
            patcher = mock.patch.object(hi, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_sweep_asks_every_host_of_the_lan_who_it_is(self):
        sock = FakeSock()
        await self.monitor.sweep(sock)
        self.assertEqual([socket.inet_ntoa(f[38:42]) for f in sock.sent], self.HOSTS)
        self.assertTrue(all(f[0:6] == hi.BROADCAST and f[20:22] == b"\x00\x01" for f in sock.sent))

    async def test_what_was_not_found_after_a_sweep_is_absent_not_unknown(self):
        self.monitor.refresh()
        self.assertFalse(self.links[INV_IP].checked)          # nothing searched yet
        await self.monitor.sweep(FakeSock())
        self.monitor.refresh()
        link = self.links[INV_IP]
        self.assertEqual((link.checked, link.alive, link.moved_to), (True, False, None))

    async def test_the_sweep_finds_an_inverter_that_moved(self):
        self.monitor.handle_frame(arp_reply(INV_MAC, "192.168.1.140"))   # its answer to the sweep
        await self.monitor.sweep(FakeSock())
        self.monitor.refresh()
        link = self.links[INV_IP]
        self.assertEqual((link.moved_to, link.checked), ("192.168.1.140", True))

    async def test_a_sweep_stops_covering_the_network_after_a_while(self):
        await self.monitor.sweep(FakeSock())
        self.now += hi.SWEEP_VALID_S + 1
        self.monitor.refresh()
        self.assertFalse(self.links[INV_IP].checked)

    async def test_a_full_send_queue_is_waited_for_not_given_up_on(self):
        # The incident of 23 November: "[Errno 11] Resource temporarily unavailable" on every
        # sweep, so the network was never "looked at" and a faded inverter read "unavailable".
        sock = FakeSock()
        sock.would_block = 5                                  # the queue is full for a moment
        await self.monitor.sweep(sock)
        self.assertEqual([socket.inet_ntoa(f[38:42]) for f in sock.sent], self.HOSTS)  # none lost
        self.monitor.refresh()
        self.assertTrue(self.links[INV_IP].checked)

    async def test_a_full_queue_answering_no_buffer_space_is_waited_for_too(self):
        sock = FakeSock()
        sock.would_block, sock.block_errno = 5, errno.ENOBUFS      # what a full qdisc answers
        await self.monitor.sweep(sock)
        self.assertEqual([socket.inet_ntoa(f[38:42]) for f in sock.sent], self.HOSTS)

    async def test_a_queue_that_blocks_in_the_middle_of_the_sweep_resumes_where_it_stopped(self):
        sock = FakeSock()
        original_send = sock.send
        calls = []

        def send(frame):
            calls.append(1)
            if len(calls) == 4:
                sock.would_block = 3
            original_send(frame)
        sock.send = send
        await self.monitor.sweep(sock)
        self.assertEqual([socket.inet_ntoa(f[38:42]) for f in sock.sent], self.HOSTS)

    async def test_a_queue_that_never_drains_gives_up_after_a_while_and_claims_nothing(self):
        sock = FakeSock()
        sock.would_block = float("inf")
        with support.logging_enabled(), self.assertLogs("hypon.intercept", "WARNING") as logs:
            await self.monitor.sweep(sock)                    # returns, never loops forever
        self.assertIn("stays full", logs.records[0].getMessage())
        self.monitor.refresh()
        self.assertFalse(self.links[INV_IP].checked)

    async def test_the_sweep_is_paced_in_small_batches(self):
        delays, real_sleep = [], asyncio.sleep

        async def spy(delay, *args):
            delays.append(delay)
            await real_sleep(0)
        with (mock.patch.object(hi, "SWEEP_BATCH", 4), mock.patch.object(hi, "SWEEP_PAUSE_S", 0.01),
              mock.patch.object(hi.asyncio, "sleep", spy)):
            await self.monitor.sweep(FakeSock())
        self.assertEqual(delays.count(0.01), len(self.HOSTS) // 4)   # a pause after every batch of 4

    async def test_a_failing_sweep_gives_up_without_claiming_anything(self):
        sock = FakeSock()
        sock.fail_send = True
        await self.monitor.sweep(sock)                        # must not raise
        self.monitor.refresh()
        self.assertFalse(self.links[INV_IP].checked)

    async def test_a_sweep_is_only_needed_while_a_known_inverter_does_not_answer(self):
        self.assertTrue(self.monitor.needs_sweep())
        self.monitor.handle_frame(arp_reply(INV_MAC, INV_IP))
        self.assertFalse(self.monitor.needs_sweep())          # answering: no need to search
        self.now += hi.ALIVE_TTL_S + 1
        self.assertTrue(self.monitor.needs_sweep())
        self.inv.known_mac = None
        self.assertFalse(self.monitor.needs_sweep())          # nothing to look for without a MAC


class SweepScheduleTests(unittest.IsolatedAsyncioTestCase):
    """The search backs off: a night of silence must not mean a scan every two minutes."""

    def setUp(self):
        self.now = 1000.0
        self.inv = hi.Inverter(INV_IP)
        self.inv.known_mac = INV_MAC
        self.monitor = hi.LinkMonitor([self.inv], OUR_MAC, OUR_IP, {}, hosts=["192.168.1.9"],
                                      clock=lambda: self.now)
        for name in ("SWEEP_REPLY_WAIT_S", "SWEEP_PAUSE_S"):
            patcher = mock.patch.object(hi, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def sweeps_until(self, horizon):
        """Times (seconds from now) at which a sweep is made during the next `horizon` seconds."""
        start, times = self.now, []
        while self.now - start <= horizon:
            if self.monitor.sweep_due():
                times.append(self.now - start)
                await self.monitor.sweep(FakeSock())
            self.now += 1
        return times

    async def test_first_at_once_then_30_s_1_min_2_min_then_every_5_min(self):
        times = await self.sweeps_until(30 + 60 + 120 + 300 + 300 + 5)
        self.assertEqual(times, [0, 30, 90, 210, 510, 810])

    async def test_a_silent_night_settles_at_one_search_every_five_minutes(self):
        times = await self.sweeps_until(8 * 3600)
        gaps = [b - a for a, b in itertools.pairwise(times)]
        self.assertEqual(gaps[:4], [30, 60, 120, 300])
        self.assertEqual(set(gaps[3:]), {300})
        self.assertLess(len(times), 8 * 3600 / 300 + 5)         # about 100, not 240

    async def test_nothing_is_searched_while_the_inverter_answers(self):
        self.monitor.handle_frame(arp_reply(INV_MAC, INV_IP))
        self.assertFalse(self.monitor.sweep_due())

    async def test_an_inverter_that_answers_again_starts_the_sequence_over(self):
        await self.sweeps_until(700)                            # deep into the slow rhythm
        self.monitor.handle_frame(arp_reply(INV_MAC, INV_IP))   # it is back
        self.assertFalse(self.monitor.sweep_due())
        self.now += hi.ALIVE_TTL_S + 1                          # and gone again
        times = await self.sweeps_until(100)
        self.assertEqual(times, [0, 30, 90])                    # at once, then 30 s, then 1 min

    async def test_the_sweep_still_covers_the_network_at_the_slowest_rhythm(self):
        await self.sweeps_until(2000)
        self.monitor.refresh()
        self.assertTrue(self.monitor.links[INV_IP].checked)     # coverage never lapses between sweeps


class LanHostsTests(unittest.TestCase):
    ROUTES = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
              "enp0s18\t00000000\tFE01A8C0\t0003\t0\t0\t100\t00000000\n"
              "enp0s18\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n")

    def hosts(self, routes, iface="enp0s18"):
        with mock.patch.object(hi, "open", mock.mock_open(read_data=routes), create=True):
            return hi.lan_hosts(iface, OUR_IP)

    def test_hosts_of_the_connected_network_without_ourselves(self):
        hosts = self.hosts(self.ROUTES)
        self.assertEqual(len(hosts), 253)                    # a /24: 254 hosts, minus us
        self.assertNotIn(OUR_IP, hosts)
        self.assertIn(INV_IP, hosts)
        self.assertNotIn("192.168.1.0", hosts)
        self.assertNotIn("192.168.1.255", hosts)

    def test_a_smaller_network_is_honoured(self):
        routes = self.ROUTES.replace("00FFFFFF", "F0FFFFFF")    # a /28 (mask 255.255.255.240)
        routes = routes.replace("0001A8C0", "3801A8C0")         # 192.168.1.56 network
        hosts = self.hosts(routes)
        self.assertEqual(len(hosts), 13)
        self.assertTrue(all(h.startswith("192.168.1.") for h in hosts))

    def test_a_huge_network_falls_back_to_the_24_around_us(self):
        routes = self.ROUTES.replace("00FFFFFF", "0000FFFF").replace("0001A8C0", "0000A8C0")  # a /16
        hosts = self.hosts(routes)
        self.assertEqual(len(hosts), 253)
        self.assertTrue(all(h.startswith("192.168.1.") for h in hosts))

    def test_another_interface_or_an_unreadable_route_falls_back_to_the_24_around_us(self):
        self.assertEqual(len(self.hosts(self.ROUTES, iface="wlan0")), 253)
        with mock.patch.object(hi, "open", side_effect=OSError("nope"), create=True):
            self.assertEqual(len(hi.lan_hosts("enp0s18", OUR_IP)), 253)


class LinkMonitorRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_probes_sweeps_while_nothing_answers_then_stops_on_request(self):
        class Selectable(FakeSock):
            def setblocking(self, flag):
                self.blocking = flag

            def fileno(self):
                return self.r_fd
        read_end, write_end = socket.socketpair()
        self.addCleanup(read_end.close)
        self.addCleanup(write_end.close)
        sock = Selectable()
        sock.r_fd = read_end.fileno()
        inv = hi.Inverter(INV_IP)
        inv.known_mac = INV_MAC
        links, stop = {}, asyncio.Event()
        monitor = hi.LinkMonitor([inv], OUR_MAC, OUR_IP, links, hosts=["192.168.1.9", "192.168.1.10"])
        with (mock.patch.object(hi, "LINK_PROBE_S", 0.01), mock.patch.object(hi, "SWEEP_REPLY_WAIT_S", 0),
              mock.patch.object(hi, "SWEEP_BACKOFF_S", (1000.0,))):
            task = asyncio.create_task(monitor.run(sock, stop))
            await wait_until(lambda: INV_IP in links and links[INV_IP].checked)   # a sweep was done
            swept = [socket.inet_ntoa(f[38:42]) for f in sock.sent]
            self.assertIn("192.168.1.9", swept)
            self.assertEqual(swept.count("192.168.1.9"), 1)          # and only one so far
            sock.incoming.append(arp_reply(INV_MAC, INV_IP))
            write_end.send(b"x")                                      # the socket becomes readable
            await wait_until(lambda: links[INV_IP].alive)
            stop.set()
            await asyncio.wait_for(task, 3)
        self.assertFalse(sock.blocking)


if __name__ == "__main__":
    unittest.main()
