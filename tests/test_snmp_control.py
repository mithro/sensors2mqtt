"""Tests for PoE control service."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.snmp import MODELS, SwitchConfig
from sensors2mqtt.collector.snmp_control import (
    _LEGACY_BRIDGE_TOPIC,
    IF_OPER_OID,
    POE_ADMIN_OID,
    POE_DETECT_OID,
    PoeController,
    PortControlState,
)
from sensors2mqtt.snmp_client import SnmpRow
from snmp_helpers import FakeSnmpClient, rows_from_snmpwalk_txt

FIXTURES = Path(__file__).parent / "fixtures"


def _make_switch(name: str, model_name: str, write_community: str | None = None) -> SwitchConfig:
    """Helper to build a SwitchConfig from model for tests."""
    model = MODELS[model_name]
    return SwitchConfig(
        node_id=name.replace("-", "_"),
        name=name,
        host=f"{name}.test",
        community="public",
        manufacturer=model.manufacturer,
        model=model.model,
        port_count=model.port_count,
        poe_port_count=model.poe_port_count,
        write_community=write_community,
        sensors=list(model.sensors),
        walk_sensors=list(model.walk_sensors),
    )


def controller_with(switches, *, walk_rows=None, get_rows=None, set_ok=True, fakes=None):
    """Build a PoeController with an injected FakeSnmpClient and a mock MQTT client."""
    cfg = MqttConfig(host="test", port=1883, user="u", password="p")
    fake = FakeSnmpClient(walk_rows=walk_rows or {}, get_rows=get_rows or {}, set_ok=set_ok)
    ctrl = PoeController(
        mqtt_config=cfg,
        switches=switches,
        client_factory=lambda sw: (fakes or {}).get(sw.node_id, fake),
    )
    ctrl._mqtt_client = MagicMock()
    return ctrl, fake


def _make_controller(switches=None):
    """Helper to build a PoeController with mock MQTT and a default FakeSnmpClient."""
    if switches is None:
        switches = [
            _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private"),
        ]
    ctrl, _ = controller_with(switches)
    return ctrl


class TestCommandResubscription:
    """Command-topic subscriptions must be re-established on every reconnect.

    The broker drops subscriptions on disconnect (clean session) and paho does
    not resubscribe, so subscribing once at startup means a reconnect silently
    stops command delivery while polling/publishing keep working. The handler is
    wired to make_client's on_connect, which fires on every (re)connect.
    """

    def test_subscribe_commands_covers_all_topics_for_every_switch(self):
        ctrl = _make_controller(switches=[
            _make_switch("sw-a", "gsm7252ps", write_community="private"),
            _make_switch("sw-b", "gsm7252ps", write_community="private"),
        ])
        client = MagicMock()
        ctrl._subscribe_commands(client)
        subscribed = {c.args[0] for c in client.subscribe.call_args_list}
        for node in ("sw_a", "sw_b"):
            assert f"sensors2mqtt/{node}/port/+/poe/set" in subscribed
            assert f"sensors2mqtt/{node}/port/+/poe/cycle" in subscribed
            assert f"sensors2mqtt/{node}/port/+/poe/force/set" in subscribed

    def test_on_connect_resubscribes_and_signals_connected(self):
        ctrl = _make_controller()
        ctrl._once = False
        client = MagicMock()
        ctrl._on_mqtt_connected(client)
        assert ctrl._connected.is_set()
        assert client.subscribe.called

    def test_on_connect_in_once_mode_signals_but_skips_subscribe(self):
        ctrl = _make_controller()
        ctrl._once = True
        client = MagicMock()
        ctrl._on_mqtt_connected(client)
        assert ctrl._connected.is_set()
        assert not client.subscribe.called


class TestConnectionAvailability:
    """snmp_control uses a per-host connection topic (not a fixed bridge).

    The per-host connection topic is the Last-Will and the per-cycle heartbeat.
    Control entities list only the switch-level status and (for toggle/cycle)
    the per-port PoE available topic — no bridge topic.
    """

    def test_on_connect_publishes_connection_online(self):
        """_on_mqtt_connected publishes the per-host connection topic online."""
        conn_topic = "sensors2mqtt/testhost/snmp_control/status"
        ctrl = _make_controller()
        ctrl._once = False
        client = MagicMock()
        with patch(
            "sensors2mqtt.collector.snmp_control.connection_status_topic",
            return_value=conn_topic,
        ), patch(
            "sensors2mqtt.collector.snmp_control.publish_connection_diagnostic",
        ), patch(
            "sensors2mqtt.collector.snmp_control.host_id", return_value="testhost",
        ):
            ctrl._on_mqtt_connected(client)
        client.publish.assert_any_call(conn_topic, "online", retain=True)

    def test_toggle_availability_has_no_bridge(self):
        """Toggle availability: switch status + per-port available, no bridge."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)
        toggles = [
            json.loads(c.args[1]) for c in ctrl._mqtt_client.publish.call_args_list
            if "poe_toggle/config" in c.args[0]
        ]
        assert toggles
        for cfg in toggles:
            topics = [a["topic"] for a in cfg["availability"]]
            assert f"sensors2mqtt/{sw.node_id}/status" in topics
            assert any("/port/" in t for t in topics)
            assert all("bridge" not in t for t in topics)
            assert cfg["availability_mode"] == "all"

    def test_force_availability_is_switch_status_only(self):
        """Force override availability: switch status only (single topic, no list)."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)
        forces = [
            json.loads(c.args[1]) for c in ctrl._mqtt_client.publish.call_args_list
            if "poe_force/config" in c.args[0]
        ]
        assert forces
        for cfg in forces:
            assert cfg["availability_topic"] == f"sensors2mqtt/{sw.node_id}/status"
            assert "availability" not in cfg

    def test_legacy_bridge_topic_constant_exists(self):
        """Legacy bridge topic constant still exists for one-time cleanup."""
        assert _LEGACY_BRIDGE_TOPIC == "sensors2mqtt/snmp_control_bridge/status"


# ---------------------------------------------------------------------------
# PortControlState tests
# ---------------------------------------------------------------------------

class TestPortControlState:
    def test_default_state(self):
        s = PortControlState()
        assert s.poe_admin == 0
        assert s.poe_detect == 0
        assert s.link == 0
        assert s.force_override is False
        assert s.busy is False

    def test_available_when_link_down(self):
        """Link down = available regardless of PoE state."""
        s = PortControlState(link=2, poe_detect=1)  # down, unused
        assert s.is_available is True

    def test_available_when_delivering(self):
        """Link up + delivering = available."""
        s = PortControlState(link=1, poe_detect=3)  # up, delivering
        assert s.is_available is True

    def test_available_when_searching(self):
        """Link up + searching = available."""
        s = PortControlState(link=1, poe_detect=2)  # up, searching
        assert s.is_available is True

    def test_available_when_fault(self):
        """Link up + fault = available."""
        s = PortControlState(link=1, poe_detect=4)  # up, fault
        assert s.is_available is True

    def test_disabled_when_link_up_unused(self):
        """Link up + unused (not negotiated) = disabled."""
        s = PortControlState(link=1, poe_detect=1)  # up, unused
        assert s.is_available is False

    def test_force_override_enables_disabled(self):
        """Force override makes disabled port available."""
        s = PortControlState(link=1, poe_detect=1, force_override=True)
        assert s.is_available is True

    def test_poe_is_on_enabled(self):
        s = PortControlState(poe_admin=1)
        assert s.poe_is_on is True

    def test_poe_is_on_disabled(self):
        s = PortControlState(poe_admin=2)
        assert s.poe_is_on is False


# ---------------------------------------------------------------------------
# Controller init / filtering tests
# ---------------------------------------------------------------------------

class TestControllerInit:
    def test_filters_to_poe_switches_with_write_community(self):
        """Only switches with write_community AND poe_port_count > 0 are managed."""
        switches = [
            _make_switch("m4300", "m4300"),                                    # no PoE, no write
            _make_switch("gsm7252ps", "gsm7252ps", write_community="private"),  # PoE + write
            _make_switch("s3300", "s3300"),                                    # PoE but no write
        ]
        ctrl = _make_controller(switches)
        assert len(ctrl.switches) == 1
        assert ctrl.switches[0].name == "gsm7252ps"

    def test_m4300_excluded_even_with_write_community(self):
        """M4300 has 0 PoE ports — excluded even with write_community."""
        switches = [
            _make_switch("m4300", "m4300", write_community="private"),
        ]
        ctrl = _make_controller(switches)
        assert len(ctrl.switches) == 0

    def test_port_states_initialized(self):
        """Port states are created for all PoE ports."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        assert len(ctrl._port_states[sw.node_id]) == sw.poe_port_count
        assert 1 in ctrl._port_states[sw.node_id]
        assert sw.poe_port_count in ctrl._port_states[sw.node_id]


