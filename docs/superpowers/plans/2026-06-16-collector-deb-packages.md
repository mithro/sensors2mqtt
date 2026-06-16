# Per-collector Debian Packages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `sensors2mqtt-snmp`, `sensors2mqtt-snmp-control`, and `sensors2mqtt-ipmi-sensors` Debian binary packages (mirroring `sensors2mqtt-local`) so every collector is apt-installable; centralize the shared example configs in `python3-sensors2mqtt`; install the three new units enabled-but-not-started.

**Architecture:** Pure Debian packaging change under `debian/` (plus docs). **No Python source changes.** Verification is build-and-inspect (`dpkg-buildpackage` + `dpkg-deb -f/-c` + maintainer-script inspection), since packaging is not covered by pytest.

**Tech Stack:** debhelper-compat 13, dh-python, pybuild-plugin-pyproject, `dh_installsystemd`, dpkg.

---

## File Structure

**Created:**
- `debian/python3-sensors2mqtt.postinst` — seed `/etc/sensors2mqtt/env` from the example
- `debian/sensors2mqtt-snmp.install`
- `debian/sensors2mqtt-snmp-control.install`
- `debian/sensors2mqtt-ipmi-sensors.install`

**Modified:**
- `debian/control` — `Replaces`/`Breaks` on `python3-sensors2mqtt`; 3 new binary stanzas
- `debian/python3-sensors2mqtt.install` — add `env.example` + `snmp.toml.example`
- `debian/sensors2mqtt-local.install` — drop `env.example`
- `debian/sensors2mqtt-local.postinst` — drop env-seeding + mkdir
- `debian/rules` — `override_dh_installsystemd` (`--no-start` for the 3 new units)
- `README.md`, `docs/collectors.md`, `docs/getting-started.md`

The three service packages need **no** custom `postinst`/`prerm` — `dh_installsystemd` autogenerates the enable/start/stop snippets; `--no-start` in `rules` suppresses only the start.

**Build-artifact hygiene (applies to every build step):** `dpkg-buildpackage` writes `.deb`/`.changes`/`.buildinfo` to the parent dir (`..` = `.claude/worktrees/`). After inspecting, always run `fakeroot debian/rules clean` and `rm -f ../sensors2mqtt_*_* ../python3-sensors2mqtt_*_* ../sensors2mqtt-*_*_*` so `git status` stays clean. Do **not** use `2>/dev/null`.

---

### Task 1: Relocate shared example configs into python3-sensors2mqtt

**Files:**
- Modify: `debian/python3-sensors2mqtt.install`
- Create: `debian/python3-sensors2mqtt.postinst`
- Modify: `debian/control` (python3-sensors2mqtt stanza)
- Modify: `debian/sensors2mqtt-local.install`
- Modify: `debian/sensors2mqtt-local.postinst`

- [ ] **Step 1: Add the examples to the lib package's install list**

`debian/python3-sensors2mqtt.install`:
```
usr/lib/python3*/dist-packages/sensors2mqtt
usr/lib/python3*/dist-packages/sensors2mqtt-*.dist-info
deploy/env.example usr/share/sensors2mqtt/
snmp.toml.example usr/share/sensors2mqtt/
```

- [ ] **Step 2: Create the lib postinst that seeds /etc/sensors2mqtt/env**

`debian/python3-sensors2mqtt.postinst`:
```bash
#!/bin/bash
set -e

if [ "$1" = "configure" ]; then
    mkdir -p /etc/sensors2mqtt
    if [ ! -f /etc/sensors2mqtt/env ]; then
        cp /usr/share/sensors2mqtt/env.example /etc/sensors2mqtt/env
    fi
fi

#DEBHELPER#
```

- [ ] **Step 3: Add Replaces/Breaks to the python3-sensors2mqtt stanza**

In `debian/control`, in the `Package: python3-sensors2mqtt` stanza, insert these two lines immediately after the `Depends:` block (before `Description:`):
```
Replaces: sensors2mqtt-local (<< 0.4~)
Breaks: sensors2mqtt-local (<< 0.4~)
```
(`<< 0.4~` covers every current `0.3.postN` version, so the `env.example` move is handled without hardcoding the exact build version.)

- [ ] **Step 4: Drop env.example from the local package**

`debian/sensors2mqtt-local.install` (remove the env.example line):
```
deploy/sensors2mqtt-local.service lib/systemd/system/
```

- [ ] **Step 5: Strip env-seeding (and now-redundant mkdir) from local's postinst**

`debian/sensors2mqtt-local.postinst`:
```bash
#!/bin/bash
set -e

if [ "$1" = "configure" ]; then
    systemctl daemon-reload
    systemctl enable sensors2mqtt-local
fi

#DEBHELPER#
```

- [ ] **Step 6: Build and verify the file move**

