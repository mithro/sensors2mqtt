# sensors2mqtt MQTT Topic Inventory

A complete reference of every MQTT topic the sensors2mqtt collectors publish or
subscribe to, the substitutions used in those topic templates, and what the
payloads look like.

Reflects the multi-host scheme on branch `multi-host-mqtt-safety`:
`base.py`, `discovery.py`, and `collector/{snmp,snmp_control,ipmi_sensors}.py`
plus `collector/local/{base,rpi,mellanox}.py`.

> **Two facts that apply to every row below**, so they are not repeated:
> - **Every publish is retained** (`retain=True` everywhere in the code).
> - **Every publish is QoS 0** (nothing in the codebase sets a `qos=` argument,
>   so paho's default of 0 is used).

> **The topic tree reads by ownership.** `sensors2mqtt/{host}/{module}/…` is
> everything one daemon owns (its sensors + its liveness); `sensors2mqtt/{switch}/…`
> is a shared switch device any host may publish to. Host ids and switch ids are
> disjoint, so the two namespaces never collide.

---

## What "JSON discovery" means

Home Assistant can create entities two ways: hand-written YAML, or **MQTT
auto-discovery**. sensors2mqtt uses auto-discovery exclusively.

With auto-discovery, the collector publishes a **retained JSON configuration
message** to a specially-named topic under the discovery prefix
(`homeassistant/...`). Home Assistant's MQTT integration is subscribed to that
prefix; when it sees one of these messages it **automatically creates (or
updates) the matching entity**. Because the message is retained, HA picks it up
on every restart or integration reload without the device having to re-announce.
Publishing an **empty** payload to the same config topic **deletes** the entity.

That JSON payload is what "JSON discovery" refers to. It does **not** carry any
sensor reading — it only describes *how to build the entity and where to read its
value from*. The actual readings live on a separate `state_topic`, and HA extracts
each entity's value from that shared JSON blob using the `value_template`.

### Worked example

For the IPMI collector's CPU1 temperature, the config message is published
(retained) to:

```
homeassistant/sensor/big_storage/cpu1_temp/config
```

with this JSON body (built by `discovery.py:discovery_payload` + `device_dict` + `ORIGIN`):

```json
{
  "name": "CPU1 Temperature",
  "unique_id": "big_storage_cpu1_temp",
  "state_topic": "sensors2mqtt/big_storage/ipmi_sensors/state",
  "value_template": "{{ value_json.cpu1_temp }}",
  "unit_of_measurement": "°C",
  "device": {
    "identifiers": ["sensors2mqtt_big_storage"],
    "name": "big-storage",
    "manufacturer": "Supermicro",
    "model": "X11DSC+"
  },
  "expire_after": 300,
  "availability_topic": "sensors2mqtt/big_storage/ipmi_sensors/status",
  "payload_available": "online",
  "payload_not_available": "offline",
  "state_class": "measurement",
  "device_class": "temperature",
  "origin": {
    "name": "sensors2mqtt",
    "sw": "<installed package version>",
    "url": "https://github.com/mithro/sensors2mqtt"
  }
}
```

Note the **discovery topic** (`homeassistant/sensor/big_storage/cpu1_temp/config`)
and the **`unique_id`** (`big_storage_cpu1_temp`) are keyed by the host node_id
only — they did **not** change in the multi-host migration. Only the
`state_topic`/`availability_topic` moved under `…/{module}/…`, so Home Assistant
keeps the existing entity and its history and just re-points it.

Field-by-field:

| Field                                                         | Purpose                                                                                                                                                                                              |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                                                        | Human-readable entity name shown in HA.                                                                                                                                                              |
| `unique_id`                                                   | Stable ID so HA can track the entity across restarts and let the user customise it.                                                                                                                  |
| `state_topic`                                                 | The topic HA reads the value **from** — a per-daemon JSON blob.                                                                                                                                      |
| `value_template`                                              | Jinja2 expression to pull *this* entity's value out of that blob (`value_json.<key>`).                                                                                                               |
| `unit_of_measurement`, `device_class`, `state_class`, `icon`  | Presentation / classification metadata.                                                                                                                                                              |
| `device`                                                      | Groups entities under one HA device (registry). `identifiers` ties them together; `manufacturer`/`model` are set only by a hardware-aware collector (generic "Unknown" is omitted to avoid clobber). |
| `expire_after`                                                | Seconds (300) after which HA marks the entity unavailable if no fresh state arrives — connection-agnostic freshness.                                                                                 |
| `availability_topic` / `availability` (+ `availability_mode`) | Where HA reads online/offline; see the availability-wiring table.                                                                                                                                    |
| `origin`                                                      | Attribution: which integration produced the entity, and its version.                                                                                                                                 |

So discovery (the `homeassistant/.../config` topic) is published **once per entity
at startup**, and the readings flow continuously on the `state_topic`.

---

## Substitutions used in topic templates

| Placeholder   | Meaning                                                                                                                                  | Derived from                                                                                                                                               | Example                                             |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `{component}` | HA entity domain segment in the discovery path.                                                                                          | Chosen per entity type. One of `sensor`, `binary_sensor`, `switch`, `button`.                                                                              | `sensor`                                            |
| `{host}`      | The host's node_id — short hostname, `-`→`_`. Used for host-local device topics and as the `{host}` in client-ids and connection topics. | `host_id()` = `socket.gethostname().split(".")[0].replace("-","_")` (`base.py`).                                                                           | `big_storage`, `ten64`, `rpi_sdr_kraken`            |
| `{module}`    | Collector module token (= Python module basename, underscores).                                                                          | Per collector: `local`, `snmp`, `snmp_control`, `ipmi_sensors`.                                                                                            | `ipmi_sensors`                                      |
| `{sw}`        | A switch's node_id — a *shared* device key (not host-scoped).                                                                            | snmp.toml `[switches.<key>]` name with `-`→`_` (`snmp.py`).                                                                                                | key `sw-netgear-m4300-24x` → `sw_netgear_m4300_24x` |
| `{NN}`        | Physical port number, **zero-padded to 2 digits** (`str(port).zfill(2)`), range `1..port_count`.                                         | `snmp.py` / `snmp_control.py`.                                                                                                                             | `01`, `24`, `48`                                    |
| `{slot}`      | PSU slot number, 1-based integer (not padded).                                                                                           | `ipmi_sensors.py` PSU enumeration.                                                                                                                         | `1`, `2`                                            |
| `{suffix}`    | Sensor key — also the JSON key in the state blob and the discovery object_id.                                                            | per-collector sensor definitions.                                                                                                                          | `cpu1_temp`, `asic_temp`, `psu_power`, `rail_12v`   |
| `{pkey}`      | Per-port sensor value key (SNMP ports).                                                                                                  | `snmp.py:_publish_port_discovery`: `link`, `speed_mbps`, `vlan_pvid`, `vlan_name`, `description`, `lldp_neighbor`, `poe_watts`, `poe_admin`, `poe_status`. | `link`                                              |
| `+`           | MQTT **single-level wildcard** (subscriptions only) — matches any one level, i.e. any port `NN`.                                         | `snmp_control.py` subscribes.                                                                                                                              | matches `.../port/24/...`                           |

### ⚠ Two substitutions render *differently* in discovery vs data topics

Port and PSU appear **concatenated** inside a discovery `object_id`, but as their
own **slash-separated level(s)** in the runtime data/command topics:

| Concept  | In discovery object_id (concatenated)          | In data/command topic (slash levels)    |
| -------- | ---------------------------------------------- | --------------------------------------- |
| Port     | `port{NN}_{pkey}` → `port24_link`              | `port/{NN}/...` → `port/24/state`       |
| PoE port | `port{NN}_poe_toggle` → `port24_poe_toggle`    | `port/{NN}/poe/...` → `port/24/poe/set` |
| PSU      | `psu{slot}_{suffix}` → `psu1_ac_input_voltage` | `psu{slot}/...` → `psu1/state`          |

The host-local `{module}` segment is the reverse: it is in the **data** topics
(`sensors2mqtt/{host}/{module}/state`) but **not** in the host-local sensor's
discovery object_id (still `homeassistant/sensor/{host}/{suffix}/config`, so
existing entities are preserved). It does appear in the connection diagnostic's
object_id (`{module}_connection`).

### Topic id vs display name

Topics use the **node_id** form (underscores, e.g. `sw_netgear_m4300_24x`). The HA
device **display name** is the raw hostname/switch name (e.g. `big-storage`,
`sw-netgear-m4300-24x`). The two are intentionally different.

---

## Table A — Discovery topics (`homeassistant/...`)

All Pub · retained · QoS 0. These carry the JSON discovery payloads described above.

| Collector         | Topic template                                                  | Rendered example                                                     | Entity type              |
| ----------------- | --------------------------------------------------------------- | -------------------------------------------------------------------- | ------------------------ |
| local / ipmi      | `homeassistant/sensor/{host}/{suffix}/config`                   | `homeassistant/sensor/big_storage/cpu1_temp/config`                  | device sensor            |
| ipmi (PSU)        | `homeassistant/sensor/{host}/psu{slot}_{suffix}/config`         | `homeassistant/sensor/big_storage/psu1_ac_input_voltage/config`      | PSU sensor               |
| **every daemon**  | `homeassistant/binary_sensor/{host}/{module}_connection/config` | `homeassistant/binary_sensor/ten64/snmp_connection/config`           | connectivity diagnostic  |
| snmp (hardware)   | `homeassistant/sensor/{sw}/{suffix}/config`                     | `homeassistant/sensor/sw_netgear_m4300_24x/psu_power/config`         | switch sensor            |
| snmp (per-port)   | `homeassistant/sensor/{sw}/port{NN}_{pkey}/config`              | `homeassistant/sensor/sw_netgear_m4300_24x/port24_link/config`       | port sub-device sensor   |
| snmp-ctl (toggle) | `homeassistant/switch/{sw}/port{NN}_poe_toggle/config`          | `homeassistant/switch/sw_netgear_gsm7252ps/port24_poe_toggle/config` | switch                   |
| snmp-ctl (cycle)  | `homeassistant/button/{sw}/port{NN}_poe_cycle/config`           | `homeassistant/button/sw_netgear_gsm7252ps/port24_poe_cycle/config`  | button                   |
| snmp-ctl (force)  | `homeassistant/switch/{sw}/port{NN}_poe_force/config`           | `homeassistant/switch/sw_netgear_gsm7252ps/port24_poe_force/config`  | switch (config category) |

---

## Table B — Published runtime topics (`sensors2mqtt/...`)

All Pub · retained · QoS 0.

| Collector    | Topic template                                     | Rendered example                                            | Payload                                                                                                                        |
| ------------ | -------------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| local / ipmi | `sensors2mqtt/{host}/{module}/state`               | `sensors2mqtt/big_storage/ipmi_sensors/state`               | JSON `{suffix: value, ...}`                                                                                                    |
| local / ipmi | `sensors2mqtt/{host}/{module}/status`              | `sensors2mqtt/big_storage/ipmi_sensors/status`              | `online` / `offline` — daemon availability **and** Last-Will + per-cycle heartbeat (also the connection binary_sensor's state) |
| ipmi (PSU)   | `sensors2mqtt/{host}/ipmi_sensors/psu{slot}/state` | `sensors2mqtt/big_storage/ipmi_sensors/psu1/state`          | JSON per-PSU blob (`ac_input_voltage_v`, …, `fan_2_rpm`, `status`, `serial`, `slot`, `max_power_w`)                            |
| snmp         | `sensors2mqtt/{sw}/state`                          | `sensors2mqtt/sw_netgear_m4300_24x/state`                   | JSON hardware values (`fan*_rpm`, `temp`, `psu_power`; PoE models also `port{NN}_poe_mw`)                                      |
| snmp         | `sensors2mqtt/{sw}/status`                         | `sensors2mqtt/sw_netgear_m4300_24x/status`                  | `online` / `offline` — **shared** per-switch availability (offline on poll failure)                                            |
| snmp         | `sensors2mqtt/{sw}/port/{NN}/state`                | `sensors2mqtt/sw_netgear_m4300_24x/port/24/state`           | JSON port blob (`link`, `speed_mbps`, `vlan_pvid`, `vlan_name`, `description`, `lldp_neighbor`, `poe_*`)                       |
| snmp         | `sensors2mqtt/{host}/snmp/status`                  | `sensors2mqtt/ten64/snmp/status`                            | `online` / `offline` — the snmp daemon's connection: Last-Will + per-cycle heartbeat                                           |
| snmp-ctl     | `sensors2mqtt/{sw}/port/{NN}/poe/state`            | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/state`       | `ON` / `OFF` (PoE admin state)                                                                                                 |
| snmp-ctl     | `sensors2mqtt/{sw}/port/{NN}/poe/available`        | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/available`   | `online` / `offline` (greys out the toggle)                                                                                    |
| snmp-ctl     | `sensors2mqtt/{sw}/port/{NN}/poe/force/state`      | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/force/state` | `ON` / `OFF` (force-override, read back on startup)                                                                            |
| snmp-ctl     | `sensors2mqtt/{sw}/status`                         | `sensors2mqtt/sw_netgear_gsm7252ps/status`                  | `online` / `offline` — **shared** per-switch status (also written by the snmp collector)                                       |
| snmp-ctl     | `sensors2mqtt/{host}/snmp_control/status`          | `sensors2mqtt/ten64/snmp_control/status`                    | `online` / `offline` — the snmp_control daemon's connection: Last-Will + per-cycle heartbeat                                   |

---

## Table C — Subscribed command topics (`sensors2mqtt/...`)

Sub (inbound from Home Assistant) · QoS 0. `+` is the MQTT single-level wildcard
(any port).

| Collector                     | Subscribe template                         | Matches example                                             | Payload in                                                                   |
| ----------------------------- | ------------------------------------------ | ----------------------------------------------------------- | ---------------------------------------------------------------------------- |
| snmp-ctl                      | `sensors2mqtt/{sw}/port/+/poe/set`         | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/set`         | `ON` / `OFF`                                                                 |
| snmp-ctl                      | `sensors2mqtt/{sw}/port/+/poe/cycle`       | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/cycle`       | `PRESS`                                                                      |
| snmp-ctl                      | `sensors2mqtt/{sw}/port/+/poe/force/set`   | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/force/set`   | `ON` / `OFF`                                                                 |
| snmp-ctl (startup, transient) | `sensors2mqtt/{sw}/port/+/poe/force/state` | `sensors2mqtt/sw_netgear_gsm7252ps/port/24/poe/force/state` | `ON` / `OFF` — 1-second read-back of retained force state, then unsubscribed |

---

## Identity & liveness per collector

Client-ids are the MQTT **connection identity** (not topics) — `sensors2mqtt-{host}-{module}`
everywhere, so two daemons of the same kind on different hosts never collide. The
Last-Will is the per-host connection status topic the broker flips to `offline` if
the connection drops ungracefully.

| Collector | device node_id           | client_id                          | Last-Will / connection topic              |
| --------- | ------------------------ | ---------------------------------- | ----------------------------------------- |
| local     | `host_id()`              | `sensors2mqtt-{host}-local`        | `sensors2mqtt/{host}/local/status`        |
| snmp      | switch keys (**shared**) | `sensors2mqtt-{host}-snmp`         | `sensors2mqtt/{host}/snmp/status`         |
| snmp-ctl  | switch keys (shared)     | `sensors2mqtt-{host}-snmp_control` | `sensors2mqtt/{host}/snmp_control/status` |
| ipmi      | `host_id()`              | `sensors2mqtt-{host}-ipmi_sensors` | `sensors2mqtt/{host}/ipmi_sensors/status` |

> `host_id()` is deliberately the **short** hostname for now. Two machines sharing
> a short hostname (e.g. a `ten64` at two sites) would still collide on one broker;
> making it globally unique is a separate, later decision.

---

## Availability wiring (how each entity's "depends" is built)

Built by `discovery.py:availability_config()`: one topic → a single
`availability_topic`; two or more → an `availability` list with `availability_mode`
(`all` = AND). Every `sensor`/`binary_sensor` also gets `expire_after: 300`
(connection-agnostic freshness); `switch`/`button` entities cannot use it.

| Entity class                         | Availability topics                                  | Mode        | expire_after        |
| ------------------------------------ | ---------------------------------------------------- | ----------- | ------------------- |
| local / ipmi sensors & PSU           | `{host}/{module}/status`                             | single      | yes (300)           |
| connection diagnostic (every daemon) | *(none — relies on `expire_after`)*                  | —           | yes (300)           |
| snmp switch hardware sensors         | `{sw}/status`                                        | single      | yes (300)           |
| snmp per-port sensors                | `{sw}/status`                                        | single      | yes (300)           |
| PoE toggle / cycle                   | `{sw}/status` **AND** `{sw}/port/{NN}/poe/available` | `all` (AND) | n/a (switch/button) |
| PoE force                            | `{sw}/status`                                        | single      | n/a (switch)        |

`expire_after` is what makes a **shared** switch survive multi-host polling: any
host publishing fresh state keeps the entity alive; only when *all* stop does it
expire. The fixed per-collector bridge topics that used to gate switch availability
were removed — they marked a switch offline when *one* of several hosts died.

HA's `availability_mode` is a single flat operator over the list (`all`/`any`/`latest`),
so it can't express a nested boolean; freshness via `expire_after` carries the
"is any publisher alive" half instead.

---

## Other observations from the code

- **Everything is retained and QoS 0** (a QoS-0 publish issued while disconnected
  is silently dropped — which is why connect-failure logging matters).
- **Per-daemon namespace `sensors2mqtt/{host}/{module}/…`** lets several collectors
  run on one host (e.g. `local` + `ipmi_sensors`) without overwriting each other,
  and rolls their entities + connection status up into **one merged host device**.
- **The shared `{sw}/status` topic has multiple writers** — the `snmp` and
  `snmp_control` daemons (and potentially multiple hosts). "Many publishers, one
  device-keyed status topic" is the intended pattern for shared switches.
- **No fixed, non-host-keyed topics remain.** The old `snmp_bridge/status` /
  `snmp_control_bridge/status` are gone (cleared on startup); each daemon's liveness
  is now `sensors2mqtt/{host}/{module}/status`.
- **`manufacturer`/`model` are set only by hardware-aware collectors** (ipmi,
  rpi, mellanox); generic base `local` omits them so it can't clobber the merged
  host device's metadata.
- **Legacy retained topics are cleared on startup**: the pre-module
  `sensors2mqtt/{host}/state|status` and the two old bridge topics. (Old per-PSU
  `sensors2mqtt/{host}/psu{slot}/state` are left — harmless; HA re-points cleanly.)
- **The `--once` paths** (`collector/local/__main__.py`, and ipmi/snmp/snmp_control
  one-shot modes) don't all route through `make_client`, so some lack the Last-Will
  / connect-failure logging the daemon paths have (tracked separately).
