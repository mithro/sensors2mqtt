# Requirements: gdoc2netcfg-issued MQTT credentials for sensors2mqtt

**Date:** 2026-06-13
**Status:** Draft requirements — handoff to the gdoc2netcfg maintainer
**Related:** `docs/mqtt-broker-credentials-investigation.md` (root-cause investigation)

> This document specifies *what* gdoc2netcfg needs to do. The *how* (which
> module, generator, or subcommand) is left to the gdoc2netcfg maintainer.
> It is intentionally self-contained — no prior context required.

---

## 1. Summary

Have **gdoc2netcfg** issue and manage **per-host MQTT credentials** for the
`sensors2mqtt` collector fleet:

1. Derive a deterministic username + password for each host.
2. Register those users on the Home Assistant Mosquitto broker.
3. Emit each host's `sensors2mqtt` MQTT config (a systemd `EnvironmentFile`).

gdoc2netcfg is the natural home because it already owns the host inventory (the
Google Sheets), already generates and distributes config, and already holds MQTT
broker credentials.

---

## 2. Background / why this is needed

- `sensors2mqtt` collectors run on **ten64** (2 collectors), **~30 welland
  Raspberry Pis** (`sensors2mqtt-local`), and a couple of servers/switches
  (`big_storage`, `sw_bb_25g`). They publish sensor data to the Home Assistant
  MQTT broker (`ha.welland.mithis.com`, the `core_mosquitto` add-on).
- The add-on (now **v7.1.0**, mosquitto **2.1.2** + **mosquitto-go-auth 3.0.0**)
  **rejects empty/anonymous connections**:
  `error: received null username or password for unpwd check` →
  `disconnected: not authorised`. This began (~2026-06-11/12) as a side effect
  of adding MQTT `logins` to the add-on (for `gdoc2netcfg` + `tweed-bridge`).
- The collectors currently connect with **empty credentials**. They survive
  only on TCP sessions that predate the change; **≥11 RPi collectors have
  already reconnected and are now locked out** (offline in HA). Every other one
  goes dark on its next reboot.
- **No `sensors2mqtt` code change is required.** Its `MqttConfig.from_env()`
  already reads `MQTT_USER` / `MQTT_PASSWORD`. The missing piece is *issuing and
  delivering credentials* — which is what this requests of gdoc2netcfg.

Full evidence: `docs/mqtt-broker-credentials-investigation.md`.

---

## 3. Credential scheme (REQUIRED)

- **R1 — Username:** `s2m-<hostname>`, where `<hostname>` is the host's short
  hostname (`hostname -s`). Examples: `s2m-rpi4-pmod`, `s2m-ten64`,
  `s2m-rpi-sdr-kraken`.
- **R2 — Password:** deterministically derived as
  `base64url( HMAC-SHA256(key = SHARED_SECRET, msg = <hostname>) )` (strip `=`
  padding). Use **HMAC** (keyed) — *not* a plain `sha256(secret + hostname)`
  concatenation. The value is recomputable at any time from the secret + the
  hostname, so no per-host password needs to be stored long-term.
- **R3 — Shared secret placement (security-critical):** a single high-entropy
  `SHARED_SECRET` stored **only on the gdoc2netcfg host** (in gdoc2netcfg's
  secret store / config, mode `0600`). Derivation happens **centrally**; only
  each host's *derived password* is distributed. **The shared secret MUST NOT be
  written to the target hosts** — otherwise compromising any single host (whose
  hostname is not secret) would let an attacker recompute every host's password.

Reference implementation (Python):

```python
import hmac, hashlib, base64

def s2m_password(shared_secret: bytes, hostname: str) -> str:
    digest = hmac.new(shared_secret, hostname.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")  # ~43 chars

username = f"s2m-{hostname}"
password = s2m_password(shared_secret, hostname)
```

---

## 4. Generated artifacts (REQUIRED)

