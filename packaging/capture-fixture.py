#!/usr/bin/env python3
"""Capture sysfs/proc fixture data from the current machine.

Run on a target RPi or Mellanox switch to create a fixture directory
that can be used in tests.

Usage:
    python3 capture-fixture.py [output_dir]

Output is written to ./fixture_capture/ by default.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def capture_file(src: Path, dst: Path) -> bool:
    """Copy a file, creating parent dirs. Returns True if successful."""
    try:
        content = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)
        return True
    except OSError:
        return False


def capture_sysfs_thermal(root: Path, out: Path) -> None:
    """Capture thermal zone data."""
    thermal_dir = root / "sys/class/thermal"
    if not thermal_dir.exists():
        return
    for zone in sorted(thermal_dir.glob("thermal_zone*")):
        zone_name = zone.name
        for fname in ("type", "temp"):
            src = zone / fname
            if src.exists():
                dst = out / "sys/class/thermal" / zone_name / fname
                if capture_file(src, dst):
                    print(f"  captured: sys/class/thermal/{zone_name}/{fname}")


def capture_sysfs_hwmon(root: Path, out: Path) -> None:
    """Capture hwmon driver data."""
    hwmon_dir = root / "sys/class/hwmon"
    if not hwmon_dir.exists():
        return
    for hwmon in sorted(hwmon_dir.glob("hwmon*")):
        hwmon_name = hwmon.name
        # Always capture the name file
        name_file = hwmon / "name"
        if name_file.exists():
            dst = out / "sys/class/hwmon" / hwmon_name / "name"
            capture_file(name_file, dst)
            driver = name_file.read_text().strip()
            print(f"  hwmon {hwmon_name}: driver={driver}")

        # Capture all sensor files
        for f in sorted(hwmon.glob("*")):
            if f.is_file() and f.name != "name" and not f.name.startswith("uevent"):
                # Only capture files that look like sensor data
                if any(
                    f.name.startswith(prefix)
                    for prefix in ("temp", "in", "fan", "pwm", "curr", "power")
                ):
                    dst = out / "sys/class/hwmon" / hwmon_name / f.name
                    if capture_file(f, dst):
                        print(f"  captured: sys/class/hwmon/{hwmon_name}/{f.name}")


def capture_cooling_fan(root: Path, out: Path) -> None:
    """Capture RPi 5 active cooler fan data."""
    fan_base = root / "sys/devices/platform/cooling_fan/hwmon"
    if not fan_base.exists():
        return
    for hwmon in sorted(fan_base.glob("hwmon*")):
        for f in sorted(hwmon.glob("fan*")):
            if f.is_file():
                rel = f.relative_to(root)
                dst = out / rel
                if capture_file(f, dst):
                    print(f"  captured: {rel}")


def capture_net_addresses(root: Path, out: Path) -> None:
    """Capture network interface MAC addresses."""
    net_dir = root / "sys/class/net"
    if not net_dir.exists():
        return
    for iface in sorted(net_dir.iterdir()):
        addr_file = iface / "address"
        if addr_file.exists():
            iface_name = iface.name
            # Skip virtual/loopback
            if iface_name in ("lo",) or iface_name.startswith("veth"):
                continue
            dst = out / "sys/class/net" / iface_name / "address"
            if capture_file(addr_file, dst):
                mac = addr_file.read_text().strip()
                print(f"  captured: {iface_name} MAC={mac}")


def capture_proc(root: Path, out: Path) -> None:
    """Capture /proc files."""
    for fname in ("uptime", "loadavg", "meminfo"):
        src = root / "proc" / fname
        if src.exists():
            capture_file(src, out / "proc" / fname)
            print(f"  captured: proc/{fname}")

    # Device tree (RPi-specific)
    for fname in ("model", "serial-number"):
        src = root / "proc/device-tree" / fname
        if src.exists():
            capture_file(src, out / "proc/device-tree" / fname)
            print(f"  captured: proc/device-tree/{fname}")


def capture_vcgencmd(out: Path) -> None:
    """Capture vcgencmd output if available."""
    if not shutil.which("vcgencmd"):
        print("  vcgencmd: not found")
        return

    commands = [
        ("measure_temp", "vcgencmd_measure_temp.txt"),
        ("measure_volts core", "vcgencmd_measure_volts_core.txt"),
        ("get_throttled", "vcgencmd_get_throttled.txt"),
    ]
    extra_dir = out / "extra"
    extra_dir.mkdir(parents=True, exist_ok=True)

    for cmd_args, filename in commands:
        try:
            result = subprocess.run(
                ["vcgencmd"] + cmd_args.split(),
                capture_output=True,
                text=True,
                timeout=5,
            )
            (extra_dir / filename).write_text(result.stdout)
            print(f"  vcgencmd {cmd_args}: {result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"  vcgencmd {cmd_args}: FAILED ({e})")


def capture_sensors_json(out: Path) -> None:
    """Capture sensors -j output if available."""
    if not shutil.which("sensors"):
        print("  sensors: not found")
        return

    try:
        result = subprocess.run(
            ["sensors", "-j"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            extra_dir = out / "extra"
            extra_dir.mkdir(parents=True, exist_ok=True)
            (extra_dir / "sensors_j.json").write_text(result.stdout)
            data = json.loads(result.stdout)
            print(f"  sensors -j: {len(data)} chips")
        else:
            print(f"  sensors -j: failed (rc={result.returncode})")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  sensors -j: FAILED ({e})")


def main():
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixture_capture")

    if output_dir.exists():
        print(f"Output directory {output_dir} already exists, removing...")
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True)
    root = Path("/")

    hostname = os.uname().nodename
    print(f"Capturing fixture data from {hostname}")
    print(f"Output: {output_dir.resolve()}")
    print()

    print("Thermal zones:")
    capture_sysfs_thermal(root, output_dir)
    print()

    print("Hwmon drivers:")
    capture_sysfs_hwmon(root, output_dir)
    print()

    print("Cooling fan:")
    capture_cooling_fan(root, output_dir)
    print()

    print("Network interfaces:")
    capture_net_addresses(root, output_dir)
    print()

    print("Proc files:")
    capture_proc(root, output_dir)
    print()

    print("vcgencmd:")
    capture_vcgencmd(output_dir)
    print()

    print("sensors -j:")
    capture_sensors_json(output_dir)
    print()

    # Write metadata
    meta = {
        "hostname": hostname,
        "uname": " ".join(os.uname()),
    }
    model_path = Path("/proc/device-tree/model")
    if model_path.exists():
        meta["model"] = model_path.read_text().rstrip("\x00")
    (output_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

    file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())
    print(f"Done. Captured {file_count} files to {output_dir.resolve()}")


if __name__ == "__main__":
    main()
