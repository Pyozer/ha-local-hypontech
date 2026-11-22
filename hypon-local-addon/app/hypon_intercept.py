#!/usr/bin/env python3
"""Lean L2 interceptor for the Hypon COM-WF collector, for the Home Assistant add-on.

No bettercap, no DNS change, no router change. Standard library only.

What it does, on the host network (one flat L2 segment):

1. Poisons the ARP cache of the inverter ONLY, so the inverter believes the LAN
   gateway is us. All of its off-subnet traffic (including the MQTT connection to
   hk.hypon.cloud) is then sent to this host.
2. Adds a surgical iptables REDIRECT so the inverter's TCP :1883 lands on the local
   bridge port; everything else the inverter sends is forwarded normally (IP
   forwarding on), so it keeps its DNS / NTP / cloud for every other purpose.
3. Runs the existing hypon_bridge (relay to the real cloud + decode + MQTT Discovery).
4. On shutdown, heals the inverter's ARP cache and removes every rule it added.
5. Meanwhile, watches ARP (LinkMonitor) to tell the bridge whether each inverter is alive on
   its configured IP or has been seen under another one, which is what lets a lost inverter
   be told apart from one that simply sleeps.

One or several inverters can be handled (HYPON_INV_IPS, comma separated): each one gets its
own ARP spoofing and its own rules, all feeding the same bridge, which tells them apart by serial
number. Only the inverters are affected. Nothing else on the network is touched.
"""
import asyncio
import ipaddress
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time

import hypon_bridge
from hypon_status import Link
from hypon_store import StateStore

LOG = logging.getLogger("hypon.intercept")

ETH_P_ARP = 0x0806
BROADCAST = b"\xff\xff\xff\xff\xff\xff"
ZERO_MAC = b"\x00\x00\x00\x00\x00\x00"
ARP_INTERVAL_S = 2.0  # how often we re-assert the spoofed gateway entry
INVERTER_RETRY_S = 25.0  # pause between ARP lookups while the inverter is unreachable (asleep)
INVERTER_WARN_EVERY = 20  # repeat the "still waiting" warning every N failed lookups (~10 min)
LINK_PROBE_S = 30.0  # how often each inverter is asked "are you there?" (ARP request)
# While an inverter does not answer, the LAN is searched for its MAC: at once, then after each of
# these waits (the last one repeats), so a night of silence costs one search every 5 minutes.
SWEEP_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
SWEEP_VALID_S = 2 * SWEEP_BACKOFF_S[-1]  # a search covers the network for this long
SWEEP_REPLY_WAIT_S = 3.0  # time given to the hosts to answer a sweep
ALIVE_TTL_S = 120.0  # an ARP sign of life from the inverter counts for this long
MOVED_TTL_S = 900.0  # a sighting of the inverter under another IP counts for this long
MAX_SWEEP_PREFIX = 22  # never sweep a network bigger than a /22 (1022 hosts): fall back to a /24


# --------------------------------------------------------------------------- configuration

def parse_ips(raw):
    """IPs from comma / space / newline separated text, in order (duplicates are kept: see discover)."""
    return [part for part in re.split(r"[\s,;]+", raw or "") if part]


# --------------------------------------------------------------------------- host discovery

def default_route():
    """Return (iface, gateway_ip) from the kernel default route, or (None, None)."""
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000":
                    gw = socket.inet_ntoa(struct.pack("<I", int(p[2], 16)))
                    return p[0], gw
    except OSError as exc:
        LOG.warning("cannot read /proc/net/route: %s", exc)
    return None, None


