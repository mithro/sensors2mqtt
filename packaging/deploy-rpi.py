#!/usr/bin/env python3
"""Deploy sensors2mqtt-local to RPi devices via apt.

Adds the apt source and installs the package on each target host.

Usage:
    python3 packaging/deploy-rpi.py [hostname...]
    python3 packaging/deploy-rpi.py --all-iot
"""

import argparse
import os
import subprocess
import sys

IOT_HOSTS: list[tuple[str, str]] = [
    # Add your RPi hosts here as ("user", "hostname") tuples
    # Example: ("pi", "rpi5.local"),
]

APT_REPO_URL = os.environ.get("APT_REPO_URL", "")
GPG_KEY_URL = f"{APT_REPO_URL}sensors2mqtt.gpg"


def ssh_run(user: str, host: str, cmd: str, timeout: int = 120) -> bool:
    """Run a command via SSH. Returns True on success."""
    print(f"  $ {cmd}")
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", f"{user}@{host}", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        print(f"    FAILED (rc={result.returncode}): {result.stderr.strip()}")
        return False
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines()[-3:]:
            print(f"    {line}")
    return True


def deploy_host(user: str, host: str) -> bool:
    """Deploy sensors2mqtt-local to a single host."""
    print(f"\n=== {user}@{host} ===")

    # Check if already installed
    if ssh_run(user, host, "dpkg -l sensors2mqtt-local 2>/dev/null | grep -q ^ii"):
        print("  Already installed, upgrading...")

    # Add GPG key (idempotent)
    ok = ssh_run(
        user, host,
        f"curl -sf {GPG_KEY_URL} | sudo gpg --yes --dearmor -o /etc/apt/keyrings/sensors2mqtt.gpg"
    )
    if not ok:
        return False

    # Add apt source (idempotent)
    ok = ssh_run(
        user, host,
        f'echo "deb [signed-by=/etc/apt/keyrings/sensors2mqtt.gpg] {APT_REPO_URL} trixie main"'
        " | sudo tee /etc/apt/sources.list.d/sensors2mqtt.list"
    )
    if not ok:
        return False

    # apt update
    ok = ssh_run(user, host, "sudo apt update -qq")
    if not ok:
        return False

    # Install
    ok = ssh_run(user, host, "sudo apt install -y sensors2mqtt-local")
    if not ok:
        return False

    # Start service
    ok = ssh_run(user, host, "sudo systemctl start sensors2mqtt-local")
    if not ok:
        return False

    # Verify
    ok = ssh_run(user, host, "systemctl is-active sensors2mqtt-local")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Deploy sensors2mqtt to RPis")
    parser.add_argument("--all-iot", action="store_true", help="Deploy to all IoT RPis")
    parser.add_argument("hosts", nargs="*", help="user@host pairs to deploy to")
    args = parser.parse_args()

    if not APT_REPO_URL:
        print("ERROR: APT_REPO_URL environment variable is required", file=sys.stderr)
        sys.exit(1)

    if args.all_iot:
        targets = IOT_HOSTS
    elif args.hosts:
        targets = []
        for h in args.hosts:
            if "@" in h:
                user, host = h.split("@", 1)
            else:
                user, host = "tim", h
            targets.append((user, host))
    else:
        parser.print_help()
        sys.exit(1)

    results = {}
    for user, host in targets:
        try:
            results[host] = deploy_host(user, host)
        except subprocess.TimeoutExpired:
            print("  TIMEOUT")
            results[host] = False

    print("\n=== Summary ===")
    for host, ok in results.items():
        status = "OK" if ok else "FAILED"
        print(f"  {host}: {status}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