- **R4 — Per-host env file content.** For each in-scope host (see §5), produce a
  systemd `EnvironmentFile` (`KEY=VALUE` lines) to be installed at
  `/etc/sensors2mqtt/env`, owner `root:root`, mode `0600`:

  ```ini
  MQTT_HOST=ha.welland.mithis.com
  MQTT_PORT=1883
  MQTT_USER=s2m-<hostname>
  MQTT_PASSWORD=<derived password>
  POLL_INTERVAL=30
  ```

  (`MQTT_HOST` exception for the SDR Pis — see R7.)

- **R5 — Broker registration.** Register each `s2m-<hostname>` user on the HA
  Mosquitto add-on's `logins` list, via the Supervisor API:
  - **MUST merge** with — not replace — the existing logins
    (`gdoc2netcfg`, `tweed-bridge`, `DVES_USER`).
  - **SHOULD store passwords pre-hashed** (`password_pre_hashed: true`, hashed
    with the add-on's `pw` tool) so plaintext passwords are not persisted in the
    add-on options or HA backups.
  - Applying changes requires `POST /addons/core_mosquitto/options` followed by
    a restart/reload of the add-on (see §10 for the API facts).

---

## 5. Scope — which hosts (REQUIRED)

- **R6 — In scope** (issue an `s2m-<hostname>` credential for each):
  - **ten64** — runs two collectors (`sensors2mqtt-snmp`,
    `sensors2mqtt-snmp-control`). One hostname → they **share `s2m-ten64`**
    (distinct MQTT client-ids, which MQTT permits). *Optional:* if you prefer
    per-service usernames, `s2m-ten64-snmp` / `s2m-ten64-snmp-control` is also
    acceptable — sensors2mqtt does not care.
  - **Every welland RPi** running `sensors2mqtt-local` (the IoT Pis:
    `rpi-*`, `rpiz-*`, `rpi4/5-*`, `inkycal`, etc.).
  - **big_storage** (Supermicro/IPMI) and **sw_bb_25g** (Mellanox SN2410).
- **R7 — SDR Pis: register the login, but do NOT repoint them.**
  `rpi-sdr-kraken`, `rpi-sdr-pluto` (and any other `rpi-sdr-*`) currently publish
  successfully to a **separate** broker, `sdr-mqtt.iot.welland.mithis.com`.
  Generate and **register their `s2m-<hostname>` login on the HA broker as a
  spare** (harmless; lets them work if ever repointed), **but do not change their
  `MQTT_HOST`** to ha.welland and do not push them an HA-pointing env file — that
  would move them off the broker they currently work on. The HA credential stays
  unused until someone intentionally repoints them.
- **R8 — Out of scope:** the `piNN.fpgas` lab Pis (`10.21.0.x`). They publish to
  the **`tweed`** broker, not HA. Do **not** issue HA credentials for them.
- **Inventory source:** gdoc2netcfg's existing host inventory. Provide a way to
  select the sensors2mqtt host set (a role/tag, or reuse existing site/category
  filtering) so the list stays in sync with the sheets.

---

## 6. Distribution & the host-side unit (REQUIRED + boundary)

- **R9 — Delivery.** The env file (R4) must land on each in-scope host at
  `/etc/sensors2mqtt/env` (`0600 root`). If gdoc2netcfg already pushes generated
  files to hosts, reuse that mechanism. **If gdoc2netcfg only generates files
  locally**, then this is the integration boundary: gdoc2netcfg
  *generates per-host env content + registers broker logins*, and a separate
  deploy step (e.g. sensors2mqtt's `packaging/deploy-rpi.py`, or ansible)
  distributes the files. Please state which model you implement.
- **R10 — Unit must read the env file (sensors2mqtt-side, FYI).** The current
  `sensors2mqtt-local` Debian package ships a unit with
  `EnvironmentFile=-/etc/sensors2mqtt/env`. **However, the live fleet currently
  runs an older venv-based deployment** whose units hardcode
  `Environment=MQTT_HOST=…` and have **no** `EnvironmentFile` — on those hosts
  the env file is inert until the unit is updated (package upgrade, or a drop-in
  `…/sensors2mqtt-local.service.d/10-credentials.conf` adding
  `EnvironmentFile=-/etc/sensors2mqtt/env`). This is a **sensors2mqtt-side**
  concern tracked separately; noted here so the env file isn't assumed to take
  effect on its own.

---

## 7. Cutover ordering (REQUIRED behavior)

Registering logins on the add-on **restarts the broker, dropping the
grandfathered sessions** — so tooling must allow this sequence:

1. Generate + distribute env files to **all** in-scope hosts (collectors **not**
   restarted yet — still-grandfathered ones keep working).
2. Register all `s2m-*` logins on the broker (+ restart/reload the add-on).
3. Restart collectors — **ten64 + one canary Pi first**, verify they appear
   online in HA, then the remainder.
4. Reconcile any that fail to reconnect.

**R11:** the generate+distribute step (1) must be runnable **independently** of
the broker-registration step (2) — e.g. via a `--dry-run` or separate
subcommands — so the safe ordering above is possible and reviewable.

---

## 8. Non-functional requirements

- **Idempotent.** Re-running converges: same `SHARED_SECRET` + hostname ⇒ same
  credentials; merging logins must neither duplicate nor drop existing entries.
- **Security.** `SHARED_SECRET` only on the generator host; per-host env files
  `0600 root`; broker stores **pre-hashed** passwords; **never log or print**
  passwords or the shared secret.
- **No collateral breakage.** Must not disturb HA core's own MQTT integration or
  the existing `gdoc2netcfg` / `tweed-bridge` / `DVES_USER` logins.
- **Rotation semantics.** Changing `SHARED_SECRET` rotates *every* password
  (full redeploy). Revoking a single host = delete its `logins` entry (clean,
  independent).

---

## 9. Acceptance criteria

- [ ] Each in-scope host has `/etc/sensors2mqtt/env` (`0600 root`) with
      `MQTT_USER=s2m-<hostname>` and the correct derived password (SDR Pis
      excepted per R7).
- [ ] The add-on `logins` contains a working `s2m-<hostname>` entry for every
      in-scope host (incl. SDR Pis as spares), **plus** the pre-existing three.
- [ ] A freshly rebooted in-scope host reconnects and publishes (verified in HA).
- [ ] The ≥11 currently-locked-out RPi collectors recover.
- [ ] No regression to HA core MQTT, gdoc2netcfg, Tasmota, or tweed-bridge.

---

## 10. Reference — broker & Supervisor API facts

- **Host:** `ha.welland.mithis.com` = HAOS 17.2, HA Core 2026.4.1. Reachable via
  SSH (the HA SSH add-on; host key already trusted from ten64).
- **Broker:** `core_mosquitto` add-on **v7.1.0**, mosquitto **2.1.2**,
  mosquitto-go-auth **3.0.0**.
- **Add-on `logins` option:** list of `{username, password}`; pre-hashed
  supported via `password_pre_hashed: true` + the add-on's `pw` tool. The add-on
  docs state anonymous access is unsupported and `allow_anonymous true` is
  ignored — so credentials are mandatory (no opt-out).
- **Supervisor API (from inside HA, e.g. the SSH add-on):**
  - `GET  http://supervisor/addons/core_mosquitto/info`
  - `POST http://supervisor/addons/core_mosquitto/options`  body `{"options": {"logins": [...]}}`
  - `POST http://supervisor/addons/core_mosquitto/restart`
  - Auth: `Authorization: Bearer <SUPERVISOR_TOKEN>` (token available in the
    add-on environment, e.g. `/run/s6/container_environment/SUPERVISOR_TOKEN`).

---

## 11. Open questions for the maintainer

1. Where this lives in gdoc2netcfg — a new generator, a new subcommand, or part
   of an existing flow.
2. Does gdoc2netcfg push files to hosts, or only generate locally? (Decides the
   R9 boundary — does gdoc2netcfg deliver `/etc/sensors2mqtt/env`, or hand off to
   a separate deploy step?)
3. Per-host vs per-service username for ten64 (R6).
4. Whether gdoc2netcfg or a separate operator step owns the broker-restart
   cutover (§7).
5. *Optional future hardening:* per-user ACLs scoping each `s2m-<hostname>` to
   only `sensors2mqtt/<node_id>/#` + the `homeassistant/` discovery prefix —
   contingent on what ACL control the HA add-on exposes.
