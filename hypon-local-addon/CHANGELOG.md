# Changelog

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
