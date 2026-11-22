# LocalHyponTech — Home Assistant add-on repository

**English** · [Français](README.fr.md)

Custom Home Assistant add-on repository containing **Hypon Local Bridge**: read a
Hypon micro-inverter (`COM-WF` collector, `HMS-800W` and variants) **locally**,
**without changing your DNS or router settings**, with automatic Home Assistant
publishing (MQTT Discovery) and transparent relaying to the Hypon cloud (the vendor
app keeps working).

> **Unofficial project.** This add-on is an independent, community project. It is not
> affiliated with, endorsed by or supported by Hypontech. Product and company names
> belong to their respective owners and are used here only to say which devices the
> add-on works with.

## Requirements

- Home Assistant OS / Supervised.
- HA and the micro-inverter on the **same network** (same subnet, no VLAN or
  router between them).
- An MQTT broker (the Mosquitto add-on) — credentials are picked up automatically.

## Quick start

### 1. Add this repository to Home Assistant

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FPyozer%2Fha-local-hypontech)

Or add it by hand: **Settings → Add-ons → Add-on Store → ⋮ menu → Repositories**, then add:

```
https://github.com/Pyozer/ha-local-hypontech
```

### 2. Find the inverter's IP address, and fix it

The inverter's Wi-Fi module gets its IP from your router. **Do this in daylight**: the
inverter is powered by the panels and is offline at night.

**Option A — your router (easiest).** In its admin page or app, open the list of
connected devices (or DHCP leases). The inverter usually shows up as `Espressif`,
`Hypon` or `COM-WF`, sometimes with no name at all.

**Option B — scan your network.** The inverter serves a small page at `/local_web`.
This command prints the IP of the device that answers with the Hypon page (set `NET`
to your subnet if it isn't `192.168.1`):

```bash
NET=192.168.1; (for i in $(seq 1 254); do (curl -s -m 2 "http://$NET.$i/local_web" | grep -q Hypon && echo "Inverter: $NET.$i") & done; wait)
```

It works on macOS, Linux, and in the Home Assistant **Terminal & SSH** add-on. To
double-check, open `http://<ip>/local_web`: you should see *Device Information* with
model `COM-WF`.

> **macOS:** if a browser can't reach the address while `curl` can, allow it under
> **System Settings → Privacy & Security → Local Network**.

**Then reserve this IP for the inverter** (recommended, otherwise the add-on loses it if
the router hands out a new one). In your router's DHCP settings, look for *DHCP
reservation*, *static lease* or *IP binding* and pick the inverter by its MAC address.
Do it on the router, not on the inverter.

### 3. Install and start

In the Add-on Store, open **Hypon Local Bridge** → **Install** → **Configuration** tab,
enter the IP in `inverter_ips`, then start the add-on and watch the **Log** tab. A
*Hypontech* device with its sensors then appears under **Settings → Devices & Services
→ MQTT**. With several micro-inverters, list all their IPs in `inverter_ips`,
separated by commas: each one gets its own device.

**It doesn't work?** In daylight, `does not answer ARP` in the log means a wrong IP
(redo step 2) or a different network/VLAN. More help in the add-on
[`DOCS.md`](hypon-local-addon/DOCS.md).

## How it works

The module sends its measurements over clear-text MQTT (TCP 1883) to
`hk.hypon.cloud`. On the Home Assistant host network, the add-on:

1. poisons the ARP cache of **only** the micro-inverter (it believes the gateway = HA);
2. redirects its port 1883 to a local bridge via `iptables`, and forwards the rest;
3. relays to the real cloud + decodes + publishes the sensors into Home Assistant;
4. restores everything cleanly on shutdown.

It is pure Python (standard library only) and idles at ~0 % CPU. See
[`DOCS.md`](hypon-local-addon/DOCS.md) for all options and troubleshooting.

## Status

Tested on a real Home Assistant installation.

## License

[MIT](LICENSE)
