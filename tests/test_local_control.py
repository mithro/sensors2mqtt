"""Tests for the host power-control service (local_control).

SAFETY: every test that exercises a command path mocks
``sensors2mqtt.collector.local_control.subprocess.run`` — no test ever runs a
real ``shutdown``/``reboot``. Constructing or importing the controller never
touches subprocess.
"""

import json
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local_control import (
    ACTIONS,
    MODULE,
    PowerController,
)


def _make_controller(node_id="testhost", name="testhost") -> PowerController:
    """Build a PowerController with a mock MQTT client."""
    config = MqttConfig(host="test", port=1883, user="u", password="p")
    ctrl = PowerController(mqtt_config=config, node_id=node_id, name=name)
    ctrl._client = MagicMock()
    return ctrl


def _payload_for(ctrl, needle):
    """Return the parsed JSON payload of the first publish whose topic matches."""
    for c in ctrl._client.publish.call_args_list:
        if needle in c.args[0]:
            return json.loads(c.args[1])
    raise AssertionError(f"no publish matching {needle!r}")


def _msg(topic, payload, retain=False):
    m = MagicMock()
    m.topic = topic
    m.payload = payload
    m.retain = retain
    return m


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestActions:
    def test_module_token(self):
        assert MODULE == "local_control"

    def test_actions_are_shutdown_and_reboot(self):
        assert set(ACTIONS) == {"shutdown", "reboot"}

    def test_shutdown_argv(self):
        assert ACTIONS["shutdown"]["argv"] == ["/sbin/shutdown", "-h", "now"]

    def test_reboot_argv(self):
        assert ACTIONS["reboot"]["argv"] == ["/sbin/shutdown", "-r", "now"]

    def test_action_states_distinct(self):
        assert ACTIONS["shutdown"]["state"] == "shutting_down"
        assert ACTIONS["reboot"]["state"] == "rebooting"


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

class TestTopics:
    def test_default_node_id_is_host_id(self):
        with patch("sensors2mqtt.collector.local_control.host_id",
                   return_value="myhost"), \
             patch("sensors2mqtt.collector.local_control.host_name",
                   return_value="my-host"):
            ctrl = PowerController(MqttConfig())
        assert ctrl.node_id == "myhost"
        assert ctrl.host_name == "my-host"

    def test_topic_shapes(self):
        ctrl = _make_controller(node_id="dash")
        assert ctrl.status_topic == "sensors2mqtt/dash/local_control/status"
        assert ctrl.state_topic == "sensors2mqtt/dash/power/state"
        assert ctrl.command_topic("shutdown") == "sensors2mqtt/dash/power/shutdown/set"
        assert ctrl.command_topic("reboot") == "sensors2mqtt/dash/power/reboot/set"


# ---------------------------------------------------------------------------
# Subscription / connect
# ---------------------------------------------------------------------------

class TestSubscription:
    def test_subscribe_commands_covers_both_actions(self):
        ctrl = _make_controller(node_id="dash")
        client = MagicMock()
        ctrl._subscribe_commands(client)
        subscribed = {c.args[0] for c in client.subscribe.call_args_list}
        assert "sensors2mqtt/dash/power/shutdown/set" in subscribed
        assert "sensors2mqtt/dash/power/reboot/set" in subscribed

    def test_on_connect_resubscribes_and_signals(self):
        ctrl = _make_controller()
        ctrl._once = False
        client = MagicMock()
        ctrl._on_mqtt_connected(client)
        assert ctrl._connected.is_set()
        assert client.subscribe.called
        client.publish.assert_any_call(ctrl.status_topic, "online", retain=True)

    def test_on_connect_once_skips_subscribe(self):
        ctrl = _make_controller()
        ctrl._once = True
        client = MagicMock()
        ctrl._on_mqtt_connected(client)
        assert ctrl._connected.is_set()
        assert not client.subscribe.called


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------

