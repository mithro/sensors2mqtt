"""Tests for PoE control service."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.snmp import MODELS, SwitchConfig
from sensors2mqtt.collector.snmp_control import (
    IF_OPER_OID,
    POE_ADMIN_OID,
    POE_DETECT_OID,
    PoeController,
    PortControlState,
)


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


def _make_controller(switches=None):
    """Helper to build a PoeController with mock MQTT."""
    if switches is None:
        switches = [
            _make_switch("test-gsm7252ps", "gsm7252ps", write_community="private"),
        ]
    config = MqttConfig(host="test", port=1883, user="u", password="p")
    ctrl = PoeController(mqtt_config=config, switches=switches)
    ctrl._client = MagicMock()
    return ctrl


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
    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_snmpget_int(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="iso.3.6.1... = INTEGER: 1\n", stderr="",
        )
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        result = ctrl._snmpget_int(sw, POE_ADMIN_OID, 5)
        assert result == 1
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "snmpget" in args[0]
        assert f"{POE_ADMIN_OID}.5" in args

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_snmpget_int_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Timeout")
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        result = ctrl._snmpget_int(sw, POE_ADMIN_OID, 5)
        assert result is None

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_snmpget_int_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="snmpget", timeout=10)
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        result = ctrl._snmpget_int(sw, POE_ADMIN_OID, 5)
        assert result is None

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_snmpset_int_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        result = ctrl._snmpset_int(sw, POE_ADMIN_OID, 5, 2)
        assert result is True
        args = mock_run.call_args[0][0]
        assert "snmpset" in args[0]
        assert "-c" in args
        assert "private" in args  # write_community
        assert f"{POE_ADMIN_OID}.5" in args
        assert "i" in args
        assert "2" in args

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_snmpset_int_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        result = ctrl._snmpset_int(sw, POE_ADMIN_OID, 5, 1)
        assert result is False


# ---------------------------------------------------------------------------
# Toggle mapping tests
# ---------------------------------------------------------------------------

class TestToggleMapping:
    """Verify ON/OFF → SNMP value mapping is correct.

    Critical: ON → i 1 (enable), OFF → i 2 (disable).
    Getting this backwards would disable PoE when the user wants to enable it.
    """

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_on_maps_to_1(self, mock_run):
        """ON → snmpset ... i 1 (enable PoE)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="iso.3.6.1... = INTEGER: 1\n", stderr="",
        )
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._handle_toggle(sw, 1, "ON")

        # First call is snmpset (toggle), second+ are snmpget (verify)
        set_call = mock_run.call_args_list[0]
        args = set_call[0][0]
        assert "snmpset" in args[0]
        assert "i" in args
        assert "1" in args  # enable

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_off_maps_to_2(self, mock_run):
        """OFF → snmpset ... i 2 (disable PoE)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="iso.3.6.1... = INTEGER: 2\n", stderr="",
        )
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._handle_toggle(sw, 1, "OFF")

        set_call = mock_run.call_args_list[0]
        args = set_call[0][0]
        assert "snmpset" in args[0]
        assert "i" in args
        assert "2" in args  # disable

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_invalid_payload_ignored(self, mock_run):
        """Invalid payload doesn't trigger snmpset."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._handle_toggle(sw, 1, "INVALID")
        mock_run.assert_not_called()

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_busy_port_ignored(self, mock_run):
        """Busy port ignores toggle commands."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._port_states[sw.node_id][1].busy = True
        ctrl._handle_toggle(sw, 1, "ON")
        mock_run.assert_not_called()

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_toggle_publishes_state(self, mock_run):
        """Toggle publishes confirmed state after verification."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="iso.3.6.1... = INTEGER: 1\n", stderr="",
        )
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._handle_toggle(sw, 1, "ON")

        # Should have published PoE state via MQTT
        publish_calls = ctrl._client.publish.call_args_list
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
        publish_calls = ctrl._client.publish.call_args_list
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

        calls = ctrl._client.publish.call_args_list
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

        calls = ctrl._client.publish.call_args_list
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

        calls = ctrl._client.publish.call_args_list
        toggle_calls = [c for c in calls if "poe_toggle" in str(c[0][0])]
        assert len(toggle_calls) > 0

        payload = json.loads(toggle_calls[0][0][1])
        assert payload["name"] == "Port 01 PoE"
        assert payload["unique_id"] == f"{sw.node_id}_port01_poe_toggle"
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

        calls = ctrl._client.publish.call_args_list
        cycle_calls = [c for c in calls if "poe_cycle" in str(c[0][0])]
        assert len(cycle_calls) > 0

        payload = json.loads(cycle_calls[0][0][1])
        assert payload["name"] == "Port 01 PoE Cycle"
        assert payload["payload_press"] == "PRESS"
        assert "origin" in payload

    def test_discovery_payload_force(self):
        """Force override discovery has entity_category: config."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._client.publish.call_args_list
        force_calls = [c for c in calls if "poe_force" in str(c[0][0])]
        assert len(force_calls) > 0

        payload = json.loads(force_calls[0][0][1])
        assert payload["name"] == "Port 01 PoE Force"
        assert payload["entity_category"] == "config"
        assert "origin" in payload

    def test_discovery_retained(self):
        """All discovery messages are published with retain=True."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._client.publish.call_args_list
        for c in calls:
            # paho publish(topic, payload, qos, retain) — retain is keyword arg
            retain = c[1].get("retain", False)
            assert retain is True, f"Discovery not retained: {c[0][0]}"

    def test_toggle_dual_availability(self):
        """Toggle and cycle use dual availability (switch + per-port)."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl.publish_discovery(sw)

        calls = ctrl._client.publish.call_args_list
        toggle_calls = [c for c in calls if "poe_toggle" in str(c[0][0])]
        payload = json.loads(toggle_calls[0][0][1])

        assert "availability" in payload
        assert isinstance(payload["availability"], list)
        assert len(payload["availability"]) == 2
        assert payload["availability_mode"] == "all"


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

        ctrl._on_message(ctrl._client, None, msg)
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

        ctrl._on_message(ctrl._client, None, msg)
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

        ctrl._on_message(ctrl._client, None, msg)
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

        ctrl._on_message(ctrl._client, None, msg)
        ctrl._executor.submit.assert_not_called()

    def test_invalid_port_ignored(self):
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = f"sensors2mqtt/{sw.node_id}/port/99/poe/set"
        msg.payload = b"ON"

        ctrl._on_message(ctrl._client, None, msg)
        ctrl._executor.submit.assert_not_called()

    def test_unrelated_topic_ignored(self):
        ctrl = _make_controller()
        ctrl._executor = MagicMock()

        msg = MagicMock()
        msg.topic = "sensors2mqtt/some_switch/port/01/state"
        msg.payload = b"{}"

        ctrl._on_message(ctrl._client, None, msg)
        ctrl._executor.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Power cycle tests
# ---------------------------------------------------------------------------

class TestPowerCycle:
    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_cycle_sequence(self, mock_run):
        """Power cycle calls: snmpset disable, snmpget polls, snmpset enable, snmpget polls."""
        # Mock responses: first snmpset (disable) ok, then polls show off, then enable ok,
        # then polls show delivering
        call_count = [0]
        def mock_side_effect(cmd, **kwargs):
            call_count[0] += 1
            # snmpset calls return success
            if "snmpset" in cmd[0]:
                return MagicMock(returncode=0, stdout="ok\n", stderr="")
            # snmpget calls — vary based on which OID
            oid_str = cmd[-1]
            if POE_DETECT_OID in oid_str:
                # After disable: return unused (1), then after enable: delivering (3)
                if call_count[0] < 10:
                    return MagicMock(returncode=0, stdout="iso... = INTEGER: 1\n", stderr="")
                return MagicMock(returncode=0, stdout="iso... = INTEGER: 3\n", stderr="")
            if IF_OPER_OID in oid_str:
                if call_count[0] < 10:
                    return MagicMock(returncode=0, stdout="iso... = INTEGER: 2\n", stderr="")
                return MagicMock(returncode=0, stdout="iso... = INTEGER: 1\n", stderr="")
            if POE_ADMIN_OID in oid_str:
                if call_count[0] < 10:
                    return MagicMock(returncode=0, stdout="iso... = INTEGER: 2\n", stderr="")
                return MagicMock(returncode=0, stdout="iso... = INTEGER: 1\n", stderr="")
            return MagicMock(returncode=0, stdout="iso... = INTEGER: 0\n", stderr="")

        mock_run.side_effect = mock_side_effect

        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._handle_cycle(sw, 1)

        # Verify snmpset was called at least twice (disable + enable)
        set_calls = [c for c in mock_run.call_args_list if "snmpset" in c[0][0][0]]
        assert len(set_calls) >= 2

    @patch("sensors2mqtt.collector.snmp_control.subprocess.run")
    def test_cycle_busy_rejected(self, mock_run):
        """Busy port rejects cycle command."""
        ctrl = _make_controller()
        sw = ctrl.switches[0]
        ctrl._port_states[sw.node_id][1].busy = True
        ctrl._handle_cycle(sw, 1)
        mock_run.assert_not_called()
