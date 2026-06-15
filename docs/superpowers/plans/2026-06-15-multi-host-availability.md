# Multi-host Availability & Topic Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make shared switch entities multi-host-safe via `expire_after` (dropping the fixed bridge topics from availability), give every daemon a per-host connection diagnostic under one merged host device, and namespace each daemon's data/status topics as `sensors2mqtt/{host}/{module}/…` so multiple collectors can run on one host without colliding.

**Architecture:** Freshness (`expire_after`) decides shared-device availability; per-host connection liveness is a separate diagnostic `binary_sensor` that gates nothing shared. Existing entity `unique_id`s are unchanged (Option A — no HA migration); only data/status topics move under `…/{module}/…`.

**Tech Stack:** Python 3, paho-mqtt v2, Home Assistant MQTT discovery, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-06-15-multi-host-availability-design.md`

**Working dir:** worktree `.claude/worktrees/multi-host-mqtt-safety` (branch `multi-host-mqtt-safety`). Run tests with `uv run pytest` (env already synced with `--all-extras`).

---

## File map

| File | Change |
|------|--------|
| `src/sensors2mqtt/discovery.py` | `EXPIRE_AFTER` const; emit `expire_after` in `discovery_payload`; drop `bridge_topic` params; add `publish_connection_diagnostic()` |
| `src/sensors2mqtt/base.py` | add `connection_status_topic()`; `BasePublisher.module` (abstract) + `client_id`/`state_topic`/`avail_topic` derive from it; `run()` clears legacy topics + publishes connection diagnostic |
| `src/sensors2mqtt/collector/local/base.py` | define `module = "local"` (drop explicit `client_id`) |
| `src/sensors2mqtt/collector/ipmi_sensors.py` | per-module topics; `client_id_for("ipmi_sensors")`; PSU `expire_after`; connection diagnostic + heartbeat + legacy clear |
| `src/sensors2mqtt/collector/hwmon.py` | define `module = "hwmon"` (drop explicit `client_id`) |
| `src/sensors2mqtt/collector/snmp.py` | drop bridge from availability; per-port `expire_after`; per-host connection topic (LWT+heartbeat+diagnostic); clear legacy bridge topic |
| `src/sensors2mqtt/collector/snmp_control.py` | drop bridge from availability; per-host connection topic; `client_id_for("snmp_control")`; clear legacy bridge topic |
| `tests/*` | updated/added per task |

---

## Task 1: discovery.py — `expire_after` + connection diagnostic + drop bridge param

**Files:**
- Modify: `src/sensors2mqtt/discovery.py`
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_discovery.py`:

```python
def test_discovery_payload_emits_expire_after():
    from sensors2mqtt.discovery import EXPIRE_AFTER, SensorDef, DeviceInfo, discovery_payload
    sensor = SensorDef("temp", "Temp", "°C", device_class="temperature")
    device = DeviceInfo(node_id="x", name="x", manufacturer="x", model="x")
    cfg = discovery_payload(sensor, device, "s2m/x/state", "s2m/x/status")
    assert cfg["expire_after"] == EXPIRE_AFTER == 300


def test_publish_connection_diagnostic_shape():
    import json
    from unittest.mock import MagicMock
    from sensors2mqtt.discovery import publish_connection_diagnostic, EXPIRE_AFTER
    client = MagicMock()
    publish_connection_diagnostic(client, "ten64", "snmp", "ten64")
    topic, payload = client.publish.call_args[0][0], client.publish.call_args[0][1]
    assert topic == "homeassistant/binary_sensor/ten64/snmp_connection/config"
    cfg = json.loads(payload)
    assert cfg["state_topic"] == "sensors2mqtt/ten64/snmp/status"
    assert cfg["device_class"] == "connectivity"
    assert cfg["entity_category"] == "diagnostic"
    assert cfg["unique_id"] == "ten64_snmp_connection"
    assert cfg["payload_on"] == "online" and cfg["payload_off"] == "offline"
    assert cfg["expire_after"] == EXPIRE_AFTER
    assert cfg["device"]["identifiers"] == ["sensors2mqtt_ten64"]
    assert cfg["device"]["name"] == "ten64"
    assert "manufacturer" not in cfg["device"]  # identifiers+name only, no clobber


def test_device_dict_omits_unknown_metadata():
    from sensors2mqtt.discovery import DeviceInfo, device_dict
    d = device_dict(DeviceInfo(node_id="x", name="x", manufacturer="Unknown", model="Unknown"))
    assert "manufacturer" not in d and "model" not in d  # generic collector won't clobber
    d2 = device_dict(DeviceInfo(node_id="y", name="y", manufacturer="Supermicro", model="X11DSC+"))
    assert d2["manufacturer"] == "Supermicro" and d2["model"] == "X11DSC+"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_discovery.py::test_discovery_payload_emits_expire_after tests/test_discovery.py::test_publish_connection_diagnostic_shape -v`
Expected: FAIL (`ImportError: cannot import name 'EXPIRE_AFTER'` / `publish_connection_diagnostic`).

- [ ] **Step 3: Implement in `discovery.py`**

Add the constant near `DISCOVERY_PREFIX`:

```python
DISCOVERY_PREFIX = "homeassistant"

# Seconds after which HA marks a push entity unavailable if no fresh state arrives.
# Connection-agnostic freshness: any host publishing keeps a shared entity alive;
# all silent -> it expires. Generous enough that a slow sequential SNMP poll cycle
# never flaps an entity.
EXPIRE_AFTER = 300
```

In `discovery_payload`, drop `bridge_topic` and emit `expire_after`:

```python
def discovery_payload(
    sensor: SensorDef,
    device: DeviceInfo,
    state_topic: str,
    avail_topic: str,
) -> dict:
    """Build HA auto-discovery config payload for a sensor."""
    config = {
        "name": sensor.name,
        "unique_id": f"{device.node_id}_{sensor.suffix}",
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{sensor.suffix} }}}}",
        "unit_of_measurement": sensor.unit,
        "device": device_dict(device),
        "expire_after": EXPIRE_AFTER,
        **availability_config(avail_topic),
        "origin": ORIGIN,
    }
    if sensor.state_class:
        config["state_class"] = sensor.state_class
    if sensor.device_class:
        config["device_class"] = sensor.device_class
    if sensor.icon:
        config["icon"] = sensor.icon
    if sensor.entity_category:
        config["entity_category"] = sensor.entity_category
    return config
```

Drop `bridge_topic` from `publish_discovery`:

```python
def publish_discovery(
    client: mqtt.Client,
    sensors: list[SensorDef],
    device: DeviceInfo,
    state_topic: str,
    avail_topic: str,
) -> int:
    """Publish HA auto-discovery configs for all sensors. Returns count published."""
    for sensor in sensors:
        config_topic = f"{DISCOVERY_PREFIX}/sensor/{device.node_id}/{sensor.suffix}/config"
        payload = discovery_payload(sensor, device, state_topic, avail_topic)
        client.publish(config_topic, json.dumps(payload), retain=True)
    return len(sensors)
```

Add the connection-diagnostic helper at the end of the module:

```python
def publish_connection_diagnostic(
    client: mqtt.Client, host: str, module: str, hostname: str
) -> None:
    """Publish a per-host, per-daemon connectivity binary_sensor.

    Attaches to the host's device (identifiers + name only, so it never clobbers
    the manufacturer/model that a hardware-aware collector sets). Its state is the
    daemon's connection status topic, which is the daemon's Last-Will + per-cycle
    heartbeat. Surfaces "which daemon on which host is alive" without gating any
    shared device.
    """
    status_topic = f"sensors2mqtt/{host}/{module}/status"
    config = {
        "name": module,
        "unique_id": f"{host}_{module}_connection",
        "state_topic": status_topic,
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "entity_category": "diagnostic",
        "expire_after": EXPIRE_AFTER,
        "device": {"identifiers": [f"sensors2mqtt_{host}"], "name": hostname},
        "origin": ORIGIN,
    }
    config_topic = f"{DISCOVERY_PREFIX}/binary_sensor/{host}/{module}_connection/config"
    client.publish(config_topic, json.dumps(config), retain=True)
```

Make `device_dict` omit `manufacturer`/`model` when they are the generic `"Unknown"`, so that when several collectors on one host share the merged `sensors2mqtt_{host}` device, only a hardware-aware collector sets those fields (no last-writer-wins clobber):

```python
def device_dict(device: DeviceInfo) -> dict:
    """Build HA device registry dict."""
    d: dict = {
        "identifiers": [f"sensors2mqtt_{device.node_id}"],
        "name": device.name,
    }
    if device.manufacturer and device.manufacturer != "Unknown":
        d["manufacturer"] = device.manufacturer
    if device.model and device.model != "Unknown":
        d["model"] = device.model
    if device.configuration_url:
        d["configuration_url"] = device.configuration_url
    if device.connections:
        d["connections"] = [list(c) for c in device.connections]
    if device.via_device:
        d["via_device"] = device.via_device
    return d
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_discovery.py -v`
Expected: PASS (all). Note: existing `discovery_payload`/`publish_discovery` callers that passed `bridge_topic=` will now break at the call sites — those are fixed in Tasks 6/8/9; do not run the full suite yet.

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/discovery.py tests/test_discovery.py
git commit -m "feat: expire_after + connection-diagnostic discovery; drop bridge_topic param" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: base.py — per-module topics + `connection_status_topic` + run() wiring

**Files:**
- Modify: `src/sensors2mqtt/base.py`
- Test: `tests/test_base.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_base.py`, update `StubPublisher` to define `module` instead of `client_id`:

```python
class StubPublisher(BasePublisher):
    def __init__(self, poll_values=None, **kwargs):
        super().__init__(**kwargs)
        self._poll_values = poll_values
        self.poll_count = 0

    @property
    def sensors(self):
        return [SensorDef(suffix="temp", name="Temperature", unit="°C", device_class="temperature")]

    @property
    def device(self):
        return DeviceInfo(node_id="test", name="test", manufacturer="Test", model="T1")

    @property
    def module(self):
        return "stub"

    def poll(self):
        self.poll_count += 1
        return self._poll_values
```

Replace the topic + client_id assertions:

```python
class TestBasePublisher:
    @patch("sensors2mqtt.base.socket.gethostname", return_value="ten64")
    def test_topics_include_module(self, _gh):
        pub = StubPublisher(config=MqttConfig())
        assert pub.state_topic == "sensors2mqtt/test/stub/state"
        assert pub.avail_topic == "sensors2mqtt/test/stub/status"

    @patch("sensors2mqtt.base.socket.gethostname", return_value="ten64")
    def test_client_id_from_module(self, _gh):
        pub = StubPublisher(config=MqttConfig())
        assert pub.client_id == "sensors2mqtt-ten64-stub"
```

Add a `connection_status_topic` test in `TestClientIdFor` neighbourhood:

```python
class TestConnectionStatusTopic:
    @patch("sensors2mqtt.base.socket.gethostname")
    def test_topic(self, gethost):
        from sensors2mqtt.base import connection_status_topic
        gethost.return_value = "ten64.welland.mithis.com"
        assert connection_status_topic("snmp") == "sensors2mqtt/ten64/snmp/status"
```

In the existing `test_poll_once_failure` / `test_run_*` tests, update the hardcoded `"sensors2mqtt/test/status"` strings to `"sensors2mqtt/test/stub/status"` (the new per-module avail topic).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_base.py -v`
Expected: FAIL (`connection_status_topic` import error; topic assertions mismatch; `StubPublisher` is now abstract-incompatible until `module` lands).

- [ ] **Step 3: Implement in `base.py`**

Add `connection_status_topic` after `client_id_for`:

```python
def connection_status_topic(module: str) -> str:
    """Per-host, per-daemon connection status topic (Last-Will + heartbeat).

    ``sensors2mqtt/{host}/{module}/status`` — grouped under the host so all of a
    machine's topics live together, and namespaced by module so two collectors on
    one host never collide on a shared status topic.
    """
    return f"sensors2mqtt/{host_id()}/{module}/status"
```

In `BasePublisher`, replace the abstract `client_id` with an abstract `module`, derive `client_id`, and namespace the topics:

```python
    @property
    @abstractmethod
    def module(self) -> str:
        """Module token (e.g. 'local', 'hwmon'); identifies this daemon."""

    @property
    def client_id(self) -> str:
        return client_id_for(self.module)

    @property
    def state_topic(self) -> str:
        return f"sensors2mqtt/{self.device.node_id}/{self.module}/state"

    @property
    def avail_topic(self) -> str:
        return f"sensors2mqtt/{self.device.node_id}/{self.module}/status"
```

Update the imports at the top of `base.py` so `client_id_for` is referenced by the new `client_id` property (it is already defined in this module, no import needed) and add `publish_connection_diagnostic` to the discovery import:

```python
from sensors2mqtt.discovery import (
    DeviceInfo,
    SensorDef,
    publish_connection_diagnostic,
    publish_discovery,
    publish_state,
)
```

In `run()`, after `client.loop_start()` and before the poll loop, clear the legacy (pre-module) topics and publish the connection diagnostic:

```python
        client.loop_start()

        # One-time migration: clear legacy non-module-scoped retained topics.
        client.publish(f"sensors2mqtt/{self.device.node_id}/state", "", retain=True)
        client.publish(f"sensors2mqtt/{self.device.node_id}/status", "", retain=True)
        # Per-daemon connection diagnostic on the host device.
        publish_connection_diagnostic(
            client, self.device.node_id, self.module, self.device.name
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_base.py -v`
Expected: PASS. (Subclasses `LocalCollector`/`HwmonCollector` still define `client_id` not `module`; they are fixed in Tasks 3/5 — do not run the full suite yet.)

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/base.py tests/test_base.py
git commit -m "feat: per-module state/status topics + connection_status_topic; BasePublisher.module" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: local collector — `module = "local"`

**Files:**
- Modify: `src/sensors2mqtt/collector/local/base.py`
- Test: `tests/test_local_base.py`

- [ ] **Step 1: Update the client_id test to assert via module**

In `tests/test_local_base.py` `TestTopics`, the existing `test_client_id` already asserts `sensors2mqtt-rpi5_pmod-local` — keep it. Add:

```python
    @patch("sensors2mqtt.base.socket.gethostname", return_value="rpi5-pmod")
    def test_state_topic_includes_module(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.state_topic == "sensors2mqtt/rpi5_pmod/local/state"
        assert c.avail_topic == "sensors2mqtt/rpi5_pmod/local/status"
```

Update the existing `test_state_topic`/`test_avail_topic` expected values to the `/local/` form (`sensors2mqtt/rpi5_pmod/local/state` and `/status`).

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_local_base.py::TestTopics -v`
Expected: FAIL (`LocalCollector` has no `module`; topics lack `/local/`).

- [ ] **Step 3: Implement in `local/base.py`**

Replace the `client_id` property with `module`:

```python
    @property
    def module(self) -> str:
        return "local"
```

(Remove the old `client_id` property and the now-unused `client_id_for` import if present; `base.py` derives `client_id` from `module`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_local_base.py tests/test_local_rpi.py tests/test_local_mellanox.py tests/test_local_autodetect.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/local/base.py tests/test_local_base.py
git commit -m "refactor: local collector defines module=local (per-module topics)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: hwmon collector — `module = "hwmon"`

**Files:**
- Modify: `src/sensors2mqtt/collector/hwmon.py`
- Test: `tests/test_hwmon.py`

- [ ] **Step 1: Update topic tests**

In `tests/test_hwmon.py`, update `test_topics` expected values:

```python
    @patch("sensors2mqtt.base.socket.gethostname", return_value="sw-bb-25g")
    def test_topics(self, _mock):
        c = self.make_collector()
        assert c.state_topic == "sensors2mqtt/sw_bb_25g/hwmon/state"
        assert c.avail_topic == "sensors2mqtt/sw_bb_25g/hwmon/status"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_hwmon.py -v`
Expected: FAIL (no `module`; topics lack `/hwmon/`).

- [ ] **Step 3: Implement in `hwmon.py`**

Replace the `client_id` property with:

```python
    @property
    def module(self) -> str:
        return "hwmon"
```

Remove the now-unused `client_id_for` import (keep `host_id` — still used for `node_id`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_hwmon.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/hwmon.py tests/test_hwmon.py
git commit -m "refactor: hwmon collector defines module=hwmon (per-module topics)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: ipmi collector — per-module topics, connection diagnostic, PSU expire_after

**Files:**
- Modify: `src/sensors2mqtt/collector/ipmi_sensors.py`
- Test: `tests/test_ipmi_sensors.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_ipmi_sensors.py`:

```python
def test_psu_discovery_has_expire_after():
    import json
    from unittest.mock import MagicMock
    from sensors2mqtt.collector.ipmi_sensors import publish_psu_discovery
    from sensors2mqtt.discovery import DeviceInfo, EXPIRE_AFTER
    client = MagicMock()
    device = DeviceInfo(node_id="big_storage", name="big-storage", manufacturer="x", model="y")
    psu_data = {"psus": [{"slot": 1}]}
    publish_psu_discovery(client, device, psu_data, "sensors2mqtt/big_storage/ipmi_sensors/status")
    payloads = [json.loads(c.args[1]) for c in client.publish.call_args_list]
    assert payloads and all(p.get("expire_after") == EXPIRE_AFTER for p in payloads)
    assert payloads[0]["state_topic"] == "sensors2mqtt/big_storage/ipmi_sensors/psu1/state"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_ipmi_sensors.py::test_psu_discovery_has_expire_after -v`
Expected: FAIL (no `expire_after`; PSU topic lacks `/ipmi_sensors/`).

- [ ] **Step 3: Implement in `ipmi_sensors.py`**

Add `MODULE = "ipmi_sensors"` near `NODE_ID`, and update imports:

```python
from sensors2mqtt.base import (
    MqttConfig,
    client_id_for,
    connection_status_topic,
    host_id,
    make_client,
)
from sensors2mqtt.discovery import (
    ORIGIN,
    DeviceInfo,
    SensorDef,
    device_dict,
    publish_connection_diagnostic,
    publish_discovery,
    publish_state,
)
```
```python
NODE_ID = host_id()
MODULE = "ipmi_sensors"
```

In `publish_psu_discovery`, change the PSU state topic and add `expire_after` to both config dicts:

```python
        psu_state_topic = f"sensors2mqtt/{NODE_ID}/{MODULE}/psu{slot}/state"
```
Add `"expire_after": EXPIRE_AFTER,` to the measurement config and the status/serial config (import `EXPIRE_AFTER` from discovery). i.e. both dicts gain that key.

In `main()`, change the topics, client_id token, connection wiring, heartbeat, legacy clear:

```python
    state_topic = f"sensors2mqtt/{NODE_ID}/{MODULE}/state"
    avail_topic = f"sensors2mqtt/{NODE_ID}/{MODULE}/status"
    conn_topic = connection_status_topic(MODULE)  # == avail_topic
```
```python
    client = make_client(
        config, client_id_for(MODULE), will_topic=conn_topic,
    )
```
After `client.loop_start()`:
```python
    # One-time migration: clear legacy non-module-scoped retained topics.
    client.publish(f"sensors2mqtt/{NODE_ID}/state", "", retain=True)
    client.publish(f"sensors2mqtt/{NODE_ID}/status", "", retain=True)
    publish_connection_diagnostic(client, NODE_ID, MODULE, socket.gethostname())
```
In the poll loop, the existing `client.publish(avail_topic, "online", retain=True)` already heartbeats the connection topic (avail_topic == conn_topic). The PSU loop's `psu_topic` must match the new path:
```python
                        psu_topic = f"sensors2mqtt/{NODE_ID}/{MODULE}/psu{slot}/state"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ipmi_sensors.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/ipmi_sensors.py tests/test_ipmi_sensors.py
git commit -m "feat: ipmi per-module topics + connection diagnostic + PSU expire_after" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: snmp collector — drop bridge from availability, per-host connection, expire_after

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py`
- Test: `tests/test_snmp.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_snmp.py`:

```python
def test_port_discovery_drops_bridge_and_has_expire_after():
    import json
    from unittest.mock import MagicMock
    from sensors2mqtt.collector.snmp import _publish_port_discovery
    from sensors2mqtt.discovery import EXPIRE_AFTER
    sw = make_switch(model="m4300")  # existing helper; port_count>0
    client = MagicMock()
    _publish_port_discovery(client, sw, f"sensors2mqtt/{sw.node_id}/status")
    cfgs = [json.loads(c.args[1]) for c in client.publish.call_args_list]
    assert cfgs
    for c in cfgs:
        assert c["availability_topic"] == f"sensors2mqtt/{sw.node_id}/status"
        assert "availability" not in c  # no multi-topic list -> no bridge
        assert c["expire_after"] == EXPIRE_AFTER


def test_connection_status_topic_for_snmp(monkeypatch):
    import sensors2mqtt.base as base
    monkeypatch.setattr(base.socket, "gethostname", lambda: "ten64")
    from sensors2mqtt.base import connection_status_topic
    assert connection_status_topic("snmp") == "sensors2mqtt/ten64/snmp/status"
```

(If `make_switch` doesn't exist in `tests/test_snmp.py`, build a `SwitchConfig` inline as the existing tests do — reuse the module's existing construction pattern at `tests/test_snmp.py:29`.)

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_snmp.py::test_port_discovery_drops_bridge_and_has_expire_after -v`
Expected: FAIL (`_publish_port_discovery` still takes/uses `bridge_topic`; config has an `availability` list and no `expire_after`).

- [ ] **Step 3: Implement in `snmp.py`**

Update imports:

```python
import socket
...
from sensors2mqtt.base import (
    MqttConfig,
    client_id_for,
    connection_status_topic,
    host_id,
    make_client,
)
from sensors2mqtt.discovery import (
    DISCOVERY_PREFIX,
    EXPIRE_AFTER,
    ORIGIN,
    DeviceInfo,
    SensorDef,
    availability_config,
    device_dict,
    publish_connection_diagnostic,
    publish_discovery,
    publish_state,
)
```

Remove the `SNMP_BRIDGE_TOPIC` constant and add a legacy reference for cleanup:

```python
# Legacy fixed bridge topic (pre multi-host). Cleared on startup; no longer used.
_LEGACY_BRIDGE_TOPIC = "sensors2mqtt/snmp_bridge/status"
```

In `_publish_port_discovery`, drop the `bridge_topic` parameter, switch availability to the switch status only, and add `expire_after`:

```python
def _publish_port_discovery(
    client: mqtt.Client,
    switch: SwitchConfig,
    avail_topic: str,
    chassis_macs: dict[int, str] | None = None,
) -> int:
    ...
            config = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": port_state_topic,
                "value_template": f"{{{{ value_json.{value_key} }}}}",
                "device": port_dev_dict,
                "expire_after": EXPIRE_AFTER,
                **availability_config(avail_topic),
                "origin": ORIGIN,
            }
```

In `main()`, replace bridge wiring with the per-host connection topic:

```python
    conn_topic = connection_status_topic("snmp")

    def _on_connected(c: mqtt.Client) -> None:
        c.publish(conn_topic, "online", retain=True)
        publish_connection_diagnostic(c, host_id(), "snmp", socket.gethostname())

    client = make_client(
        config, client_id_for("snmp"),
        on_connected=_on_connected,
        will_topic=conn_topic,
    )

    log.info("Connecting to MQTT %s:%d", config.host, config.port)
    client.connect(config.host, config.port, keepalive=120)
    client.loop_start()
    client.publish(_LEGACY_BRIDGE_TOPIC, "", retain=True)  # one-time cleanup
```

Drop `bridge_topic=` from the `publish_discovery(...)` and `_publish_port_discovery(...)` calls. In the poll loop, after the per-switch publishing, heartbeat the connection topic once per cycle:

```python
            # heartbeat the collector's own connection liveness
            client.publish(conn_topic, "online", retain=True)
            if args.once:
                break
            stop_event.wait(timeout=config.poll_interval)
```

In `finally`, replace the bridge offline with the connection offline:

```python
    finally:
        for switch in collector.switches:
            client.publish(collector.avail_topic(switch), "offline", retain=True)
        client.publish(conn_topic, "offline", retain=True)
        client.disconnect()
        client.loop_stop()
        log.info("Disconnected from MQTT")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_snmp.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat: snmp drops bridge from availability; per-host connection + expire_after" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: snmp_control collector — drop bridge, per-host connection, underscore token

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp_control.py`
- Test: `tests/test_snmp_control.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_snmp_control.py` `TestBridgeAvailability`, replace the bridge assertions with no-bridge + per-host connection. Add:

```python
def test_toggle_availability_has_no_bridge(make_controller):
    import json
    ctrl = make_controller()
    ctrl._client = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    sw = ctrl.switches[0]
    ctrl.publish_discovery(sw)
    toggles = [
        json.loads(c.args[1]) for c in ctrl._client.publish.call_args_list
        if "poe_toggle/config" in c.args[0]
    ]
    assert toggles
    for cfg in toggles:
        topics = [a["topic"] for a in cfg["availability"]]
        assert topics == [
            f"sensors2mqtt/{sw.node_id}/status",
            f"sensors2mqtt/{sw.node_id}/port/01/poe/available",
        ]
        assert all("bridge" not in t for t in topics)


def test_force_availability_is_switch_status_only(make_controller):
    import json
    ctrl = make_controller()
    ctrl._client = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    sw = ctrl.switches[0]
    ctrl.publish_discovery(sw)
    forces = [
        json.loads(c.args[1]) for c in ctrl._client.publish.call_args_list
        if "poe_force/config" in c.args[0]
    ]
    assert forces
    for cfg in forces:
        assert cfg["availability_topic"] == f"sensors2mqtt/{sw.node_id}/status"
        assert "availability" not in cfg
```

(Reuse the existing controller-construction fixture/helper already in `tests/test_snmp_control.py`; if it is a plain function, adapt these two tests to that pattern. Remove/replace the old `test_toggle_availability_covers_switch_port_and_bridge` and the `SNMP_CONTROL_BRIDGE_TOPIC` import.)

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_snmp_control.py -v`
Expected: FAIL (availability lists still include the bridge; `SNMP_CONTROL_BRIDGE_TOPIC` import may error once removed in Step 3).

- [ ] **Step 3: Implement in `snmp_control.py`**

Update imports:

```python
import socket
...
from sensors2mqtt.base import (
    MqttConfig,
    client_id_for,
    connection_status_topic,
    host_id,
    make_client,
)
from sensors2mqtt.discovery import ORIGIN, availability_config, device_dict, publish_connection_diagnostic
```

Remove `SNMP_CONTROL_BRIDGE_TOPIC` and add the legacy cleanup constant:

```python
# Legacy fixed bridge topic (pre multi-host). Cleared on startup; no longer used.
_LEGACY_BRIDGE_TOPIC = "sensors2mqtt/snmp_control_bridge/status"
```

In `publish_discovery`, drop the bridge from each `availability_config(...)` call:

```python
                **availability_config(avail_topic, port_avail),   # toggle
                ...
                **availability_config(avail_topic, port_avail),   # cycle
                ...
                **availability_config(avail_topic),               # force
```

In `_on_mqtt_connected`, publish the connection topic + diagnostic instead of the bridge:

```python
    def _on_mqtt_connected(self, client: mqtt.Client) -> None:
        """Called on each successful MQTT (re)connect (from make_client)."""
        self._connected.set()
        conn_topic = connection_status_topic("snmp_control")
        client.publish(conn_topic, "online", retain=True)
        publish_connection_diagnostic(client, host_id(), "snmp_control", socket.gethostname())
        if not self._once:
            self._subscribe_commands(client)
```

In `run()`, update the client id token, will topic, legacy cleanup, heartbeat, and shutdown:

```python
        conn_topic = connection_status_topic("snmp_control")
        client = make_client(
            self.mqtt_config, client_id_for("snmp_control"),
            on_connected=self._on_mqtt_connected,
            will_topic=conn_topic,
        )
        client.on_message = self._on_message
        self._client = client
        ...
        client.loop_start()
        client.publish(_LEGACY_BRIDGE_TOPIC, "", retain=True)  # one-time cleanup
```

Add a heartbeat in the poll loop:

```python
                for sw in self.switches:
                    self.poll_all_ports(sw)
                    self.publish_all_poe_states(sw)
                    self.publish_availability(sw)
                client.publish(conn_topic, "online", retain=True)  # heartbeat
```

In `finally`, replace the bridge offline:

```python
            client.publish(conn_topic, "offline", retain=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_snmp_control.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/snmp_control.py tests/test_snmp_control.py
git commit -m "feat: snmp_control drops bridge from availability; per-host connection; underscore token" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -q`
Expected: PASS, 0 failures. (Was 271 before this plan; new tests raise the count.)

- [ ] **Step 2: Lint**

Run: `uv run ruff check src/ tests/`
Expected: `All checks passed!` (Fix any unused imports left from removed bridge constants / `client_id` properties.)

- [ ] **Step 3: Sanity-check emitted config (no bridge, expire_after present)**

Run:
```bash
uv run python - <<'PY'
import json
from unittest.mock import patch, MagicMock
with patch("sensors2mqtt.base.socket.gethostname", return_value="ten64"):
    from sensors2mqtt.discovery import publish_connection_diagnostic
    c = MagicMock(); publish_connection_diagnostic(c, "ten64", "snmp", "ten64")
    print(c.publish.call_args[0][0])
    print(json.loads(c.publish.call_args[0][1])["state_topic"])
PY
```
Expected:
```
homeassistant/binary_sensor/ten64/snmp_connection/config
sensors2mqtt/ten64/snmp/status
```

- [ ] **Step 4: Confirm no stray bridge references remain**

Run: `grep -rn "snmp_bridge\|snmp_control_bridge\|SNMP_BRIDGE_TOPIC\|SNMP_CONTROL_BRIDGE_TOPIC" src/`
Expected: only the two `_LEGACY_BRIDGE_TOPIC` string literals (used for one-time cleanup), nothing else.

- [ ] **Step 5: Final commit (if any lint fixes were needed)**

```bash
git add -A
git commit -m "chore: lint fixups for multi-host availability" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## After implementation

- Then unblocks task #10 (refresh `docs/mqtt-topic-inventory.md` to this model) and task #14 (open PR for the branch — **after approval**, never merge unprompted).
