# Changelog

## 0.5.1 - 2026-11-22

### Added
- An icon and a logo for the add-on, shown by Home Assistant in the add-on list and on
  the add-on page.

### Documentation
- State that this is an unofficial, community project, not affiliated with Hypontech
  (READMEs and add-on docs).

## 0.5.0 - 2026-11-22

Home Assistant now only shows what the add-on can vouch for: a silent inverter that is off
reads 0 W, one that is lost is unavailable, and a frozen value never stays on screen.

### Fixed
- **A stale value could stay on screen for hours.** The last production (e.g. 87 W)
  was kept as long as the add-on had not seen the inverter disconnect, which never
  happens after a power cut or a restart: the broker kept the retained value while the
  inverter, back under another IP address, was talking to nobody. Silence is now
  detected by the clock, whatever the connection does.

### Added
- **Status** diagnostic sensor per inverter: `online`, `sleeping`, `unreachable` or
  `ip_changed`. See *Night, silence and availability* in the docs.
- Telling an inverter that is off (night, storm, shade: **0 W**, entities stay available)
  from one that is lost (**unavailable**) with what the network says, not the sun: an
  inverter that answers ARP is not off, and one whose MAC address shows up under another
  IP is reported `ip_changed`, with its new address. While an inverter does not answer,
  the add-on asks the addresses of the local network who they are (ARP) to find it
  again: at once, then after 30 s, 1 min, 2 min, then every 5 min, so a night of silence
  stays quiet.
- Diagnostic sensors **Dernière trame** (time of the last frame), **Adresse IP** (where
  the inverter really is), **Adresse IP configurée** (where the add-on expects it, so an
  automation can compare the two) and **Adresse MAC** (disabled by default).
- The add-on remembers the inverters it served and their MAC address in `/data`, so a
  restart no longer leaves stale retained values behind.
- The entities are published again whenever the MQTT broker reconnects.

### Changed
- **Breaking:** measurements become *unavailable* when the inverter is `unreachable` or
  `ip_changed` (they used to keep the last value forever). When it is off they still
  read 0 W and stay available. The production is also `null` on the
  `<base_topic>/<sn>/state` topic in those two statuses.
- **Breaking:** `offline_grace` is now the silence tolerated before the network is
  looked at to conclude (it is no longer counted from a dropped connection), and is
  extended automatically for an inverter that reports slowly (three times its usual
  period).
- The `<base_topic>/<sn>/availability` topic is back, now `online` / `offline`
  depending on whether the measurements can be trusted; measurement entities depend on
  it and on the add-on's own status.
- `<base_topic>/<sn>/state` carries a `last_seen` timestamp.

### Upgrading
- The first start after the update has no memory of the previous run: a value retained
  by the previous version stays on screen until the inverter reports (or until the
  silence is concluded as above). Nothing to do.

## 0.4.0 - 2026-11-17

### Added
- Support for several micro-inverters. Each one is intercepted on its own (ARP
  spoofing and `iptables` rules) and appears as its own *Hypontech* device in
  Home Assistant, identified by its serial number. One going to sleep does not
  affect the others.
- Startup checks on the IP list: invalid addresses and duplicates are refused
  with a clear message.

### Changed
- **Breaking:** the `inverter_ip` option is renamed `inverter_ips`. It takes one
  IP or several separated by commas (spaces optional), e.g.
  `192.168.1.87, 192.168.1.88`. Existing installations must set it again after
  updating, otherwise the add-on will not start.

### Fixed
- A dropped collector connection no longer marks every inverter as offline, only
  the ones seen on that connection.

### Documentation
- Step-by-step quick start in the README, with a more reliable command to find
  the inverter's IP and a tip to reserve it in the router.

## 0.3.2 - 2026-11-08

### Changed
- Close the local MQTT socket when the broker drops the connection (it leaked a
  file descriptor on every reconnect).

### Removed
- The deprecated `armv7` architecture (no longer supported by Home Assistant
  since 2025.12). Supported: `aarch64`, `amd64`.
- `boot: auto` from the manifest: it is the default value, as flagged by the
  add-on linter.

## 0.3.1 - 2026-11-08

### Fixed
- The add-on no longer exits when the inverter does not answer ARP at startup
  (restart or update after sunset, while the inverter is switched off). The
  bridge now starts immediately and the add-on keeps looking for the inverter
  every ~30 s, enabling the ARP spoofing and redirect as soon as it wakes up.
  Previously it stopped for good and nothing restarted it.

## 0.3.0 - 2026-11-08

### Changed
- Sensors no longer become `unavailable` when the inverter goes to sleep (dusk,
  night) or the Wi-Fi collector drops. Their availability now follows the add-on
  itself through an MQTT Last Will on `<base_topic>/bridge/status`.
- While the inverter is offline, instantaneous production values (power, PV
  voltage/current/power, grid current) are published as `0`, like the Hypon
  cloud does; energy totals, temperature and grid voltage/frequency keep their
  last value.

### Added
- `Connexion` diagnostic binary sensor (`connectivity`) reporting whether the
  inverter is currently reachable.
- `offline_grace` option (default 300 s): delay before the inverter is declared
  offline, absorbing the collector reconnecting repeatedly around sunset.

### Fixed
- Stale retained `<base_topic>/<sn>/availability` topic from previous versions is
  cleared on discovery.

## 0.2.0 - 2026-11-08

### Added
- Name and description for every option in the add-on UI (English and French
  translations).
- `inverter_ip` is prefilled with the usual `192.168.1.` prefix as a hint.
- Early validation of `inverter_ip` and `gateway_ip` with a clear error message
  instead of a failed ARP lookup.

## 0.1.2 - 2026-11-08

### Fixed
- Skip writing `ip_forward` when it is already enabled on the host, avoiding a
  misleading warning under `host_network` on Home Assistant OS.

## 0.1.1 - 2026-11-08

### Fixed
- Add `build.yaml` to supply `BUILD_FROM` so the image builds (fixed the empty
  base-name error).
- Limit `arch` to the architectures that have a base image (aarch64, amd64, armv7).

## 0.1.0 - 2026-11-08

### Added
- Initial release: **Hypon Local Bridge** add-on — local read of a Hypon
  micro-inverter via targeted ARP + iptables redirect, MQTT bridge with cloud
  relay, frame decoding and MQTT Discovery.