Run:
```bash
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b
```
Expected: build succeeds.

Verify the examples ship in the lib package:
```bash
dpkg-deb -c ../python3-sensors2mqtt_*_all.deb | grep -E "env.example|snmp.toml.example"
```
Expected: both `./usr/share/sensors2mqtt/env.example` and `./usr/share/sensors2mqtt/snmp.toml.example`.

Verify local no longer ships env.example (so no two packages own it):
```bash
dpkg-deb -c ../sensors2mqtt-local_*_all.deb | grep -c "env.example"
```
Expected: `0`.

Verify the lib postinst seeds env:
```bash
grep -c "cp /usr/share/sensors2mqtt/env.example" debian/python3-sensors2mqtt/DEBIAN/postinst
```
Expected: `1`.

- [ ] **Step 7: Clean artifacts and commit**
```bash
fakeroot debian/rules clean
rm -f ../sensors2mqtt_*_* ../python3-sensors2mqtt_*_* ../sensors2mqtt-local_*_*
git add debian/
git commit -m "build(deb): centralize env/snmp example configs in python3-sensors2mqtt"
```

---

### Task 2: Add the three collector service packages

**Files:**
- Modify: `debian/control` (3 new binary stanzas)
- Create: `debian/sensors2mqtt-snmp.install`
- Create: `debian/sensors2mqtt-snmp-control.install`
- Create: `debian/sensors2mqtt-ipmi-sensors.install`

- [ ] **Step 1: Append three binary stanzas to debian/control**

Append to `debian/control`:
```
Package: sensors2mqtt-snmp
Architecture: all
Depends: python3-sensors2mqtt (= ${binary:Version}),
         snmp,
         systemd,
         ${misc:Depends}
Description: sensors2mqtt SNMP collector for Netgear managed switches
 Installs the sensors2mqtt-snmp systemd service, which polls Netgear managed
 switches over SNMP and publishes sensor data to Home Assistant via MQTT
 auto-discovery.
 .
 Configure switches in /etc/sensors2mqtt/snmp.toml and the MQTT connection in
 /etc/sensors2mqtt/env, then start the service.

Package: sensors2mqtt-snmp-control
Architecture: all
Depends: python3-sensors2mqtt (= ${binary:Version}),
         snmp,
         systemd,
         ${misc:Depends}
Description: sensors2mqtt PoE control service for Netgear managed switches
 Installs the sensors2mqtt-snmp-control systemd service, which exposes per-port
 PoE control for Netgear managed switches to Home Assistant via MQTT.
 .
 Configure switches in /etc/sensors2mqtt/snmp.toml and the MQTT connection in
 /etc/sensors2mqtt/env, then start the service.

Package: sensors2mqtt-ipmi-sensors
Architecture: all
Depends: python3-sensors2mqtt (= ${binary:Version}),
         ipmitool,
         python3-requests,
         systemd,
         ${misc:Depends}
Description: sensors2mqtt IPMI sensor collector
 Installs the sensors2mqtt-ipmi-sensors systemd service, which reads sensors
 from a BMC via ipmitool and the BMC web API and publishes them to Home
 Assistant via MQTT auto-discovery.
 .
 Configure BMC_HOST/BMC_USER/BMC_PASS and the MQTT connection in
 /etc/sensors2mqtt/env, then start the service.
```

- [ ] **Step 2: Create the three .install files**

`debian/sensors2mqtt-snmp.install`:
```
deploy/sensors2mqtt-snmp.service lib/systemd/system/
```
`debian/sensors2mqtt-snmp-control.install`:
```
deploy/sensors2mqtt-snmp-control.service lib/systemd/system/
```
`debian/sensors2mqtt-ipmi-sensors.install`:
```
deploy/sensors2mqtt-ipmi-sensors.service lib/systemd/system/
```

- [ ] **Step 3: Build and verify the new packages**
```bash
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b
```
Expected: builds `python3-sensors2mqtt` + `sensors2mqtt-{local,snmp,snmp-control,ipmi-sensors}`.

Check Depends:
```bash
dpkg-deb -f ../sensors2mqtt-snmp_*_all.deb Depends
dpkg-deb -f ../sensors2mqtt-snmp-control_*_all.deb Depends
dpkg-deb -f ../sensors2mqtt-ipmi-sensors_*_all.deb Depends
```
Expected: snmp/snmp-control include `python3-sensors2mqtt (= <ver>)`, `snmp`, `systemd`; ipmi-sensors includes `python3-sensors2mqtt (= <ver>)`, `ipmitool`, `python3-requests`, `systemd`.

Check each ships its unit:
```bash
for p in snmp snmp-control ipmi-sensors; do dpkg-deb -c ../sensors2mqtt-$p_*_all.deb | grep "systemd/system/sensors2mqtt-$p.service"; done
```
Expected: one matching path per package.

