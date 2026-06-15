# Multi-host + multi-collector MQTT availability & topic model — Design

- **Date:** 2026-06-15
- **Branch:** `multi-host-mqtt-safety`
- **Tasks:** #8 (umbrella), #9 (availability redesign)
- **Status:** design approved in brainstorming; pending written-spec review

## Problem

All collectors share one MQTT broker (Home Assistant's). Two gaps remain after the
per-host client_id / node_id work already committed on this branch:

1. **Shared-switch availability is gated on fixed bridge topics.** The `snmp` and
   `snmp_control` collectors serve many switches over one connection, so they used a
   single fixed Last-Will topic (`sensors2mqtt/snmp_bridge/status`,
   `sensors2mqtt/snmp_control_bridge/status`) and listed it in every switch/port
   entity's `availability` (mode `all`). With two hosts polling the *same* switch,
   one host's death flips that shared bridge `offline` and marks the switch
   unavailable even though the other host is still publishing. A per-host bridge
   topic doesn't help either, because the switch's discovery config is a single
   shared retained message that can only name one bridge (last-writer-wins).

2. **`node_id = host_id()` collides when a host runs more than one collector.** The
   committed change made `local`, `ipmi_sensors`, and `hwmon` all use the host's
   node_id. A host running two of them (e.g. big-storage running `local` for OS
   sensors *and* `ipmi_sensors` for the BMC) then has both daemons publishing the
   same `sensors2mqtt/{host}/state` and `…/status` topics (blobs overwrite, wills
   fight) and both setting conflicting `manufacturer`/`model` on the one host device.

## Principles

- **Freshness decides shared-device availability; connection liveness is a separate
  per-host diagnostic that gates nothing shared.** A switch is "available" while its
  data keeps arriving (from any host); whether a specific daemon is alive is its own
  signal.
- **The topic tree reads by ownership.** `sensors2mqtt/{host}/{module}/…` is
  everything one daemon owns (its sensors + its liveness); `sensors2mqtt/{switch}/…`
  is a shared switch device any host may publish to. Host node_ids and switch
  node_ids are disjoint, so the two namespaces never collide.

## Identity model

- **Module token** — one consistent form everywhere (client_id, topics, connection
  unique_ids), spelled with underscores to match the Python module basename:
  `local`, `snmp`, `snmp_control`, `ipmi_sensors`, `hwmon`. This revises the two
  committed client_ids (`snmp-control` → `snmp_control`, `ipmi-sensors` →
  `ipmi_sensors`). Systemd unit filenames keep their existing dashed names.
- **client_id** = `sensors2mqtt-{host}-{module}` where `{host}` = `host_id()` (short
  hostname, dashes→underscores). Unchanged in shape.
- **HA devices:**
  - **One host device per machine:** `identifiers: ["sensors2mqtt_{host}"]`,
    `name: {hostname}`. All host-local collectors *and* the `snmp`/`snmp_control`
    connection entities attach to it.
  - `manufacturer`/`model` are set **only** by a collector that actually knows the
    hardware (`ipmi_sensors` → Supermicro/X11DSC+, `local`-rpi → Raspberry Pi/model,
    `local`-mellanox → Mellanox/SN2410). Generic base `local` (manufacturer
    "Unknown") sends `identifiers`+`name` only, so it never clobbers richer metadata.
    In practice ≤1 known-hardware collector runs per host, so there is no conflict.
  - **Switch devices unchanged:** `sensors2mqtt_{switch}` plus per-port sub-devices
    (`via_device`), shared across hosts.

## Topic model

- **Per-daemon data** (host-local collectors `local`, `ipmi_sensors`, `hwmon`):
  - state: `sensors2mqtt/{host}/{module}/state`
  - ipmi PSU: `sensors2mqtt/{host}/ipmi_sensors/psu{slot}/state`
- **Per-daemon connection status** (every daemon): `sensors2mqtt/{host}/{module}/status`
  — the connection's Last-Will (`offline`), published `online` on connect and
  re-published `online` each poll cycle (heartbeat), `offline` on graceful shutdown.
- **Shared switch topics unchanged** (`snmp`, `snmp_control`):
  `sensors2mqtt/{switch}/state`, `…/status`, `…/port/{NN}/state`,
  `…/port/{NN}/poe/{state,available,force/state}`, and the `…/poe/{set,cycle,force/set}`
  command topics.

## Entity model

- **Host-local sensors** (`local`, `ipmi_sensors`, `hwmon`):
  - Keep `unique_id = {host}_{suffix}` and discovery topic
    `homeassistant/sensor/{host}/{suffix}/config` **unchanged** → existing HA entities
    and history are preserved (see Migration).
  - Change `state_topic` → `sensors2mqtt/{host}/{module}/state` and
    `availability_topic` → `sensors2mqtt/{host}/{module}/status`.
  - Add `expire_after: 300`.
- **Connection `binary_sensor`** (every daemon, including `snmp`/`snmp_control` which
  have no host-local sensors): `device_class: connectivity`,
  `entity_category: diagnostic`, `state_topic: sensors2mqtt/{host}/{module}/status`,
  `payload_on: online` / `payload_off: offline`, `expire_after: 300`,
  `unique_id: {host}_{module}_connection`, discovery
  `homeassistant/binary_sensor/{host}/{module}_connection/config`, attached to the
  host device with an `identifiers`+`name`-only device block. Entity name = the module
  (e.g. *"local"*, *"snmp"*, *"ipmi_sensors"*). This is the "daemon status under the
  host."
- **Switch sensors** (`snmp`): unchanged `unique_id`s and topics; set
  `availability_topic: sensors2mqtt/{switch}/status` (drop the bridge) and add
  `expire_after: 300`.
- **PoE command entities** (`snmp_control`; `switch`/`button` platforms, which do
  **not** support `expire_after`): drop the control bridge from availability —
  - toggle / cycle → `availability: [{switch}/status, {switch}/port/{NN}/poe/available]`, mode `all`
  - force → `availability_topic: {switch}/status`

  Daemon death for control is surfaced by the connection `binary_sensor`, not by these
  entities (PoE control is single-controller-per-switch in practice).

## `expire_after`

- A constant `EXPIRE_AFTER = 300` (5 minutes), defined in `discovery.py`, applied to
  every `sensor` and `binary_sensor`. Generous enough that even a slow sequential
  SNMP cycle never flaps an entity to unavailable; detects a dead/hung daemon within
  5 minutes. Not applied to `switch`/`button` (unsupported by HA).

## Migration (Option A — topics only, no entity recreation)

- `unique_id`s and discovery object/topics for existing entities are **unchanged**, so
  Home Assistant keeps the same entities, history, and entity_ids; it merely re-points
  each to the new `…/{module}/…` data topic when the updated discovery config is
  republished. (Hosts that previously set a custom `node_id` in `local.toml` already
  migrate as part of the earlier node_id-unification decision; hostname-derived
  deployments — the common case — see no migration here.)
- **Legacy retained cleanup** (publish empty retained payloads on startup):
  - host-local collectors clear `sensors2mqtt/{host}/state` and `sensors2mqtt/{host}/status`.
  - `snmp`/`snmp_control` clear `sensors2mqtt/snmp_bridge/status` and
    `sensors2mqtt/snmp_control_bridge/status`.
- Relies on co-located collectors not defining the *same* sensor suffix (true today:
  `local` uses `cpu_temp`/`uptime`/`mem_*`/`load_*`; `ipmi_sensors` uses
  `cpu1_temp`/`rail_12v`/…). If a future collector pair shares a suffix, switch to
  Option B (namespace `unique_id`s as `{host}_{module}_{suffix}`) — out of scope here.

## Per-collector summary

| Collector | module | data topic | connection status topic | device |
|-----------|--------|------------|--------------------------|--------|
| local (+rpi/mellanox) | `local` | `sensors2mqtt/{host}/local/state` | `sensors2mqtt/{host}/local/status` | host device (sets mfr/model when rpi/mellanox) |
| ipmi | `ipmi_sensors` | `sensors2mqtt/{host}/ipmi_sensors/state` (+ `…/psu{slot}/state`) | `sensors2mqtt/{host}/ipmi_sensors/status` | host device (sets Supermicro/X11DSC+) |
| hwmon (legacy) | `hwmon` | `sensors2mqtt/{host}/hwmon/state` | `sensors2mqtt/{host}/hwmon/status` | host device (sets Mellanox/SN2410) |
| snmp | `snmp` | shared `sensors2mqtt/{switch}/…` | `sensors2mqtt/{host}/snmp/status` | host device (connection entity only) + switch devices |
| snmp_control | `snmp_control` | shared `sensors2mqtt/{switch}/…` | `sensors2mqtt/{host}/snmp_control/status` | host device (connection entity only) + switch devices |

## Affected files

- `discovery.py`: `SensorDef.expire_after` field + emit it in `discovery_payload`;
  `EXPIRE_AFTER` constant; a connection-`binary_sensor` discovery helper; host-device
  helper; `availability_config` callers stop receiving a bridge topic.
- `base.py`: `client_id_for` module tokens are passed with underscores; add a
  `module` concept so `state_topic`/`avail_topic` become
  `sensors2mqtt/{node_id}/{module}/{state,status}`; legacy-topic cleanup; per-cycle
  status heartbeat; publish the connection binary_sensor.
- `collector/local/base.py` (+ `rpi.py`, `mellanox.py` inherit): module `local`;
  manufacturer/model only when known.
- `collector/ipmi_sensors.py`: module `ipmi_sensors`; per-module topics incl. PSU;
  host device; connection entity; `client_id_for("ipmi_sensors")`.
- `collector/hwmon.py`: module `hwmon`; per-module topics; connection entity.
- `collector/snmp.py` / `collector/snmp_control.py`: per-host connection status topic +
  binary_sensor + heartbeat + Last-Will; drop bridge from switch/port availability;
  add `expire_after` to switch sensors; legacy bridge-topic cleanup;
  `client_id_for("snmp_control")`.
- Tests for all of the above.

## Testing strategy (TDD)

- `expire_after` is emitted (300) in sensor and binary_sensor discovery payloads.
- Switch hw and per-port sensor availability is the switch status topic only — no
  bridge topic listed.
- PoE toggle/cycle availability lists exactly `[{switch}/status, …/poe/available]`
  (no bridge); force lists only the switch status.
- Host-local sensor `state_topic`/`availability_topic` are the `…/{module}/…` paths
  while `unique_id` is unchanged.
- Connection `binary_sensor`: connectivity device_class, diagnostic category,
  per-host `…/{module}/status` topic, host-device attachment via `identifiers`+`name`
  only, `expire_after` set.
- Each daemon publishes `online` to its connection status each cycle (heartbeat) and
  sets the matching Last-Will.
- Legacy retained topics are cleared on startup.
- `client_id_for` callers use underscore module tokens.

## Out of scope (tracked separately)

- ipmi `manufacturer`/`model` probing — task #12.
- `hwmon.py` deletion vs keep — task #11.
- `local --once` `make_client` parity — task #13.
- Option B `unique_id` namespacing and an `expire_after` env override — future, only
  if needed.
