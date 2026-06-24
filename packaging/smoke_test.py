#!/usr/bin/env python3
"""Post-publish smoke test for the INSTALLED sensors2mqtt package.

Run from a repo checkout (for the committed snmprec fixtures) after installing
the published PyPI or Debian package on a fresh system:

    python3 packaging/smoke_test.py

This file lives in ``packaging/`` rather than ``src/``, so the repo's ``src/``
tree is never on ``sys.path`` — ``import sensors2mqtt`` resolves to the
*installed* package, which is the whole point: it proves the uploaded artifact
(and its declared dependencies, including ezsnmp) actually import and work.

It starts a local snmpsim agent against tests/fixtures/snmprec, polls the M4300
model end-to-end through SnmpCollector, and asserts known fixture values. Exits
0 on success, non-zero on any failure.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNMPREC_DIR = REPO / "tests" / "fixtures" / "snmprec"
RESPONDER = "snmpsim-command-responder"


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_snmpsim(host: str, port: int) -> subprocess.Popen:
    if shutil.which(RESPONDER) is None:
        sys.exit(f"FAIL: {RESPONDER} not on PATH (install snmpsim-lextudio)")
    cmd = [
        RESPONDER,
        f"--data-dir={SNMPREC_DIR}",
        f"--agent-udpv4-endpoint={host}:{port}",
        "--logging-method=null",
    ]
    # snmpsim refuses to run as root without a non-privileged user to drop to;
    # the deb-verify job runs in a root container. No-op for non-root.
    if os.geteuid() == 0:
        cmd += ["--process-user", "nobody", "--process-group", "nogroup"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _wait_ready(proc: subprocess.Popen, host: str, port: int) -> None:
    import ezsnmp

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.communicate()[1].decode()[:2000]
            sys.exit(f"FAIL: snmpsim exited early: {err}")
        try:
            ezsnmp.Session(
                hostname=host, remote_port=port, community="m4300",
                version=2, timeout=1, retries=0, use_numeric=True,
            ).get("1.3.6.1.2.1.1.2.0")
            return
        except Exception:
            time.sleep(0.3)
    proc.terminate()
    sys.exit("FAIL: snmpsim did not become ready")


def main() -> None:
    # Imports from the INSTALLED package (not src/) — this is what we verify.
    import sensors2mqtt
    from sensors2mqtt.base import MqttConfig
    from sensors2mqtt.collector.snmp import MODELS, SnmpCollector, SwitchConfig
    from sensors2mqtt.collector.snmp_control import PoeController
    from sensors2mqtt.snmp_client import SnmpClient

    print(f"installed sensors2mqtt {sensors2mqtt.__version__} from {sensors2mqtt.__file__}")
    print(f"snmp-control import OK: {PoeController.__name__}")

    host, port = "127.0.0.1", _free_udp_port()
    proc = _start_snmpsim(host, port)
    try:
        _wait_ready(proc, host, port)

        m = MODELS["m4300"]
        sw = SwitchConfig(
            node_id="smoke", name="smoke", host=f"{host}:{port}", community="m4300",
            manufacturer=m.manufacturer, model=m.model, port_count=m.port_count,
            poe_port_count=m.poe_port_count, write_community=None,
            sensors=list(m.sensors), walk_sensors=list(m.walk_sensors),
            box_walks=list(m.box_walks),
        )
        cfg = MqttConfig(host="x", port=1883, user="u", password="p")
        collector = SnmpCollector(
            config=cfg, switches=[sw],
            client_factory=lambda s: SnmpClient(s.host, s.community, timeout=2, retries=1),
        )
        values = collector.poll_switch(sw)
        assert values, "no sensor values polled from the m4300 fixture"
        temp, fan1 = values.get("temp"), values.get("fan1_rpm")
        assert temp == 65, f"temp expected 65, got {temp}"
        assert fan1 == 5280, f"fan1_rpm expected 5280, got {fan1}"
        print(f"SMOKE OK: polled {len(values)} sensors (temp={temp}, fan1_rpm={fan1})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