def iface_mac(iface):
    with open(f"/sys/class/net/{iface}/address", encoding="ascii") as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def our_ip_towards(target_ip):
    """Source IP the kernel would use to reach target_ip (our LAN address)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# --------------------------------------------------------------------------- ARP

def build_arp(op, s_mac, s_ip, t_mac, t_ip, eth_dst):
    eth = eth_dst + s_mac + b"\x08\x06"
    arp = struct.pack(">HHBBH", 1, 0x0800, 6, 4, op)
    arp += s_mac + socket.inet_aton(s_ip) + t_mac + socket.inet_aton(t_ip)
    return eth + arp


def open_arp_socket(iface):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP))
    s.bind((iface, 0))
    return s


def resolve_mac(sock, our_mac, our_ip, target_ip, tries=5, timeout=1.0):
    """Send ARP requests for target_ip and return its MAC, or None."""
    req = build_arp(1, our_mac, our_ip, ZERO_MAC, target_ip, BROADCAST)
    want = socket.inet_aton(target_ip)
    sock.settimeout(timeout)
    for _ in range(tries):
        sock.send(req)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = sock.recv(2048)
            except socket.timeout:
                break
            if len(frame) < 42 or frame[12:14] != b"\x08\x06":
                continue
            arp = frame[14:]
            op = struct.unpack(">H", arp[6:8])[0]
            if op == 2 and arp[14:18] == want:
                return arp[8:14]  # sender hardware address
    return None


def fmt_mac(mac):
    return ":".join(f"{b:02x}" for b in mac)


def lan_hosts(iface, our_ip):
    """Addresses of the local network (other than ours), to search it for an inverter.

    Taken from the kernel's route for `iface`; a network bigger than a /22 or an unreadable
    route falls back to the /24 around `our_ip`."""
    network = ipaddress.ip_network(f"{our_ip}/24", strict=False)
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) > 7 and p[0] == iface and p[1] != "00000000" and p[2] == "00000000":
                    dest = ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<I", int(p[1], 16))))
                    mask = ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<I", int(p[7], 16))))
                    found = ipaddress.ip_network(f"{dest}/{mask}", strict=False)
                    if found.prefixlen >= MAX_SWEEP_PREFIX and ipaddress.ip_address(our_ip) in found:
                        network = found
                    break
    except (OSError, ValueError) as exc:
        LOG.warning("cannot read /proc/net/route (%s): sweeping %s", exc, network)
    return [str(host) for host in network.hosts() if str(host) != our_ip]


def parse_mac(text):
    """Bytes of a 'aa:bb:cc:dd:ee:ff' address, None if it is not one."""
    try:
        mac = bytes.fromhex((text or "").replace(":", ""))
    except ValueError:
        return None
    return mac if len(mac) == 6 else None


def parse_arp(frame):
    """(sender MAC, sender IP) of an Ethernet frame carrying ARP, else None."""
    if len(frame) < 42 or frame[12:14] != b"\x08\x06":
        return None
    arp = frame[14:]
    return arp[8:14], socket.inet_ntoa(arp[14:18])


# --------------------------------------------------------------------------- netfilter

def run_ipt(args):
    cmd = ["iptables", "-w"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        LOG.debug("iptables %s -> %s", " ".join(args), res.stderr.strip())
    return res.returncode == 0


class NetRules:
    """Surgical, fully reversible netfilter changes scoped to one inverter IP."""

    def __init__(self, inverter_ip, listen_port):
        self.inv = inverter_ip
        self.port = str(listen_port)
        self._redirect = ["-t", "nat", "PREROUTING", "-s", self.inv,
                          "-p", "tcp", "--dport", "1883",
                          "-j", "REDIRECT", "--to-ports", self.port]
        self._fwd_out = ["-t", "filter", "FORWARD", "-s", self.inv, "-j", "ACCEPT"]
        self._fwd_in = ["-t", "filter", "FORWARD", "-d", self.inv, "-j", "ACCEPT"]
        self._ip_forward_prev = None

    def _add(self, rule):
        # -I inserts at the top so our ACCEPT wins over a restrictive policy.
        run_ipt([rule[0], rule[1], "-I"] + rule[2:])

    def _del(self, rule):
        run_ipt([rule[0], rule[1], "-D"] + rule[2:])

    def apply(self):
        current = None
        try:
            with open("/proc/sys/net/ipv4/ip_forward") as f:
                current = f.read().strip()
        except OSError:
            pass
        if current == "1":
            # Normal case on HA OS: Docker already enables forwarding on the host,
            # and host_network means we inherit it. Nothing to change.
            LOG.info("ip_forward already enabled on the host")
        else:
            try:
                with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                    f.write("1\n")
                self._ip_forward_prev = current  # restore only what we changed
                LOG.info("ip_forward enabled")
            except OSError as exc:
                LOG.warning("ip_forward is off and could not be enabled (%s); the "
                            "inverter's non-MQTT traffic (DNS/NTP) may be disrupted. "
                            "Enable net.ipv4.ip_forward=1 on the host if needed.", exc)
        self._add(self._fwd_out)
        self._add(self._fwd_in)
        self._add(self._redirect)
        LOG.info("netfilter: :1883 from %s -> local :%s, forwarding on", self.inv, self.port)

    def revert(self):
        self._del(self._redirect)
        self._del(self._fwd_in)
        self._del(self._fwd_out)
        if self._ip_forward_prev is not None:
            try:
                with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                    f.write(self._ip_forward_prev + "\n")
            except OSError:
                pass
        LOG.info("netfilter rules removed")


# --------------------------------------------------------------------------- orchestration

class Inverter:
    """One intercepted inverter: its address, its MAC once known, and its netfilter rules."""

    def __init__(self, ip):
        self.ip = ip
        self.mac = None        # resolved during this run: what the spoofing is aimed at
        self.known_mac = None  # also remembered from a previous run: how the inverter is recognised
        self.rules = None


class LinkMonitor:
    """Tells the bridge what the network says about each inverter.

    Silence alone cannot tell an inverter that is off (night, storm) from one whose IP address
    changed. The network can: a powered inverter answers ARP, at its configured IP or at
    another one if it moved, always with the same MAC; an inverter that is off is nowhere. This
    watches ARP on a socket of its own, asks each inverter "are you there?" now and then and,
    while one does not answer, searches the whole LAN for its MAC. It keeps `links[ip]` up to
    date (see Link)."""

    def __init__(self, inverters, our_mac, our_ip, links, hosts=(), clock=time.monotonic):
        self.inverters = inverters
        self.our_mac = our_mac
        self.our_ip = our_ip
        self.links = links
        self.hosts = list(hosts)  # addresses searched for a moved inverter
        self._clock = clock
        self._alive_at = {}  # configured IP -> when the inverter last spoke from that address
        self._moved = {}     # configured IP -> (IP seen instead, when)
        self._swept_at = None  # when the last complete search of the LAN ended
        self._sweeps = 0       # searches made since the inverter stopped answering
        self._last_sweep = None  # when the last one started

    def handle_frame(self, frame):
        arp = parse_arp(frame)
        if arp is None:
            return
        mac, ip = arp
        if ip == "0.0.0.0":
            return  # an address-conflict probe: it carries no sender address
        now = self._clock()
        for inv in self.inverters:
            if inv.known_mac is None or mac != inv.known_mac:
                continue
            if ip == inv.ip:
                self._alive_at[inv.ip] = now
                self._moved.pop(inv.ip, None)
            else:
                self._moved[inv.ip] = (ip, now)

    def _alive(self, inv, now):
        alive_at = self._alive_at.get(inv.ip)
        return alive_at is not None and now - alive_at <= ALIVE_TTL_S

    def refresh(self):
        now = self._clock()
        swept = self._swept_at is not None and now - self._swept_at <= SWEEP_VALID_S
        for inv in self.inverters:
            moved = self._moved.get(inv.ip)
            alive = self._alive(inv, now)
            self.links[inv.ip] = Link(
                mac=fmt_mac(inv.known_mac) if inv.known_mac else None,
                alive=alive,
                moved_to=moved[0] if moved and now - moved[1] <= MOVED_TTL_S else None,
                # "not seen" only means "absent" if we really looked, and we can only look for
                # an inverter whose MAC we know.
                checked=inv.known_mac is not None and (alive or swept))

    def _request(self, sock, ip):
        sock.send(build_arp(1, self.our_mac, self.our_ip, ZERO_MAC, ip, BROADCAST))

    def probe(self, sock):
        for inv in self.inverters:
            try:
                self._request(sock, inv.ip)
            except OSError as exc:
                LOG.warning("ARP probe failed: %s", exc)

    def needs_sweep(self):
        """True while a known inverter does not answer at its configured IP."""
        now = self._clock()
        return any(inv.known_mac is not None and not self._alive(inv, now) for inv in self.inverters)

    def sweep_due(self):
        """True when the LAN should be searched now: at once when an inverter stops answering,
        then after 30 s, 1 min, 2 min and every 5 min. An inverter that answers again starts
        the sequence over."""
        if not self.needs_sweep():
            self._sweeps, self._last_sweep = 0, None
            return False
        if self._last_sweep is None:
            return True
        wait = SWEEP_BACKOFF_S[min(self._sweeps - 1, len(SWEEP_BACKOFF_S) - 1)]
        return self._clock() - self._last_sweep >= wait

    async def sweep(self, sock):
        """Ask every address of the LAN who it is: an inverter that moved shows its MAC again."""
        self._last_sweep = self._clock()
        self._sweeps += 1
        for count, ip in enumerate(self.hosts, 1):
            try:
                self._request(sock, ip)
            except OSError as exc:
                LOG.warning("ARP sweep failed: %s", exc)
                return
            if count % 64 == 0:
                await asyncio.sleep(0)  # let the replies through
        await asyncio.sleep(SWEEP_REPLY_WAIT_S)
        self._swept_at = self._clock()

    def drain(self, sock):
        """Handle every frame waiting on the (non-blocking) socket."""
        while True:
            try:
                frame = sock.recv(2048)
            except (BlockingIOError, socket.timeout):
                break
            self.handle_frame(frame)
        self.refresh()

    async def run(self, sock, stop):
        loop = asyncio.get_running_loop()
        sock.setblocking(False)
        loop.add_reader(sock.fileno(), self.drain, sock)
        try:
            while not stop.is_set():
                self.probe(sock)
                if self.sweep_due():
                    await self.sweep(sock)
                self.refresh()
                try:
                    await asyncio.wait_for(stop.wait(), LINK_PROBE_S)
                except asyncio.TimeoutError:
                    pass
        finally:
            loop.remove_reader(sock.fileno())


class Interceptor:
    def __init__(self):
        self.inverters = [Inverter(ip) for ip in parse_ips(os.environ.get("HYPON_INV_IPS"))]
        self._applied = []  # inverters whose rules are in place, in the order they were applied
        self.store = None   # persistent state, opened by run()
        self.gw_ip = os.environ.get("HYPON_GW_IP", "").strip()
        self.iface = os.environ.get("HYPON_IFACE", "").strip()
        self.listen_port = int(os.environ.get("HYPON_LISTEN_PORT", "18883"))
        self.sock = None
        self.our_mac = None
        self.our_ip = None
        self.gw_mac = None
        self._stop = asyncio.Event()

    def discover(self):
        if not self.inverters:
            raise SystemExit("HYPON_INV_IPS (inverter IPs) is required")
        checks = [("inverter_ips", inv.ip) for inv in self.inverters] + [("gateway_ip", self.gw_ip)]
        for label, value in checks:
            if not value:
                continue
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise SystemExit(f"{label} {value!r} is not a valid IPv4 address "
                                 "(expected something like 192.168.1.87)") from None
        seen = set()
        for inv in self.inverters:
            if inv.ip in seen:
                raise SystemExit(f"inverter_ips lists {inv.ip} more than once: "
                                 "list each micro-inverter once")
            seen.add(inv.ip)
        route_iface, route_gw = default_route()
        if not self.iface:
            self.iface = route_iface
        if not self.gw_ip:
            self.gw_ip = route_gw
        if not self.iface:
            raise SystemExit("could not determine the LAN interface; set 'interface'")
        if not self.gw_ip:
            raise SystemExit("could not determine the gateway IP; set 'gateway_ip'")
        self.our_mac = iface_mac(self.iface)
        self.our_ip = our_ip_towards(self.inverters[0].ip)
        self.sock = open_arp_socket(self.iface)
        LOG.info("interface %s  us %s/%s  gateway %s  inverter%s %s",
                 self.iface, self.our_ip, fmt_mac(self.our_mac), self.gw_ip,
                 "s" if len(self.inverters) > 1 else "", ", ".join(inv.ip for inv in self.inverters))
        # The inverter MACs are resolved later (see _wait_for_inverter): an inverter is powered by
        # the panels and does not answer ARP at night, which must not stop the add-on.
        self.gw_mac = resolve_mac(self.sock, self.our_mac, self.our_ip, self.gw_ip)
        LOG.info("gateway MAC %s", fmt_mac(self.gw_mac) if self.gw_mac else "unknown")

    async def _wait_for_inverter(self, inv):
        """Resolve the inverter MAC, retrying for as long as it takes (night, power cut...)."""
        failures = 0
        while not self._stop.is_set():
            mac = await asyncio.to_thread(
                resolve_mac, self.sock, self.our_mac, self.our_ip, inv.ip)
            if mac:
                inv.mac = inv.known_mac = mac
                if self.store is not None:
                    self.store.set_mac(inv.ip, fmt_mac(mac))
                LOG.info("inverter %s MAC %s%s", inv.ip, fmt_mac(mac),
                         f" (answered after {failures} failed lookups)" if failures else "")
                return True
            if failures % INVERTER_WARN_EVERY == 0:
                LOG.warning("inverter %s does not answer ARP: probably switched off (no light on the "
                            "panels), waiting for it. If it never shows up, check the IP and that "
                            "the add-on is on the same L2 network.", inv.ip)
            failures += 1
            try:
                await asyncio.wait_for(self._stop.wait(), INVERTER_RETRY_S)
            except asyncio.TimeoutError:
                pass
        return False

    async def _intercept(self):
        """Each inverter is handled on its own: one that sleeps never holds back the others."""
        await asyncio.gather(*(self._intercept_one(inv) for inv in self.inverters))

    async def _intercept_one(self, inv):
        """Start spoofing + redirecting as soon as this inverter is reachable."""
        if not await self._wait_for_inverter(inv):
            return
        inv.rules = NetRules(inv.ip, self.listen_port)
        inv.rules.apply()
        self._applied.append(inv)
        await self.arp_loop(inv)

    async def _monitor(self, monitor):
        """Feed the bridge with what the network says about the inverters (own raw socket, so
        the lookups of _wait_for_inverter and the monitor never steal each other's replies)."""
        try:
            sock = open_arp_socket(self.iface)
        except OSError as exc:
            LOG.warning("cannot watch ARP (%s): a changed inverter IP will not be recognised", exc)
            return
        try:
            await monitor.run(sock, self._stop)
        finally:
            sock.close()

    def _spoof_frame(self, inv):
        # Tell the inverter: gateway_ip is at OUR mac.
        return build_arp(2, self.our_mac, self.gw_ip, inv.mac, inv.ip, inv.mac)

    def _heal_frame(self, inv):
        if not self.gw_mac or not inv.mac:
            return None
        # Restore the truth: gateway_ip is at the REAL gateway mac.
        return build_arp(2, self.gw_mac, self.gw_ip, inv.mac, inv.ip, inv.mac)

    async def arp_loop(self, inv):
        frame = self._spoof_frame(inv)
        while not self._stop.is_set():
            try:
                self.sock.send(frame)
            except OSError as exc:
                LOG.warning("ARP send failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), ARP_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    def heal(self):
        for inv in self.inverters:
            frame = self._heal_frame(inv)
            if not frame:
                continue
            for _ in range(5):
                try:
                    self.sock.send(frame)
                except OSError:
                    break
            LOG.info("inverter %s ARP cache restored to the real gateway", inv.ip)

    async def run(self):
        self.discover()
        # The bridge listens on our chosen port; the redirect feeds it. It starts right away so
        # the sensors stay available even while the inverter is asleep.
        os.environ.setdefault("HYPON_LISTEN_HOST", "0.0.0.0")
        os.environ["HYPON_LISTEN_PORT"] = str(self.listen_port)
        cfg = hypon_bridge.Config()
        # The state survives restarts: the MAC of each inverter is how it is recognised even
        # when it is asleep or has changed IP, and it tells the bridge what it served before.
        self.store = StateStore.open(cfg.data_dir)
        for inv in self.inverters:
            inv.known_mac = parse_mac(self.store.get_mac(inv.ip))
        links = {}
        monitor = LinkMonitor(self.inverters, self.our_mac, self.our_ip, links,
                              hosts=lan_hosts(self.iface, self.our_ip))
        try:
            await asyncio.gather(hypon_bridge.main_async(cfg, self.store, links),
                                 self._intercept(), self._monitor(monitor))
        except asyncio.CancelledError:
            pass
        finally:
            self._stop.set()
            self.heal()
            # Last in, first out: the first inverter applied is the one that may have enabled
            # ip_forward, so it must be the last to put it back.
            for inv in reversed(self._applied):
                inv.rules.revert()
            if self.sock:
                self.sock.close()

    def request_stop(self):
        self._stop.set()


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("HYPON_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    interceptor = Interceptor()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(interceptor.run())

    def _stop(*_):
        LOG.info("stopping...")
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
