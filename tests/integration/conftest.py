"""Integration test harness: a local snmpsim agent serving snmprec fixtures.

Skips the whole integration package when ezsnmp or snmpsim are unavailable
(e.g. a dev box without libsnmp-dev). CI installs both, so they always run there.
"""
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("ezsnmp", reason="ezsnmp (libnetsnmp) not installed")

SNMPREC_DIR = Path(__file__).parent.parent / "fixtures" / "snmprec"
_RESPONDER = "snmpsim-command-responder"


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def snmpsim_agent():
    """Start snmpsim serving tests/fixtures/snmprec, yield (host, port)."""
    if shutil.which(_RESPONDER) is None:
        pytest.skip("snmpsim-command-responder not on PATH")
    host, port = "127.0.0.1", _free_udp_port()
    proc = subprocess.Popen(
        [
            _RESPONDER,
            f"--data-dir={SNMPREC_DIR}",
            f"--agent-udpv4-endpoint={host}:{port}",
            "--logging-method=null",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Poll until the UDP responder answers (a GET of sysObjectID succeeds).
    deadline = time.monotonic() + 15
    import ezsnmp
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError(f"snmpsim exited early: {err.decode()[:500]}")
        try:
            ezsnmp.Session(
                hostname=host, remote_port=port, community="m4300", version=2,
                timeout=1, retries=0, use_numeric=True,
            ).get("1.3.6.1.2.1.1.2.0")
            ready = True
            break
        except Exception:
            time.sleep(0.3)
    if not ready:
        proc.terminate()
        pytest.skip("snmpsim agent did not become ready")
    yield host, port
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
