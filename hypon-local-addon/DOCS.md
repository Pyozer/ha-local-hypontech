# Hypon Local Bridge

Read the live data of a Hypon micro-inverter **locally** (Wi-Fi collector
`COM-WF`, inverter `HMS-800W` and variants) and publish it to Home Assistant via
MQTT Discovery — **without touching DNS, DHCP or your router** — while keeping the
vendor app working.

> 🇫🇷 Version française : [`DOCS.fr.md`](DOCS.fr.md)

## How it works

The collector speaks clear-text MQTT (TCP 1883) to `hk.hypon.cloud`. On the Home
Assistant host network, this add-on:

1. **Targeted ARP** — makes **only the inverter** believe the network gateway is
   Home Assistant. All of the module's outgoing traffic then arrives on the HA host.
   No other device is affected.
2. **Redirect** — a surgical `iptables` rule sends the module's port 1883 to a local
   bridge; all of its other traffic (DNS, NTP…) is forwarded normally, so nothing
   else breaks.
3. **Bridge** — relays the connection byte-for-byte to the real Hypon cloud (the app
   keeps working), decodes the `invmonitor` frames and creates the Home Assistant
   sensors automatically.
4. **On shutdown** — immediately heals the inverter's ARP cache and removes every
   rule it added.

## Important requirements

- **Flat network (same L2 segment).** ARP does not cross a router or a VLAN. Home
  Assistant and the micro-inverter must be on the **same subnet** (e.g.
  `192.168.1.0/24`). This is the case for the vast majority of home installations. A
  Wi-Fi repeater / access point in bridge mode is fine.
- **Home Assistant OS / Supervised.** The add-on needs the host network
  (`host_network`) and the `NET_ADMIN` / `NET_RAW` capabilities. It therefore cannot
  come from the official store and is installed from a **custom add-on repository**
  (see the repository README).
- **An MQTT broker** (the *Mosquitto broker* add-on). Credentials are picked up
  automatically; otherwise set them manually (see options).

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ menu → Repositories**,
   add the URL of the Git repository that contains this add-on.
2. Install **Hypon Local Bridge**, then open the **Configuration** tab.
3. Set at least `inverter_ip`, start, and watch the **Log** tab.

## Options

| Option | Default | Purpose |
|---|---|---|
| `inverter_ip` | *(required)* | IP of the micro-inverter's Wi-Fi collector |
| `gateway_ip` | *(auto)* | Gateway IP spoofed towards the module; empty = default gateway detected |
| `interface` | *(auto)* | Host network interface; empty = detected from the default route |
| `relay_to_cloud` | `true` | Relay to the Hypon cloud (keeps the app working). `false` = 100% local |
| `cloud_host` | `gateway-…amazonaws.com` | AWS name of the cloud (avoids a loop). Change only if needed |
| `cloud_port` | `1883` | Cloud port |
| `listen_port` | `18883` | Local bridge port (redirect target). **Different from 1883** to avoid clashing with Mosquitto |
| `base_topic` | `hypon` | Prefix of the published state topics |
| `discovery_prefix` | `homeassistant` | MQTT Discovery prefix |
| `publish_raw` | `false` | Also publish raw frames as hex (`hypon/raw/*`) to keep decoding |
| `mqtt_host` | *(HA service)* | Manual MQTT broker; empty = provided by Home Assistant |
| `mqtt_port` / `mqtt_user` / `mqtt_password` | — | Manual MQTT credentials (if `mqtt_host` is set) |
| `debug` | `false` | Verbose logs |

### About `gateway_ip` (the "target address")

This is the address the add-on **impersonates towards the module**: the micro-inverter
will believe that this IP (normally your router, e.g. `192.168.1.254`) is on the Home
Assistant network card. Left empty, it is detected automatically from the host's
default route. Only change it if your real gateway is not the detected one.

## Verify it works

- Add-on **Log**: you should see `interface …`, `inverter MAC …`,
  `netfilter: :1883 from … -> local :18883`, then `invmonitor` frames.
- **Settings → Devices & Services → MQTT**: a *Hypontech* device with its sensors
  (power, PV voltages/currents, energy…) appears on its own.

## Troubleshooting

- **`inverter … did not answer ARP`**: wrong IP, or HA is not on the same L2 segment
  as the module (a VLAN/router sits between them).
- **Sensors do not appear** but frames arrive: MQTT issue. Under `host_network`, the
  name `core-mosquitto` is not always resolved; set `mqtt_host` to your Home
  Assistant LAN IP.
- **Port clash** when the bridge starts: change `listen_port` (keep it different
  from 1883).

## Safety and legality

The add-on only acts on **your** hardware, on **your** network, and targets the
**single IP of the micro-inverter**. It does not attack the cloud: it merely relays
the traffic the module already sends. On shutdown, the network returns to exactly its
previous state.
