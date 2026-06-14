# MQTT Broker Credentials Investigation

**Date:** 2026-06-13
**Hosts investigated:** ten64.welland.mithis.com + a sample of the RPi fleet
**Broker:** ha.welland.mithis.com:1883 (Home Assistant Mosquitto add-on)
**Status:** Diagnosis complete. Remediation requires broker-admin action (below).

> ⚠️ This document contains site infrastructure details (hostnames, MQTT
> usernames). It contains **no passwords**. It is intentionally left
> uncommitted — review before deciding whether to commit or keep local.

---

## TL;DR

The Home Assistant MQTT broker on ha.welland (`ipv6.ha.iot.welland.mithis.com`,
`…190::2`) **requires authentication** — anonymous connect is refused with
`CONNACK: Not authorized` (re-confirmed live 2026-06-13). The `sensors2mqtt`
collectors — **on ten64 AND across the RPi fleet** — connect with **empty
credentials** and only keep working because their TCP sessions were established
*before* the broker started requiring auth, which happened **around 2026-06-11/
06-12** (not 2026-03-30 — that was merely ten64's uptime; corrected below).
**Any restart or reboot of any of these hosts silently kills its publishing**:
the new connection is refused, paho retries forever, and the production code
logs nothing about it.

This is therefore a **fleet-wide** exposure, not a ten64-only one. The fix is
operational, not code: every empty-credential collector that targets the HA
broker needs `MQTT_USER`/`MQTT_PASSWORD`. A working precedent exists —
`gdoc2netcfg` authenticates with a dedicated `gdoc2netcfg` MQTT user and
reconnects without trouble.

**Broker topology matters here — there are at least three brokers:**

| Address | Name | Role | Anonymous? |
|---------|------|------|-----------|
| `…190::2` | `ipv6.ha.iot.welland.mithis.com` = `ha.welland` | Home Assistant broker | **refused now** |
| `…190::3` | `sdr-mqtt.iot.welland.mithis.com` | separate SDR broker | accepts empty creds |
| `…2100::1` | `…tweed…` | fpgas-lab broker | (out of scope) |

---

## Evidence

### 1. The collectors connect to ha.welland, not the local broker

Both units hard-code the HA broker:

```
# /etc/systemd/system/sensors2mqtt-snmp.service  (and -snmp-control.service)
Environment=MQTT_HOST=ha.welland.mithis.com
Environment=POLL_INTERVAL=30
```

Live sockets confirm it (ten64 `::1` → ha.welland `::2`:1883):

```
$ ss -tnp | grep 1883
ESTAB [2404:e80:a137:190::1]:42967 [2404:e80:a137:190::2]:1883 users:(("python",pid=4150319))  # snmp
ESTAB [2404:e80:a137:190::1]:55933 [2404:e80:a137:190::2]:1883 users:(("python",pid=4149801))  # snmp_control
ESTAB [2404:e80:a137:190::1]:44457 [2404:e80:a137:190::2]:1883 users:(("gdoc2netcfg",pid=1132685))
```

### 2. The grandfathered sessions (the landmine)

```
sensors2mqtt-snmp.service:          ActiveEnterTimestamp=2026-03-30 23:17:01, NRestarts=0, MainPID=4150319
sensors2mqtt-snmp-control.service:  ActiveEnterTimestamp=2026-03-30 23:15:50, NRestarts=0, MainPID=4149801
```

`NRestarts=0` with the original PIDs still running and ~74.7 days of uptime:
these processes have **never re-handshaked** with the broker. They connected
before the broker required auth and have ridden the same TCP session ever since.

### 3. The broker refuses anonymous — confirmed live today

A fresh anonymous connection (unique client id, single attempt, did not touch
the live sessions):

```
ANONYMOUS PROBE -> ha.welland.mithis.com:1883
CONNACK reason_code = ReasonCode(Connack, 'Not authorized')
is_failure = True
```

### 4. Credentialed connections work — gdoc2netcfg proves it

`gdoc2netcfg-reachability.service` restarted **today at 2026-06-13 12:01:25**
(triggered by its config-watch path unit) and is publishing normally
(`Published 3409 discovery + 4086 state for 216 hosts` at 15:38). It
authenticates with a dedicated user, per `/opt/gdoc2netcfg/gdoc2netcfg.toml`:

```toml
[[zigbee.sites]]
name = "welland"
mqtt_host = "ha.welland.mithis.com"
mqtt_port = 1883
mqtt_user = "gdoc2netcfg"
mqtt_password = "<redacted>"

[tasmota]
mqtt_user = "DVES_USER"      # Tasmota default account, device-side
mqtt_password = "<redacted>"
mqtt_host = "ha.welland.mithis.com"
```

So: a dedicated-MQTT-user pattern already exists on this broker, and fresh
credentialed connections succeed today. The collectors simply never got an
account.

### 5. `MqttConfig` already supports credentials — no code change needed

`src/sensors2mqtt/base.py` `MqttConfig.from_env()` already reads `MQTT_USER`
and `MQTT_PASSWORD` (defaulting to empty). Supplying them via the units is
sufficient.

---

## Root cause — WHY the broker started requiring credentials (added 2026-06-13)

Confirmed from the add-on's own logs (via SSH to ha.welland → Supervisor API).
ha.welland is **HAOS 17.2** (HA Core 2026.4.1); the broker is the **core_mosquitto
add-on**, currently **v7.1.0** running **mosquitto 2.1.2 + mosquitto-go-auth 3.0.0**.

The add-on now has real MQTT `logins` defined:

```
logins:
  - username: gdoc2netcfg     (password redacted)
  - username: tweed-bridge    (password redacted)
  - username: DVES_USER       (password redacted)
```

With `mosquitto-go-auth` active and logins defined, the broker performs a
username/password check on **every new connection** and rejects empty/anonymous
ones. The broker logs it explicitly, per collector:

```
error: received null username or password for unpwd check
Client sensors2mqtt-local-rpi_sdr_kraken [...] disconnected: not authorised.
```

**So this was a side effect of adding MQTT logins to the add-on (~2026-06-11/12 —
most likely to set up `gdoc2netcfg` MQTT auth and a `tweed-bridge`), not a
deliberate "enable authentication" toggle.** The add-on's v7.0.0 upgrade
(changelog: mosquitto 2.1.2, mosquitto-go-auth 3.0.0) is the enabler — go-auth
3.0.0 is strict about null credentials.

Two consequences worth knowing:

- **go-auth checks credentials only at CONNECT time.** Sessions established
  *before* the logins were added keep working untouched (ten64 since 03-30,
  rpi4-pmod since 06-11, etc.) — that's the "grandfathering". Any reconnect is
  rejected.
- **The breakage is already in progress, not just a future risk.** At least 11
  RPi collectors are currently in a reject-retry loop (already offline in HA):
  `inkycal, rpi4_gwifi, rpi4_ups, rpi5_433mhz, rpi_birds_welland_front,
  rpi_birds_welland_back, rpi_sdr_kraken, rpi_sdr_pluto, rpi_usb, rpiz_dash_1,
  rpiz_dash_2`.

The broker process has been up ~17 days (since ~2026-05-27, the v7 update), so
the 06-11/12 enforcement onset came from a go-auth config **reload** (logins
added) rather than a broker restart.

**Implication for the fix:** the remediation is unchanged and now obvious — add a
`sensors2mqtt` entry to the add-on's `logins` (or reuse one) and give the
collectors those credentials. This is the same `logins` mechanism that
`gdoc2netcfg` already uses successfully.

**Can't we just disable the auth / re-enable anonymous instead?** No — not on
this add-on. Per the official add-on docs: *"This app does not support anonymous
logins; all connections must use a username/password to connect.
`allow_anonymous true` nor any anonymous ACLs will not work with this app."* The
`customize` folder only adds supplementary config (e.g. logging) and cannot
override the auth requirement, and `mosquitto-go-auth` is the same authenticator
HA's own MQTT integration + the `gdoc2netcfg`/`tweed-bridge` logins depend on.
Re-enabling anonymous would require abandoning the HA add-on for a self-hosted
mosquitto — a far bigger change. Giving the collectors credentials is the only
supported path. (The pre-06-11 "anonymous worked" state was almost certainly
because `ha.welland` resolved to a looser/older broker, or the add-on had no
`logins` defined yet — not a configuration you can cleanly return to here.)

---

## The RPi fleet (added 2026-06-13)

*All* welland RPi hardware also runs sensors2mqtt (the `local` collector via
`sensors2mqtt-local.service`), except the `piNN.fpgas` lab Pis (10.21.0.x),
which publish to the `tweed` broker and are out of scope. The HA broker shows
~30 RPi `sensors2mqtt` nodes (plus `big_storage`, `sw_bb_25g`, and the netgear
switches from ten64).

**Same landmine, fleet-wide.** Sampled Pis all run the stock
`/usr/lib/systemd/system/sensors2mqtt-local.service` with only
`Environment=MQTT_HOST=ha.welland.mithis.com` + `POLL_INTERVAL` — **no
credentials** (confirmed in the live process env of `rpi4-pmod`). RPis carry a
normal (non-editable) install at `/opt/sensors2mqtt/lib/python3.13/...`. They
connect anonymously and are currently online only because their sessions
predate the auth change:

| Host | started | broker socket peer | status |
|------|---------|--------------------|--------|
| rpi4-pmod  | 2026-06-11 | `…190::2` (HA)       | online, grandfathered, **at risk on reboot** |
| rpi5-zigbee| (running)  | `…190::2` (HA)       | online, grandfathered, **at risk on reboot** |
| rpiz-serial| 2026-05-25 | `…190::2` (HA)       | online, grandfathered, **at risk on reboot** |
| rpi-sdr-kraken | 2026-06-11 | `…190::3` (sdr-mqtt) | publishing fine to sdr-mqtt |
| rpi-sdr-pluto  | 2026-06-11 | `…190::3` (sdr-mqtt) | publishing fine to sdr-mqtt |

**Two important wrinkles:**

1. **The "offline" RPi nodes on the HA broker are false.** `rpi-sdr-kraken` /
   `rpi-sdr-pluto` show `offline` on ha.welland but are actually `active` and
   publishing every 30s — to **sdr-mqtt** (`…190::3`), a different broker. Their
   `MQTT_HOST=ha.welland` resolves to `…::2` *today*, yet their socket is on
   `…::3` — so `ha.welland.mithis.com` resolved to `…::3` when they started
   (06-11) and was **repointed to `…::2` afterward**. The HA "offline" is stale
   retained status (no LWT to correct it). → reinforces the need for an LWT
   (see the LWT / bridge-availability tasks).

2. **Proof the auth change is recent (~06-11/06-12), not 03-30.** `rpi4-pmod`
   connected to the HA broker (`…::2`) with empty creds at **2026-06-11
   14:12:37** and is still online — so the HA broker still accepted anonymous at
   that moment. The 06-12 smoke test got `Not authorized`, and so does a probe
   today. So anonymous was disabled on the HA broker sometime **after 2026-06-11
   14:12**. (Cannot see the broker's own logs from here to pin the exact moment
   or reason — needs HA-side inspection.)

**Open question for the operator:** the SDR Pis sitting on `sdr-mqtt` (`…::3`)
while their config says `ha.welland` looks like a DNS-repoint artifact. Decide
whether they *should* be on sdr-mqtt or HA — and note that if they reboot now
they'll resolve `ha.welland`→`…::2` and hit the same auth wall.

---

## The stray local mosquitto on ten64 (separate finding)

There is also a `mosquitto.service` running **on ten64 itself** — which should
not be there (the collectors are meant to use HA's broker, and they do).

| Property | Value |
|----------|-------|
| Package | `mosquitto 2.0.22-5` (+ `mosquitto-clients`), **manually** installed ~2026-01-06 |
| Unit | stock `/usr/lib/systemd/system/mosquitto.service`, **enabled** (boot-start) |
| Listener | **localhost only** — `127.0.0.1:1883` and `[::1]:1883`, not network-exposed |
| Config | stock Debian default (`/etc/mosquitto/mosquitto.conf`); no auth, no custom listener, empty `conf.d` |
| Clients | **none** — no loopback sessions; nothing on ten64 connects to it |
| Reverse-deps | none (`apt-cache rdepends --installed mosquitto` → empty) |

It is idle and harmless (not reachable off-box) but unnecessary. Likely a
vestige of an earlier "local broker" plan — note `gdoc2netcfg-reachability.service`
still carries `After=...mosquitto.service` ordering it never uses.

**Safe to remove:**

```bash
sudo systemctl disable --now mosquitto.service
# optional, fully remove the broker (keep mosquitto-clients for mosquitto_pub/sub testing):
sudo apt-get purge mosquitto
```

---

## Remediation runbook (sensors2mqtt credentials)

> Goal: get the two collectors authenticating so they survive restarts.
> The cut-over **will** drop the grandfathered sessions — so verify the new
> credentials work *before* restarting the second service.

### Step 0 — (recommended) deploy PR #1 first
PR #1 adds `make_client()` connect/disconnect logging. With it deployed, a bad
credential is loud in `journalctl` instead of silent. Not required, but it makes
the cut-over verifiable.

### Step 1 — create an MQTT user on the broker (needs HA admin)
On the Home Assistant Mosquitto add-on, add a login (or create a dedicated HA
user) — recommended username `sensors2mqtt` with a strong password. A dedicated
account (not reusing `gdoc2netcfg`) keeps identities and any ACLs separable.

### Step 2 — verify the new credentials BEFORE touching the services
From ten64, probe with a throwaway client id (never a collector's id):

```bash
mosquitto_pub -h ha.welland.mithis.com -p 1883 \
  -u sensors2mqtt -P '<password>' \
  -i ten64-cred-test -t sensors2mqtt/_credtest -m ok
```

Exit code 0 = accepted. Non-zero / "Connection Refused: not authorised" = fix
the account before proceeding. (Do **not** restart any collector until this
passes.)

### Step 3 — store the secret on ten64 (root-only, not in the unit text)
```bash
sudo install -m 0600 -o root -g root /dev/null /etc/sensors2mqtt/mqtt.env
sudoedit /etc/sensors2mqtt/mqtt.env
# contents:
#   MQTT_USER=sensors2mqtt
#   MQTT_PASSWORD=<password>
```
Prefer `EnvironmentFile=` over `Environment=` so the password does not appear in
world-readable `systemctl cat` / `systemctl show` output or the process table.

### Step 4 — point the units at the env file
Add to **both** `sensors2mqtt-snmp.service` and `sensors2mqtt-snmp-control.service`
(via `sudo systemctl edit <unit>` drop-in, or editing the unit):

```ini
[Service]
EnvironmentFile=/etc/sensors2mqtt/mqtt.env
```
Then `sudo systemctl daemon-reload`.

### Step 5 — cut over one service at a time
```bash
sudo systemctl restart sensors2mqtt-snmp-control.service
# verify: new ESTAB session + publishing resumes
sudo ss -tnp | grep 1883
journalctl -u sensors2mqtt-snmp-control.service -n 30 -o short-iso
# (with PR #1: look for "MQTT connected"; without: confirm a new ESTAB + HA entities update)
```
Only once that one is confirmed healthy, restart the other:
```bash
sudo systemctl restart sensors2mqtt-snmp.service
```

### Step 6 — confirm in Home Assistant
Check that the switch sensor + PoE entities are updating (not stale), and that
toggling a PoE port still works (exercises the snmp_control command path).

---

## Open items requiring a human

1. **Broker admin:** create the `sensors2mqtt` MQTT account (Step 1). Needs
   access to the HA Mosquitto add-on.
2. **Decide:** dedicated `sensors2mqtt` user (recommended) vs reuse `gdoc2netcfg`.
3. **Decide:** remove the stray local mosquitto on ten64 (recommended) or leave
   it disabled.
4. The down switch `sw-netgear-s3300-1` is still timing out on SNMP (unrelated to
   MQTT, environmental) — out of scope here, noted for completeness.
