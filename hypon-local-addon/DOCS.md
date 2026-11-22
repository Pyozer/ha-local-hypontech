# Hypon Local Bridge

Read the live data of a Hypon micro-inverter **locally** (Wi-Fi collector
`COM-WF`, inverter `HMS-800W` and variants) and publish it to Home Assistant via
MQTT Discovery — **without changing your DNS or router settings** — while keeping the
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
3. Set at least `inverter_ips`, start, and watch the **Log** tab.

### Finding the inverter's IP

Look in daylight (the inverter is offline at night): in your router's device list
(`Espressif`, `Hypon` or `COM-WF`), or with the one-line scan command in the
[repository README](https://github.com/Pyozer/ha-local-hypontech#quick-start).

Then **reserve that IP for the inverter in your router's DHCP settings** (static
lease), so it never changes. If the add-on keeps logging `does not answer ARP` in
daylight, the IP has probably changed: find it again.

### Several micro-inverters

List all their IPs in `inverter_ips`, separated by commas (e.g.
`192.168.1.87, 192.168.1.88`; then restart the add-on. Each micro-inverter gets its own *Hypontech* device in
Home Assistant, identified by its serial number, so nothing is mixed up. Adding one
never changes the entities of the others, and one going to sleep does not affect the
rest. All of them must be on the same network as Home Assistant.

## Options

| Option | Default | Purpose |
|---|---|---|
| `inverter_ips` | `192.168.1.` | IP of the micro-inverter's Wi-Fi collector, or several separated by commas (see *Several micro-inverters*). Prefilled with the usual subnet: just add the last number (e.g. `192.168.1.87`) |
| `gateway_ip` | *(auto)* | Gateway IP spoofed towards the module; empty = default gateway detected |
| `interface` | *(auto)* | Host network interface; empty = detected from the default route |
| `relay_to_cloud` | `true` | Relay to the Hypon cloud (keeps the app working). `false` = 100% local |
| `cloud_host` | `hk.hypon.cloud` | Hypon cloud host to relay to. Change only if needed |
| `cloud_port` | `1883` | Cloud port |
| `listen_port` | `18883` | Local bridge port (redirect target). **Different from 1883** to avoid clashing with Mosquitto |
| `base_topic` | `hypon` | Prefix of the published state topics |
| `discovery_prefix` | `homeassistant` | MQTT Discovery prefix |
| `publish_raw` | `false` | Also publish raw frames as hex (`hypon/raw/*`) to keep decoding |
| `offline_grace` | `300` | Seconds of silence tolerated before looking at the network to conclude (off or lost, see below). Slow reporters get more time automatically. `0` = immediately |
| `mqtt_host` | *(HA service)* | Manual MQTT broker; empty = provided by Home Assistant |
| `mqtt_port` / `mqtt_user` / `mqtt_password` | — | Manual MQTT credentials (if `mqtt_host` is set) |
| `debug` | `false` | Verbose logs |

### About `gateway_ip` (the "target address")

This is the address the add-on **impersonates towards the module**: the micro-inverter
will believe that this IP (normally your router, e.g. `192.168.1.254`) is on the Home
Assistant network card. Left empty, it is detected automatically from the host's
default route. Only change it if your real gateway is not the detected one.

### Night, silence and availability

The micro-inverter and its Wi-Fi collector are powered by the panels: with no light (night,
storm, heavy shade) they are off and say nothing. An inverter whose IP address changed also
says nothing. Silence alone is ambiguous, and the position of the sun would not settle it
(the sun is "up" during a storm). The **network** does: an inverter that is powered answers
ARP, at its IP or, if its address changed, at another one, always with the same MAC address;
an inverter that is off is nowhere to be seen. After `offline_grace` seconds without a
frame, the add-on looks, and gives every inverter a **status**:

| Status | Meaning | Entities |
|---|---|---|
| `online` | Frames are arriving | Live values |
| `sleeping` | Silent and **nowhere on the network**: off for lack of light | Available, production **0**, totals kept |
| `unreachable` | It answers the network but sends nothing to the add-on | **Unavailable** |
| `ip_changed` | Silent, and its MAC address was seen under **another IP** | **Unavailable** |

So at night, or under a storm in the middle of the day, the inverter reads **0 W** and
never "unavailable". It becomes unavailable only when there is positive evidence that it
is alive but not reachable as configured.

How the network is looked at: every 30 seconds the add-on asks each inverter "are you
there?" (ARP). While one does not answer, it also asks **every address of your local
network** (one ARP request each) who it is, to find that inverter's MAC address again
wherever it went. It does so at once, then after 30 seconds, 1 minute and 2 minutes, then
only every 5 minutes: a night of silence costs one search every 5 minutes. That is a few
kilobytes, only while the inverter is silent, and nothing leaves your network. As soon as
the inverter answers again, the sequence starts over.

The usual rhythm of an inverter is learned: one that reports slowly is given three times
its period (never less than `offline_grace`). The collector often drops and reconnects
around sunset: reconnections within the delay change nothing, and neither does a dropped
connection alone, only the silence that follows.

**Why unavailable and not 0 W when it is lost?** A `0` would claim the inverter produces
nothing, which would be false. `unavailable` honestly says that nothing is known, and
never leaves a stale value (such as the last production before a power cut) on screen. The
totals are not reset: they only increase and the first frame after the outage brings the
right value.

**Limits.** Being "off" is concluded from the inverter being absent from the network, so an
inverter that is producing but **cannot be seen at all** (its Wi-Fi is down, for example, or
it is on another network) reads 0 W. The add-on can only vouch for what the network shows.
It also needs the inverter's MAC address, which it learns the first time it intercepts it;
without it (or if the ARP monitor cannot run) a silent inverter is reported `unreachable`
rather than guessing 0 W.

Everything resumes with the first frame. The add-on remembers the inverters it served
(`/data`), so after a restart or a power cut it never keeps showing a value that nobody
confirms any more: an inverter that was online gets one full delay to report again, then
the same rules apply.

#### Entities

Each inverter is a *Hypontech* device with its measurements (power, PV voltage / current /
power, grid, temperature, energy) and these **diagnostic** entities, which stay available
even when the measurements are not, so they can explain why:

| Entity | Content |
|---|---|
| **Statut** | `online`, `sleeping`, `unreachable` or `ip_changed` |
| **Connexion** | *on* only while the inverter is `online` |
| **Dernière trame** | When the last frame arrived (timestamp) |
| **Adresse IP** | Where the inverter really is: the address it talks from, or its new one after an `ip_changed` |
| **Adresse IP configurée** | The address the add-on expects it at (the one in `inverter_ips`) |
| **Adresse MAC** | Its hardware address (disabled by default, enable it if you need it) |

To be warned when the address changes, an automation can trigger when **Statut** becomes
`ip_changed`, or compare the two addresses, for instance with the template condition
`{{ states('sensor.hypon_ip_address') != states('sensor.hypon_configured_ip') }}`
(adjust the entity ids to yours), and put the new address in the notification.

## Verify it works

- Add-on **Log**: you should see `interface …`, `inverter MAC …`,
  `netfilter: :1883 from … -> local :18883`, then `invmonitor` frames.
- **Settings → Devices & Services → MQTT**: a *Hypontech* device with its sensors
  (power, PV voltages/currents, energy…) appears on its own.

## Troubleshooting

- **`inverter … does not answer ARP`**: normal at night, the inverter is switched off
  and the add-on waits for it. If it still shows up in daylight: wrong IP, or HA is
  not on the same L2 segment as the module (a VLAN/router sits between them).
- **Status `ip_changed`**: the inverter got another address (after a power cut, for
  instance). Read it in the **Adresse IP** sensor, put it in `inverter_ips`, restart,
  and reserve a fixed address for it in your router so it never moves again.
- **Status `unreachable`**: the inverter is on the network but its traffic no longer
  reaches the add-on. Restart the add-on to redo the interception, and check that
  `inverter_ips` is right.
- **0 W although the inverter produces**: the add-on does not see it on the network at
  all (see *Limits* above). Check its Wi-Fi and that it is on the same network as Home
  Assistant.
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

**Not an official product.** This add-on is an independent, community project, not
affiliated with, endorsed by or supported by Hypontech. Product and company names
belong to their respective owners and are used only to say which devices it works with.