class TestMessageRouting:
    def test_shutdown_press_dispatches(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/dash/power/shutdown/set", b"PRESS"))
        ctrl._executor.submit.assert_called_once()
        args = ctrl._executor.submit.call_args[0]
        assert args[0] == ctrl._handle_command
        assert args[1] == "shutdown"

    def test_reboot_press_dispatches(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/dash/power/reboot/set", b"PRESS"))
        ctrl._executor.submit.assert_called_once()
        assert ctrl._executor.submit.call_args[0][1] == "reboot"

    def test_non_press_payload_ignored(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/dash/power/shutdown/set", b"ON"))
        ctrl._executor.submit.assert_not_called()

    def test_empty_payload_ignored(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/dash/power/shutdown/set", b""))
        ctrl._executor.submit.assert_not_called()

    def test_retained_press_ignored(self):
        """A retained PRESS must never halt the host. The broker re-delivers
        retained messages on every (re)subscribe and after every reboot, so a
        retained PRESS (manual mosquitto_pub -r, buggy automation) would
        otherwise trigger a shutdown/boot loop. Only live presses act."""
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(
            ctrl._client, None,
            _msg("sensors2mqtt/dash/power/shutdown/set", b"PRESS", retain=True),
        )
        ctrl._executor.submit.assert_not_called()

    def test_unknown_action_ignored(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/dash/power/halt/set", b"PRESS"))
        ctrl._executor.submit.assert_not_called()

    def test_wrong_node_ignored(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/other/power/shutdown/set", b"PRESS"))
        ctrl._executor.submit.assert_not_called()

    def test_unrelated_topic_ignored(self):
        ctrl = _make_controller(node_id="dash")
        ctrl._executor = MagicMock()
        ctrl._on_message(ctrl._client, None,
                         _msg("sensors2mqtt/dash/power/state", b"PRESS"))
        ctrl._executor.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Command handling (subprocess MOCKED — never runs for real)
# ---------------------------------------------------------------------------

class TestHandleCommand:
    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_shutdown_invokes_correct_argv(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ctrl = _make_controller()
        ctrl._handle_command("shutdown")
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["/sbin/shutdown", "-h", "now"]

    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_reboot_invokes_correct_argv(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ctrl = _make_controller()
        ctrl._handle_command("reboot")
        assert mock_run.call_args[0][0] == ["/sbin/shutdown", "-r", "now"]

    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_ack_state_published_before_command(self, mock_run):
        ctrl = _make_controller()
        events = []
        ctrl._client.publish.side_effect = lambda *a, **k: events.append(("pub", a))

        def run(*a, **k):
            events.append(("run", a))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run
        ctrl._handle_command("shutdown")

        state_idx = next(
            i for i, (kind, a) in enumerate(events)
            if kind == "pub" and a[0] == ctrl.state_topic and a[1] == "shutting_down"
        )
        run_idx = next(i for i, (kind, _a) in enumerate(events) if kind == "run")
        assert state_idx < run_idx, "ack must be published before the command runs"

    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_busy_prevents_second_action(self, mock_run):
        ctrl = _make_controller()
        ctrl._busy = True
        ctrl._handle_command("shutdown")
        mock_run.assert_not_called()

    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_successful_command_keeps_busy(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ctrl = _make_controller()
        ctrl._handle_command("shutdown")
        assert ctrl._busy is True

    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_failed_command_resets_to_idle(self, mock_run):
        """A failed shutdown (e.g. not root) must NOT leave the host as if going
        down: reset busy + publish idle so the off-sequence never assumes off."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="must be root")
        ctrl = _make_controller()
        ctrl._handle_command("shutdown")
        assert ctrl._busy is False
        ctrl._client.publish.assert_any_call(ctrl.state_topic, "idle", retain=True)

    @patch("sensors2mqtt.collector.local_control.subprocess.run")
    def test_unknown_action_is_noop(self, mock_run):
        ctrl = _make_controller()
        ctrl._handle_command("halt")
        mock_run.assert_not_called()

    def test_reset_clears_busy_before_publishing_idle(self):
        """The fail-safe must clear _busy *before* announcing idle. Otherwise a
        retry press that races the reset sees state=idle (available) yet is
        rejected as busy and silently dropped."""
        ctrl = _make_controller()
        ctrl._busy = True
        busy_seen_when_idle_published = []
        real_publish = ctrl._publish_state

        def capture(state):
            if state == "idle":
                busy_seen_when_idle_published.append(ctrl._busy)
            return real_publish(state)

        ctrl._publish_state = capture
        ctrl._reset_after_failure()
        assert busy_seen_when_idle_published == [False]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discovery_count(self):
        ctrl = _make_controller()
        assert ctrl.publish_discovery() == 2

    def test_discovery_topics(self):
        ctrl = _make_controller(node_id="dash")
        ctrl.publish_discovery()
        topics = [c.args[0] for c in ctrl._client.publish.call_args_list]
        assert "homeassistant/button/dash/power_shutdown/config" in topics
        assert "homeassistant/button/dash/power_reboot/config" in topics

    def test_shutdown_payload(self):
        ctrl = _make_controller(node_id="dash", name="dash-host")
        ctrl.publish_discovery()
        p = _payload_for(ctrl, "power_shutdown/config")
        assert p["name"] == "Shutdown"
        assert p["unique_id"] == "dash_power_shutdown"
        assert p["command_topic"] == "sensors2mqtt/dash/power/shutdown/set"
        assert p["payload_press"] == "PRESS"
        assert p["entity_category"] == "config"
        assert p["device"]["identifiers"] == ["sensors2mqtt_dash"]
        assert p["device"]["name"] == "dash-host"
        assert p["availability_topic"] == "sensors2mqtt/dash/local_control/status"
        assert p["origin"]["name"] == "sensors2mqtt"
        assert p["icon"] == "mdi:power"

    def test_reboot_payload(self):
        ctrl = _make_controller(node_id="dash")
        ctrl.publish_discovery()
        p = _payload_for(ctrl, "power_reboot/config")
        assert p["name"] == "Reboot"
        assert p["unique_id"] == "dash_power_reboot"
        assert p["command_topic"] == "sensors2mqtt/dash/power/reboot/set"
        assert p["icon"] == "mdi:restart"

    def test_discovery_retained(self):
        ctrl = _make_controller()
        ctrl.publish_discovery()
        for c in ctrl._client.publish.call_args_list:
            assert c.kwargs.get("retain") is True, f"not retained: {c.args[0]}"


# ---------------------------------------------------------------------------
# Startup announce
# ---------------------------------------------------------------------------

class TestAnnounce:
    def test_announce_publishes_idle_discovery_online(self):
        ctrl = _make_controller()
        ctrl._announce()
        # stale retained shutting_down (from a prior boot) is reset to idle
        ctrl._client.publish.assert_any_call(ctrl.state_topic, "idle", retain=True)
        # online status
        ctrl._client.publish.assert_any_call(ctrl.status_topic, "online", retain=True)
        # both buttons discovered
        topics = [c.args[0] for c in ctrl._client.publish.call_args_list]
        assert any("power_shutdown/config" in t for t in topics)
        assert any("power_reboot/config" in t for t in topics)