- [ ] **Step 4: Clean artifacts and commit**
```bash
fakeroot debian/rules clean
rm -f ../sensors2mqtt_*_* ../python3-sensors2mqtt_*_* ../sensors2mqtt-*_*_*
git add debian/
git commit -m "build(deb): add sensors2mqtt-snmp, -snmp-control, -ipmi-sensors packages"
```

---

### Task 3: Install the three new units enabled-but-not-started

**Files:**
- Modify: `debian/rules`

- [ ] **Step 1: Add an override_dh_installsystemd**

`debian/rules`:
```make
#!/usr/bin/make -f

export SETUPTOOLS_SCM_PRETEND_VERSION = $(shell dpkg-parsechangelog -SVersion | sed 's/-.*//')
export PYBUILD_TEST_PYTEST = 1

%:
	dh $@ --with python3 --buildsystem=pybuild

override_dh_installsystemd:
	dh_installsystemd -p sensors2mqtt-snmp --no-start
	dh_installsystemd -p sensors2mqtt-snmp-control --no-start
	dh_installsystemd -p sensors2mqtt-ipmi-sensors --no-start
	dh_installsystemd --remaining-packages
```
(`-p<pkg> --no-start` enables but does not start those three; `--remaining-packages` gives `sensors2mqtt-local` its default enable+start.)

- [ ] **Step 2: Build and verify enable-vs-start in the generated maintainer scripts**
```bash
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b
```
The three new packages must enable but NOT start:
```bash
for p in snmp snmp-control ipmi-sensors; do
  echo "== sensors2mqtt-$p =="
  grep -c "enable" debian/sensors2mqtt-$p/DEBIAN/postinst
  grep -c "deb-systemd-invoke.* start" debian/sensors2mqtt-$p/DEBIAN/postinst
done
```
Expected per package: enable count `>= 1`, start count `0`.

`sensors2mqtt-local` must still start:
```bash
grep -c "deb-systemd-invoke.* start" debian/sensors2mqtt-local/DEBIAN/postinst
```
Expected: `>= 1`.

(If `--remaining-packages` does not produce the expected split, fall back to per-unit `dh_installsystemd --no-start --name=sensors2mqtt-<x>` calls — see spec "open items".)

- [ ] **Step 3: Clean artifacts and commit**
```bash
fakeroot debian/rules clean
rm -f ../sensors2mqtt_*_* ../python3-sensors2mqtt_*_* ../sensors2mqtt-*_*_*
git add debian/rules
git commit -m "build(deb): install snmp/snmp-control/ipmi units enabled but not started"
```

---

### Task 4: Document the new packages

**Files:**
- Modify: `README.md`, `docs/collectors.md`, `docs/getting-started.md`

- [ ] **Step 1: List the packages and the install flow**

In each doc, add the three packages alongside `python3-sensors2mqtt`/`sensors2mqtt-local`, and document the deploy flow (match each file's existing style/headings):
```
sudo apt install sensors2mqtt-snmp          # or -snmp-control / -ipmi-sensors
sudoedit /etc/sensors2mqtt/snmp.toml        # from /usr/share/sensors2mqtt/snmp.toml.example
sudoedit /etc/sensors2mqtt/env              # MQTT_USER / MQTT_PASSWORD (+ BMC_* for ipmi)
sudo systemctl start sensors2mqtt-snmp      # packages install enabled but not started
```
Note that the units install **enabled but not started**, so the admin places config + creds first.

- [ ] **Step 2: Commit**
```bash
git add README.md docs/
git commit -m "docs: document per-collector Debian packages and install flow"
```

---

## Self-Review

- **Spec coverage:** new packages (Task 2) ✓; runtime deps ✓; enable-no-start (Task 3) ✓; centralized examples + env seeding + Replaces/Breaks (Task 1) ✓; docs (Task 4) ✓; build-and-inspect verification ✓.
- **No Python changes** → existing pytest suite is unaffected; the final review still runs `uv run pytest -q` and `uv run ruff check .` to confirm no regression.
- **Placeholders:** `<ver>` in expected `dpkg-deb -f` output is the build-derived version (not a TODO). `0.4~` in Replaces/Breaks is intentional and explained.
- **Consistency:** package/unit names match the module tokens (`snmp`, `snmp_control`→`snmp-control`, `ipmi_sensors`→`ipmi-sensors`); the deploy/*.service ExecStart lines are already correct (verified in the spec).

## Final review (after all tasks)

1. `DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b` builds all 5 packages cleanly.
2. `dpkg-deb -f`/`-c` spot-check all three new packages (Depends + unit shipped) one more time.
3. `uv run pytest -q` (expect unchanged pass count) and `uv run ruff check .` (clean).
4. Clean artifacts; then use superpowers:finishing-a-development-branch.
