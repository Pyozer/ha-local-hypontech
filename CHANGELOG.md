# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.2] - 2026-11-08
### Fixed
- Skip writing `ip_forward` when it is already enabled on the host, avoiding a
  misleading warning under `host_network` on Home Assistant OS.

## [0.1.1] - 2026-11-08
### Fixed
- Add `build.yaml` to supply `BUILD_FROM` so the image builds (fixed the empty
  base-name error).
- Limit `arch` to the architectures that have a base image (aarch64, amd64, armv7).

## [0.1.0] - 2026-11-08
### Added
- Initial release: **Hypon Local Bridge** add-on — local read of a Hypon
  micro-inverter via targeted ARP + iptables redirect, MQTT bridge with cloud
  relay, frame decoding and MQTT Discovery.