# ---------------------------------------------------------------------------
# SNMP helper tests
# ---------------------------------------------------------------------------

class TestSnmpHelpers:
    def test_snmpget_int(self):
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin_row = SnmpRow("1.3.6.1.2.1.105.1.1.1.3.1.5", "1", "INTEGER")
        ctrl, fake = controller_with([sw], get_rows={
            f"{POE_ADMIN_OID}.5": admin_row,
        })
        result = ctrl._snmpget_int(sw, POE_ADMIN_OID, 5)
        assert result == 1

    def test_snmpget_int_missing_oid(self):
        """A missing OID returns None."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, fake = controller_with([sw], get_rows={})
        result = ctrl._snmpget_int(sw, POE_ADMIN_OID, 5)
        assert result is None

    def test_snmpget_int_snmp_error(self):
        """SnmpError raised by client returns None."""
        from sensors2mqtt.snmp_client import SnmpError

        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, _ = controller_with([sw])

        class ErrorFake(FakeSnmpClient):
            def get(self, oid):
                raise SnmpError("timeout")

        ctrl._clients[sw.node_id] = ErrorFake()
        result = ctrl._snmpget_int(sw, POE_ADMIN_OID, 5)
        assert result is None

    def test_snmpset_int_success(self):
        """set_int called with correct OID and value; returns True."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, fake = controller_with([sw], set_ok=True)
        result = ctrl._snmpset_int(sw, POE_ADMIN_OID, 5, 2)
        assert result is True
        assert (f"{POE_ADMIN_OID}.5", 2) in fake.sets

    def test_snmpset_int_failure(self):
        """set_int returns False → _snmpset_int returns False."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, fake = controller_with([sw], set_ok=False)
        result = ctrl._snmpset_int(sw, POE_ADMIN_OID, 5, 1)
        assert result is False

    def test_snmpset_int_snmp_error(self):
        """SnmpError raised by client.set_int returns False."""
        from sensors2mqtt.snmp_client import SnmpError

        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, _ = controller_with([sw])

        class ErrorFake(FakeSnmpClient):
            def set_int(self, oid, value):
                raise SnmpError("timeout")

        ctrl._clients[sw.node_id] = ErrorFake()
        result = ctrl._snmpset_int(sw, POE_ADMIN_OID, 5, 1)
        assert result is False


# ---------------------------------------------------------------------------
# poll_all_ports tests
# ---------------------------------------------------------------------------

class TestPollAllPorts:
    def test_poll_all_ports_sets_state(self):
        """poll_all_ports populates port state from walk rows."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_poe_admin.txt").read_text()
        )
        detect = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_poe_detect.txt").read_text()
        )
        oper = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_ifoperstatus.txt").read_text()
        )
        ctrl, _ = controller_with([sw], walk_rows={
            "105.1.1.1.3.1": admin, "105.1.1.1.6.1": detect, "2.2.1.8": oper,
        })
        ctrl.poll_all_ports(sw)
        st = ctrl._port_states[sw.node_id][1]
        assert st.poe_admin in (1, 2)

    def test_poll_all_ports_ignores_out_of_range_ports(self):
        """Port indices outside [1, poe_port_count] are ignored."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        # The ifoperstatus fixture has ports > 52; those should be silently ignored
        oper = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_ifoperstatus.txt").read_text()
        )
        ctrl, _ = controller_with([sw], walk_rows={"2.2.1.8": oper})
        ctrl.poll_all_ports(sw)
        # No exception; port 417+ should not have been set
        assert 417 not in ctrl._port_states[sw.node_id]

    def test_poll_all_ports_walk_error_continues(self):
        """SnmpError on a walk OID is logged and skipped; other OIDs still run."""

        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_gsm7252ps_poe_admin.txt").read_text())
        ctrl, _ = controller_with([sw], walk_rows={"105.1.1.1.3.1": admin},
                                  fakes={sw.node_id: FakeSnmpClient(
                                      walk_rows={"105.1.1.1.3.1": admin},
                                      walk_error=["105.1.1.1.6.1", "2.2.1.8"],
                                  )})
        # Should not raise
        ctrl.poll_all_ports(sw)
        # poe_admin should be set from the first walk that succeeded
        assert ctrl._port_states[sw.node_id][1].poe_admin in (1, 2)


# ---------------------------------------------------------------------------
# Toggle mapping tests
# ---------------------------------------------------------------------------

class TestToggleMapping:
    """Verify ON/OFF → SNMP value mapping is correct.

    Critical: ON → i 1 (enable), OFF → i 2 (disable).
    Getting this backwards would disable PoE when the user wants to enable it.
    """

    def test_on_maps_to_1(self):
        """ON → set_int with value 1 (enable PoE)."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin_row = SnmpRow(f"{POE_ADMIN_OID}.1", "1", "INTEGER")
        detect_row = SnmpRow(f"{POE_DETECT_OID}.1", "3", "INTEGER")
        oper_row = SnmpRow(f"{IF_OPER_OID}.1", "1", "INTEGER")
        ctrl, fake = controller_with([sw], set_ok=True, get_rows={
            f"{POE_ADMIN_OID}.1": admin_row,
            f"{POE_DETECT_OID}.1": detect_row,
            f"{IF_OPER_OID}.1": oper_row,
        })
        ctrl._handle_toggle(sw, 1, "ON")
        # First set call must be the toggle SET with value 1
        assert len(fake.sets) >= 1
        assert fake.sets[0] == (f"{POE_ADMIN_OID}.1", 1)

    def test_off_maps_to_2(self):
        """OFF → set_int with value 2 (disable PoE)."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin_row = SnmpRow(f"{POE_ADMIN_OID}.1", "2", "INTEGER")
        detect_row = SnmpRow(f"{POE_DETECT_OID}.1", "1", "INTEGER")
        oper_row = SnmpRow(f"{IF_OPER_OID}.1", "2", "INTEGER")
        ctrl, fake = controller_with([sw], set_ok=True, get_rows={
            f"{POE_ADMIN_OID}.1": admin_row,
            f"{POE_DETECT_OID}.1": detect_row,
            f"{IF_OPER_OID}.1": oper_row,
        })
        ctrl._handle_toggle(sw, 1, "OFF")
        assert len(fake.sets) >= 1
        assert fake.sets[0] == (f"{POE_ADMIN_OID}.1", 2)

    def test_invalid_payload_ignored(self):
        """Invalid payload doesn't trigger set_int."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, fake = controller_with([sw])
        ctrl._handle_toggle(sw, 1, "INVALID")
        assert fake.sets == []

    def test_busy_port_ignored(self):
        """Busy port ignores toggle commands."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, fake = controller_with([sw])
        ctrl._port_states[sw.node_id][1].busy = True
        ctrl._handle_toggle(sw, 1, "ON")
        assert fake.sets == []

    def test_handle_toggle_sets_via_client(self):
        """_handle_toggle calls set_int on the correct OID with the right value."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin_row = SnmpRow("1.3.6.1.2.1.105.1.1.1.3.1.1", "1", "INTEGER")
        detect_row = SnmpRow("1.3.6.1.2.1.105.1.1.1.6.1.1", "3", "INTEGER")
        oper_row = SnmpRow("1.3.6.1.2.1.2.2.1.8.1", "1", "INTEGER")
        ctrl, fake = controller_with([sw], get_rows={
            "105.1.1.1.3.1.1": admin_row, "105.1.1.1.6.1.1": detect_row, "2.2.1.8.1": oper_row,
        })
        ctrl._handle_toggle(sw, 1, "ON")
        assert (f"{POE_ADMIN_OID}.1", 1) in fake.sets

    def test_toggle_publishes_state(self):
        """Toggle publishes confirmed state after verification."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        admin_row = SnmpRow(f"{POE_ADMIN_OID}.1", "1", "INTEGER")
        detect_row = SnmpRow(f"{POE_DETECT_OID}.1", "3", "INTEGER")
        oper_row = SnmpRow(f"{IF_OPER_OID}.1", "1", "INTEGER")
        ctrl, fake = controller_with([sw], set_ok=True, get_rows={
            f"{POE_ADMIN_OID}.1": admin_row,
            f"{POE_DETECT_OID}.1": detect_row,
            f"{IF_OPER_OID}.1": oper_row,
        })
        ctrl._handle_toggle(sw, 1, "ON")

        # Should have published PoE state via MQTT
        publish_calls = ctrl._mqtt_client.publish.call_args_list
        state_calls = [c for c in publish_calls if "/poe/state" in str(c)]
        assert len(state_calls) >= 1


# ---------------------------------------------------------------------------
# Force override tests
# ---------------------------------------------------------------------------

class TestForceOverride:
    def test_force_on(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._handle_force(sw, 1, "ON")
        assert ctrl._port_states[sw.node_id][1].force_override is True

        # Should publish retained force state
        publish_calls = ctrl._mqtt_client.publish.call_args_list
        force_calls = [c for c in publish_calls if "/poe/force/state" in str(c)]
        assert len(force_calls) >= 1
        # Check retained
        force_call = force_calls[0]
        assert force_call[1].get("retain", False) is True or force_call[0][2] is True

    def test_force_off(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._port_states[sw.node_id][1].force_override = True
        ctrl._handle_force(sw, 1, "OFF")
        assert ctrl._port_states[sw.node_id][1].force_override is False


# ---------------------------------------------------------------------------
# Availability tests
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_publish_availability(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        # Set port 1 to delivering (available), port 2 to link up + unused (disabled)
        ctrl._port_states[sw.node_id][1].link = 1
        ctrl._port_states[sw.node_id][1].poe_detect = 3
        ctrl._port_states[sw.node_id][2].link = 1
        ctrl._port_states[sw.node_id][2].poe_detect = 1

        ctrl.publish_availability(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        # Find port 01 and 02 availability
        p01_calls = [c for c in calls if "port/01/poe/available" in str(c)]
        p02_calls = [c for c in calls if "port/02/poe/available" in str(c)]
        assert len(p01_calls) >= 1
        assert len(p02_calls) >= 1
        assert p01_calls[0][0][1] == "online"   # delivering = available
        assert p02_calls[0][0][1] == "offline"   # unused = disabled


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discovery_count(self):
        """Each PoE port gets 3 entities: toggle, cycle, force."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        count = ctrl.publish_discovery(sw)
        assert count == sw.poe_port_count * 3

    def test_discovery_topics(self):
        """Discovery publishes to correct HA topic patterns."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        topics = [c[0][0] for c in calls]

        # Check port 01 has all three entity types
        assert any("homeassistant/switch/" in t and "poe_toggle" in t for t in topics)
        assert any("homeassistant/button/" in t and "poe_cycle" in t for t in topics)
        assert any("homeassistant/switch/" in t and "poe_force" in t for t in topics)

    def test_discovery_payload_toggle(self):
        """Toggle discovery payload has correct structure."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        toggle_calls = [c for c in calls if "poe_toggle" in str(c[0][0])]
        assert len(toggle_calls) > 0

        payload = json.loads(toggle_calls[0][0][1])
        assert payload["name"] == "PoE"
        assert payload["unique_id"] == f"{sw.node_id}_01_poe_toggle"
        assert payload["command_topic"].endswith("/poe/set")
        assert payload["state_topic"].endswith("/poe/state")
        assert payload["payload_on"] == "ON"
        assert payload["payload_off"] == "OFF"
        assert "origin" in payload
        assert payload["origin"]["name"] == "sensors2mqtt"
        assert "device" in payload

    def test_discovery_payload_cycle(self):
        """Cycle button discovery payload has correct structure."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        cycle_calls = [c for c in calls if "poe_cycle" in str(c[0][0])]
        assert len(cycle_calls) > 0

        payload = json.loads(cycle_calls[0][0][1])
        assert payload["name"] == "PoE Cycle"
        assert payload["payload_press"] == "PRESS"
        assert "origin" in payload

    def test_discovery_payload_force(self):
        """Force override discovery has entity_category: config."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        force_calls = [c for c in calls if "poe_force" in str(c[0][0])]
        assert len(force_calls) > 0

        payload = json.loads(force_calls[0][0][1])
        assert payload["name"] == "PoE Force"
        assert payload["entity_category"] == "config"
        assert "origin" in payload

    def test_discovery_retained(self):
        """All discovery messages are published with retain=True."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        for c in calls:
            # paho publish(topic, payload, qos, retain) — retain is keyword arg
            retain = c[1].get("retain", False)
            assert retain is True, f"Discovery not retained: {c[0][0]}"

    def test_toggle_availability_covers_switch_and_port_only(self):
        """Toggle availability spans switch status + per-port (no bridge)."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._mqtt_client.publish.call_args_list
        toggle_calls = [c for c in calls if "poe_toggle" in str(c[0][0])]
        payload = json.loads(toggle_calls[0][0][1])

        assert payload["availability_mode"] == "all"
        topics = [a["topic"] for a in payload["availability"]]
        assert f"sensors2mqtt/{sw.node_id}/status" in topics      # switch
        assert any("/port/" in t for t in topics)                 # per-port
        assert all("bridge" not in t for t in topics)             # no bridge


