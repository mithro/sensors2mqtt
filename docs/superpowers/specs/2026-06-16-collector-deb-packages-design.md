# Per-collector Debian packages — Design

**Date:** 2026-06-16

## Problem

The Debian packaging builds only two binaries: `python3-sensors2mqtt` (the
library) and `sensors2mqtt-local` (the local-collector service). The `snmp`,
`snmp-control`, and `ipmi-sensors` collectors have **no** deb service package —
they exist only as `deploy/*.service` templates and hand-deployed editable
venvs (e.g. ten64 runs an `/opt` uv editable venv for `sensors2mqtt-snmp` +
`sensors2mqtt-snmp-control`).

Consequences: hosts cannot be migrated to apt-managed installs (task #5), and
`apt install` cannot provision the collectors that ten64 / big_storage actually
run.

## Goal

Add one Debian binary package per collector type, mirroring `sensors2mqtt-local`,
so every collector is `apt`-installable and co-installable:

- `sensors2mqtt-snmp`
- `sensors2mqtt-snmp-control`
- `sensors2mqtt-ipmi-sensors`

## Design

### 1. New `debian/control` binary stanzas

Each is `Architecture: all` and `Depends: python3-sensors2mqtt (= ${binary:Version}),
systemd, ${misc:Depends}`, plus collector-specific runtime deps:

| Package | Extra Depends | Ships unit | ExecStart |
|---------|---------------|-----------|-----------|
| `sensors2mqtt-snmp` | `snmp` | `deploy/sensors2mqtt-snmp.service` | `/usr/bin/python3 -m sensors2mqtt.collector.snmp --config /etc/sensors2mqtt/snmp.toml` |
| `sensors2mqtt-snmp-control` | `snmp` | `deploy/sensors2mqtt-snmp-control.service` | `/usr/bin/python3 -m sensors2mqtt.collector.snmp_control --config /etc/sensors2mqtt/snmp.toml` |
| `sensors2mqtt-ipmi-sensors` | `ipmitool`, `python3-requests` | `deploy/sensors2mqtt-ipmi-sensors.service` | `/usr/bin/python3 -m sensors2mqtt.collector.ipmi_sensors` |

`snmp` provides `snmpget`/`snmpwalk`/`snmpset`; `ipmi_sensors` imports `requests`
and shells out to `ipmitool`. The service files already exist in `deploy/` with
the correct system-python `ExecStart` and `EnvironmentFile=-/etc/sensors2mqtt/env`.
All three are co-installable (ten64 = snmp + snmp-control).

### 2. Install / enable / start behavior

The three new units install **enabled but not started**:

- `*.install`: `deploy/sensors2mqtt-<type>.service` → `lib/systemd/system/`.
- `postinst` (configure): `mkdir -p /etc/sensors2mqtt`; `systemctl daemon-reload`;
  `systemctl enable sensors2mqtt-<type>` — **no** `start`. (`#DEBHELPER#` present.)
- `prerm` (remove/deconfigure): `systemctl stop … || true`; `systemctl disable … || true`;
  `systemctl daemon-reload`. (`#DEBHELPER#` present.)
- `debian/rules`: `override_dh_installsystemd` passing `--no-start` scoped to the
  three new units, while leaving `sensors2mqtt-local` on its current enable +
  auto-start (it auto-detects hardware and needs no config).

**Rationale:** snmp/snmp-control require `/etc/sensors2mqtt/snmp.toml`, ipmi
requires `BMC_*`, and all require MQTT creds in `/etc/sensors2mqtt/env`.
Auto-starting a freshly-installed unit would crash-loop (missing config) and/or
silently strand MQTT (no creds — the broker rejects anonymous). Enable-but-no-start
lets the admin place config + creds and then `systemctl start`; the unit also
auto-starts on the next boot (config present by then).

### 3. Shared example configs (file-conflict resolution)

`snmp.toml` is used by **both** snmp and snmp-control, and `env.example` is used
by every collector — so no two binary packages may ship the same file. Centralize
shared examples in the common `python3-sensors2mqtt` package:

- `python3-sensors2mqtt.install`: add `usr/share/sensors2mqtt/env.example` and
  `usr/share/sensors2mqtt/snmp.toml.example` (from `deploy/env.example` and
  `snmp.toml.example`).
- New `python3-sensors2mqtt.postinst` (configure): `mkdir -p /etc/sensors2mqtt`;
  seed `/etc/sensors2mqtt/env` from the example **if absent**. (This seeding moves
  here from `sensors2mqtt-local`.) Do **not** seed `/etc/sensors2mqtt/snmp.toml`
  (real switch config required) — ship the example only.
- `sensors2mqtt-local`: remove `env.example` from its `.install`; drop the
  env-seeding lines from its `postinst` (now handled by the lib package).
- File-move handling: `env.example` moves `sensors2mqtt-local` → `python3-sensors2mqtt`.
  Add `Replaces: sensors2mqtt-local (<< <ver>~)` and `Breaks: sensors2mqtt-local (<< <ver>~)`
  to the `python3-sensors2mqtt` stanza so dpkg reassigns the file cleanly on upgrade.

**Dependency ordering:** apt configures `python3-sensors2mqtt` (a dependency)
before any service package, so `/etc/sensors2mqtt/env` is seeded before a service
`postinst` runs.

### 4. Build / CI

`debian/rules` keeps `dh … --with python3 --buildsystem=pybuild`, adding only the
`override_dh_installsystemd`. `deb.yml` builds all binary packages from one
`dpkg-buildpackage`, so the apt repo gains the three new packages on merge to main.

## Testing / Verification

No Python source changes → the pytest suite is unaffected (run it to confirm).
Packaging is verified by building and inspecting (spot-check, as for issue #13):

- `DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b` builds
  `python3-sensors2mqtt` + `sensors2mqtt-{local,snmp,snmp-control,ipmi-sensors}`.
- Per new `.deb`: `dpkg-deb -f <deb> Depends` shows the correct deps; `dpkg-deb -c`
  shows the unit shipped to `lib/systemd/system/`.
- `env.example` + `snmp.toml.example` appear in `python3-sensors2mqtt` only (no
  duplicate-file conflicts).
- `ruff check` (unchanged, but run).

## Docs

`README.md` package table, `docs/collectors.md`, `docs/getting-started.md`: list
the new packages and document the `apt install` → create `/etc/sensors2mqtt/snmp.toml`
(from `snmp.toml.example`) + `/etc/sensors2mqtt/env` → `systemctl start` flow.

## Out of scope

- The actual ten64 / fleet migration off the editable venv (tasks #5 / #30); this
  task only produces the packages.
- Any restructuring of `sensors2mqtt-local` beyond moving `env.example` out.

## Open items / risks

- The `Replaces`/`Breaks` version must be ≥ the version this change first ships in
  (derived from git by `packaging/deb-version.py` at build); verify against the
  built version.
- Confirm the exact `dh_installsystemd` invocation that auto-starts `local` but not
  the three new units (e.g. separate `dh_installsystemd` calls with `--name`, or
  `--no-start` listing the three `.service` names).
