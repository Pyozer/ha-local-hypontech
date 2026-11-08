# AGENTS.md

Guidance for anyone — humans or AI agents — working on this repository.

## What this project is

A custom Home Assistant add-on repository. The add-on **Hypon Local Bridge**
(`hypon-local-addon/`) reads a Hypon micro-inverter locally and publishes it to
Home Assistant over MQTT, without touching DNS or the router.

## How it works

On the Home Assistant host network, the add-on:

1. ARP-spoofs **only the inverter** so it believes the gateway is Home Assistant;
2. redirects the inverter's MQTT port (1883) to a local bridge via `iptables`,
   forwarding its other traffic normally;
3. relays the connection to the Hypon cloud (the vendor app keeps working),
   decodes the frames and publishes sensors via MQTT Discovery;
4. restores the inverter's ARP cache and removes its rules on shutdown.

## Layout

| Path | Role |
|---|---|
| `repository.yaml` | Marks the repo as an HA add-on repository |
| `LICENSE` | MIT license |
| `hypon-local-addon/config.yaml` | Add-on manifest + options/schema |
| `hypon-local-addon/build.yaml` | Base image per architecture (`BUILD_FROM`) |
| `hypon-local-addon/Dockerfile` | Image build |
| `hypon-local-addon/run.sh` | bashio launcher: options -> env, then runs the app |
| `hypon-local-addon/translations/en.yaml` / `fr.yaml` | Option labels and descriptions shown in the HA UI |
| `hypon-local-addon/app/hypon_intercept.py` | Targeted ARP + iptables + lifecycle |
| `hypon-local-addon/app/hypon_bridge.py` | MQTT bridge: cloud relay + decode + Discovery |
| `hypon-local-addon/DOCS.md` / `DOCS.fr.md` | User docs (EN / FR) |
| `tests/` | Test-suite (`unittest`, standard library only): codec, decoding, bridge logic, relay, interceptor, repo conventions |
| `.github/workflows/ci.yml` | CI: lint, tests on Python 3.12–3.14, add-on lint, image build + tests inside the image |
| `ruff.toml` / `.yamllint` / `.coveragerc` | Lint and coverage configuration used by CI |

The Python is **standard library only** (no third-party dependencies).

## Conventions

### Language
- All code, comments, commit messages and primary docs are in **English**.
- French docs are provided as `*.fr.md` companions and kept in sync.

### Options
- Every option in `config.yaml` needs a `name` and a `description` in both
  `translations/en.yaml` and `translations/fr.yaml`; keep them in sync.

### Commits
- Follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `build:`, `ci:`, `test:`…
- Imperative mood, concise subject (~72 chars), optional body explaining *why*.

### Releases
On every release:
1. Bump `version` in `hypon-local-addon/config.yaml` (SemVer `MAJOR.MINOR.PATCH`).
2. Add a matching entry at the top of `hypon-local-addon/CHANGELOG.md`, newest
   first (Home Assistant only reads the changelog from the add-on folder; the
   root `CHANGELOG.md` is a symlink to it, so edit the add-on one).
3. Commit it, e.g. `chore(release): 0.1.3`, and tag if desired.

### Local checks
CI runs the same checks (`.github/workflows/ci.yml`); run them before pushing.
- `python3 -m unittest discover -s tests` — the test-suite (it also enforces these
  conventions: options/translations/docs in sync, `config.yaml` version = top
  `CHANGELOG.md` entry, standard-library-only imports).
- `ruff check .` (needs `pip install ruff`) and `shellcheck -e SC2155 hypon-local-addon/run.sh`
- `python3 -m py_compile hypon-local-addon/app/*.py`
- `bash -n hypon-local-addon/run.sh`

When changing behaviour, add or update a test. Keep the add-on lightweight: it
must stay standard-library only and idle at ~0 % CPU / ~25 MB RAM.
- No real Home Assistant is available in CI; validate the runtime bits (raw ARP
  socket, iptables) on an actual HA OS install.

## Requirements / constraints

- Home Assistant OS / Supervised; the add-on needs `host_network` and the
  `NET_ADMIN` / `NET_RAW` capabilities.
- HA and the inverter must be on the **same L2 segment** (ARP does not cross a
  router or a VLAN).
- The add-on acts only on the configured inverter IP and is fully reversible.