# ---------------------------------------------------------------------------
# Message routing tests
# ---------------------------------------------------------------------------

class TestMessageRouting:
    def test_toggle_message_dispatches(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = f"sensors2mqtt/{sw.node_id}/port/01/poe/set"
        msg.payload = b"ON"

        ctrl._on_message(ctrl._mqtt_client, None, msg)
        ctrl._executor.submit.assert_called_once()
        args = ctrl._executor.submit.call_args[0]
        assert args[0] == ctrl._handle_toggle
        assert args[1] == sw
        assert args[2] == 1   # port
        assert args[3] == "ON"

    def test_cycle_message_dispatches(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = f"sensors2mqtt/{sw.node_id}/port/05/poe/cycle"
        msg.payload = b"PRESS"

        ctrl._on_message(ctrl._mqtt_client, None, msg)
        ctrl._executor.submit.assert_called_once()
        args = ctrl._executor.submit.call_args[0]
        assert args[0] == ctrl._handle_cycle
        assert args[2] == 5

    def test_force_message_dispatches(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = f"sensors2mqtt/{sw.node_id}/port/10/poe/force/set"
        msg.payload = b"ON"

        ctrl._on_message(ctrl._mqtt_client, None, msg)
        ctrl._executor.submit.assert_called_once()
        args = ctrl._executor.submit.call_args[0]
        assert args[0] == ctrl._handle_force
        assert args[2] == 10
        assert args[3] == "ON"

    def test_unknown_switch_ignored(self):
        ctrl = _make_controller()
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = "sensors2mqtt/unknown_switch/port/01/poe/set"
        msg.payload = b"ON"

        ctrl._on_message(ctrl._mqtt_client, None, msg)
        ctrl._executor.submit.assert_not_called()

    def test_invalid_port_ignored(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = f"sensors2mqtt/{sw.node_id}/port/99/poe/set"
        msg.payload = b"ON"

        ctrl._on_message(ctrl._mqtt_client, None, msg)
        ctrl._executor.submit.assert_not_called()

    def test_unrelated_topic_ignored(self):
        ctrl = _make_controller()
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = "sensors2mqtt/some_switch/port/01/state"
        msg.payload = b"{}"

        ctrl._on_message(ctrl._mqtt_client, None, msg)
        ctrl._executor.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Power cycle tests
# ---------------------------------------------------------------------------

class TestPowerCycle:
    def test_cycle_sequence(self):
        """Power cycle calls: set disable, poll gets off, set enable, poll gets delivering."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")

        # Provide GET responses that first show off (disabled/down) then delivering
        call_tracker = {"count": 0}

        class CycleStateFake(FakeSnmpClient):
            def get(self, oid):
                call_tracker["count"] += 1
                n = call_tracker["count"]
                if POE_DETECT_OID in oid:
                    # After disable: return unused (1); after enable: delivering (3)
                    val = "1" if n < 10 else "3"
                    return SnmpRow(oid, val, "INTEGER")
                if IF_OPER_OID in oid:
                    val = "2" if n < 10 else "1"
                    return SnmpRow(oid, val, "INTEGER")
                if POE_ADMIN_OID in oid:
                    val = "2" if n < 10 else "1"
                    return SnmpRow(oid, val, "INTEGER")
                return SnmpRow(oid, "0", "INTEGER")

        fake = CycleStateFake(set_ok=True)
        cfg = MqttConfig(host="test", port=1883, user="u", password="p")
        ctrl = PoeController(
            mqtt_config=cfg,
            switches=[sw],
            client_factory=lambda s: fake,
        )
        ctrl._mqtt_client = MagicMock()

        ctrl._handle_cycle(sw, 1)

        # Verify set_int was called at least twice (disable + enable)
        set_calls = fake.sets
        assert len(set_calls) >= 2
        # First set = disable (value 2), second = enable (value 1)
        assert set_calls[0][1] == 2  # disable
        assert set_calls[1][1] == 1  # enable

    def test_cycle_busy_rejected(self):
        """Busy port rejects cycle command."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private")
        ctrl, fake = controller_with([sw])
        ctrl._port_states[sw.node_id][1].busy = True
        ctrl._handle_cycle(sw, 1)
        assert fake.sets == []


# ---------------------------------------------------------------------------
# Discovery short-name tests
# ---------------------------------------------------------------------------

class TestDiscoveryShortNames:
    """Entity names are short — device name identifies the port."""

    def test_toggle_name_is_short(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)
        calls = ctrl._mqtt_client.publish.call_args_list
        toggle_01 = [c for c in calls if "port01_poe_toggle" in str(c[0][0])]
        payload = json.loads(toggle_01[0][0][1])
        assert payload["name"] == "PoE"

    def test_cycle_name_is_short(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)
        calls = ctrl._mqtt_client.publish.call_args_list
        cycle_01 = [c for c in calls if "port01_poe_cycle" in str(c[0][0])]
        payload = json.loads(cycle_01[0][0][1])
        assert payload["name"] == "PoE Cycle"

    def test_force_name_is_short(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)
        calls = ctrl._mqtt_client.publish.call_args_list
        force_01 = [c for c in calls if "port01_poe_force" in str(c[0][0])]
        payload = json.loads(force_01[0][0][1])
        assert payload["name"] == "PoE Force"
