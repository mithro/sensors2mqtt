#!/usr/bin/env python3
"""Build .deb packages for sensors2mqtt.

Creates:
  - sensors2mqtt-common_VERSION_all.deb  (wheel + dependencies)
  - sensors2mqtt-local_VERSION_all.deb   (systemd service)

Usage:
    python3 packaging/build-debs.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
PACKAGING_DIR = PROJECT_ROOT / "packaging"
DIST_DIR = PROJECT_ROOT / "dist"
WHEELS_DIR = PACKAGING_DIR / "wheels"


def get_version() -> str:
    """Read version from pyproject.toml."""
    import tomllib

    with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def build_wheel() -> Path:
    """Build the sensors2mqtt wheel."""
    print("=== Building wheel ===")
    subprocess.run(["uv", "build"], cwd=PROJECT_ROOT, check=True)
    version = get_version()
    whl = DIST_DIR / f"sensors2mqtt-{version}-py3-none-any.whl"
    if not whl.exists():
        print(f"ERROR: Expected wheel at {whl}")
        sys.exit(1)
    print(f"  Built: {whl.name}")
    return whl


def download_wheels() -> None:
    """Download dependency wheels for offline install."""
    print("=== Downloading dependency wheels ===")
    WHEELS_DIR.mkdir(exist_ok=True)

    # Only paho-mqtt is needed for the local collector
    # requests + deps are only for ipmi_sensors
    deps = ["paho-mqtt>=2"]

    subprocess.run(
        [
            "uv",
            "run",
            "pip",
            "download",
            "--dest",
            str(WHEELS_DIR),
            "--no-deps",
        ]
        + deps,
        cwd=PROJECT_ROOT,
        check=True,
    )

    # List downloaded wheels
    for whl in sorted(WHEELS_DIR.glob("*.whl")):
        print(f"  {whl.name}")


def build_deb(config_name: str, version: str) -> Path:
    """Build a .deb using nfpm with version substitution."""
    template_file = PACKAGING_DIR / f"nfpm-{config_name}.yaml"
    if not template_file.exists():
        print(f"ERROR: Config not found: {template_file}")
        sys.exit(1)

    # Substitute ${VERSION} in template
    content = template_file.read_text().replace("${VERSION}", version)
    generated = PACKAGING_DIR / f".nfpm-{config_name}-generated.yaml"
    generated.write_text(content)

    print(f"=== Building sensors2mqtt-{config_name} .deb ===")
    subprocess.run(
        [
            "nfpm",
            "package",
            "--config",
            str(generated),
            "--packager",
            "deb",
            "--target",
            str(DIST_DIR),
        ],
        cwd=PACKAGING_DIR,
        check=True,
    )
    generated.unlink()

    # Find the built .deb
    pattern = f"sensors2mqtt-{config_name}_{version}_*.deb"
    debs = list(DIST_DIR.glob(pattern))
    if not debs:
        print(f"ERROR: No .deb found matching {pattern}")
        sys.exit(1)
    print(f"  Built: {debs[0].name}")
    return debs[0]


def main() -> None:
    version = get_version()
    print(f"sensors2mqtt version: {version}")
    print()

    # Step 1: Build wheel
    build_wheel()
    print()

    # Step 2: Download dependency wheels
    download_wheels()
    print()

    # Step 3: Build .deb packages
    packages = ["common", "local"]
    built = []
    for pkg in packages:
        deb = build_deb(pkg, version)
        built.append(deb)
        print()

    print("=== Build complete ===")
    for deb in built:
        size_kb = deb.stat().st_size // 1024
        print(f"  {deb.name} ({size_kb} KB)")


if __name__ == "__main__":
    main()
