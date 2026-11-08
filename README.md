# LocalHyponTech — Home Assistant add-on repository

**English** · [Français](README.fr.md)

Custom Home Assistant add-on repository containing **Hypon Local Bridge**: read a
Hypon micro-inverter (`COM-WF` collector, `HMS-800W` and variants) **locally**,
**without touching DNS or your router**, with automatic Home Assistant publishing
(MQTT Discovery) and transparent relaying to the Hypon cloud (the vendor app keeps
working).

Lightweight version: no bettercap. The targeted ARP and the redirect are done in pure
Python (standard library) + one `iptables` rule.

## How it works

The module sends its measurements over clear-text MQTT (TCP 1883) to
`hk.hypon.cloud`. On the Home Assistant host network, the add-on:

1. poisons the ARP cache of **only** the micro-inverter (it believes the gateway = HA);
2. redirects its port 1883 to a local bridge via `iptables`, and forwards the rest;
3. relays to the real cloud + decodes + publishes the sensors into Home Assistant;
4. restores everything cleanly on shutdown.

See the add-on [`DOCS.md`](hypon-local-addon/DOCS.md) for options and troubleshooting.

## Add this repository to Home Assistant

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FPyozer%2Fha-local-hypontech)

The button above opens your Home Assistant instance with this repository pre-filled (it asks
for the address of your instance the first time). Or add it by hand:
**Settings → Add-ons → Add-on Store → ⋮ menu → Repositories**, then add:

```
https://github.com/Pyozer/ha-local-hypontech
```

Then install **Hypon Local Bridge**, set the micro-inverter IP, and start.

## Requirements

- Home Assistant OS / Supervised.
- HA and the micro-inverter on the **same L2 segment** (same subnet, no VLAN or
  router between them).
- An MQTT broker (the Mosquitto add-on) — credentials are picked up automatically.

## Repository layout

```
.
├── repository.yaml
└── hypon-local-addon/
    ├── config.yaml
    ├── Dockerfile
    ├── run.sh
    ├── DOCS.md / DOCS.fr.md
    └── app/
        ├── hypon_intercept.py   # targeted ARP + iptables + lifecycle
        └── hypon_bridge.py      # MQTT bridge (cloud relay + decode + Discovery)
```

## Status

Tested on a real Home Assistant installation.

## License

[MIT](LICENSE)
