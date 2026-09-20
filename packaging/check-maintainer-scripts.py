#!/usr/bin/env python3
"""Assert the systemd start/restart policy baked into the built .debs.

``debian/rules`` does not contain this logic: ``dh_installsystemd`` *generates*
maintainer-script fragments from ``/usr/share/debhelper/autoscripts/`` and
splices them into ``preinst``/``postinst``. The only place the real policy is
observable is inside the built package, so that is what this checks.

The policy (see issue #36)
--------------------------

Four packages ship a daemon that cannot run until an admin has written its
config (``/etc/sensors2mqtt/env``, ``snmp.toml``). They must therefore:

  * NOT be started by a fresh install -- there is nothing to connect to yet;
  * BE restarted by an upgrade, but only if they were already running.

``dh_installsystemd --no-start --restart-after-upgrade`` produces exactly that
pair, via the ``postinst-systemd-restartnostart`` autoscript whose action is
``try-restart`` (a no-op on a stopped unit).

``--no-start`` on its own does NOT: it suppresses the restart as well, *and*
still emits ``preinst-systemd-stop``. The net effect of an upgrade was then
"stop the daemon and never start it again", which is what #36 reported.

``sensors2mqtt-local`` is deliberately excluded: it auto-detects its hardware
and needs no per-host config, so starting it on install is correct.

Usage::

    python3 packaging/check-maintainer-scripts.py ../*.deb
    python3 packaging/check-maintainer-scripts.py built-debs/
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

# Packages whose daemon needs config before it can run: no start on install,
# try-restart on upgrade.
RESTART_ONLY = {
    "sensors2mqtt-snmp",
    "sensors2mqtt-snmp-control",
    "sensors2mqtt-local-control",
    "sensors2mqtt-ipmi-sensors",
}

# Packages that are safe to start the moment they are installed.
START_ON_INSTALL = {"sensors2mqtt-local"}


def control_scripts(deb: Path) -> dict[str, str]:
    """Return {script_name: contents} from a .deb's control archive."""
    raw = subprocess.run(
        ["dpkg-deb", "--ctrl-tarfile", str(deb)],
        capture_output=True, check=True,
    ).stdout
    scripts = {}
    with tarfile.open(fileobj=BytesIO(raw)) as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            if name in {"preinst", "postinst", "prerm", "postrm"}:
                handle = tar.extractfile(member)
                if handle is not None:
                    scripts[name] = handle.read().decode()
    return scripts


def package_name(deb: Path) -> str:
    return subprocess.run(
        ["dpkg-deb", "--field", str(deb), "Package"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def check(deb: Path) -> list[str]:
    """Return a list of policy violations for one .deb (empty == OK)."""
    pkg = package_name(deb)
    if pkg not in RESTART_ONLY and pkg not in START_ON_INSTALL:
        return []

    scripts = control_scripts(deb)
    preinst = scripts.get("preinst", "")
    postinst = scripts.get("postinst", "")
    unit = f"'{pkg}.service'"
    problems = []

    if pkg in RESTART_ONLY:
        if f"deb-systemd-invoke try-restart {unit}" not in postinst:
            problems.append(
                f"{pkg}: postinst does not try-restart {unit} on upgrade. "
                f"An upgrade will leave the daemon on the old code (or stopped). "
                f"debian/rules must pass --restart-after-upgrade alongside --no-start."
            )
        if "deb-systemd-invoke stop" in preinst:
            problems.append(
                f"{pkg}: preinst stops the unit before upgrade, but nothing starts "
                f"it again -- the daemon stays dead. --restart-after-upgrade "
                f"suppresses this preinst fragment."
            )
        if f"deb-systemd-invoke start {unit}" in postinst:
            problems.append(
                f"{pkg}: postinst starts {unit} on a fresh install, but this daemon "
                f"has no config yet and will crash-loop. --no-start must be kept."
            )

    if pkg in START_ON_INSTALL:
        # debhelper's postinst-systemd-restart template picks the verb at run
        # time (`_dh_action=start` on install, `restart` on upgrade) and invokes
        # it as `deb-systemd-invoke $_dh_action`, so match that indirection
        # rather than a literal `start`.
        if f"deb-systemd-invoke $_dh_action {unit}" not in postinst:
            problems.append(
                f"{pkg}: postinst no longer starts {unit} on install. This package "
                f"needs no per-host config and is expected to start immediately."
            )

    return problems


def collect(args: list[str]) -> list[Path]:
    debs: list[Path] = []
    for arg in args:
        path = Path(arg)
        debs.extend(sorted(path.glob("*.deb")) if path.is_dir() else [path])
    return debs


def main() -> int:
    debs = collect(sys.argv[1:])
    if not debs:
        print("usage: check-maintainer-scripts.py <dir|*.deb>", file=sys.stderr)
        return 2

    problems: list[str] = []
    seen: set[str] = set()
    for deb in debs:
        pkg = package_name(deb)
        found = check(deb)
        if pkg in RESTART_ONLY or pkg in START_ON_INSTALL:
            seen.add(pkg)
            print(f"{'FAIL' if found else 'ok  '}  {pkg}  ({deb.name})")
        problems.extend(found)

    if problems:
        # Deduplicated: the same package built at several versions reports the
        # same violation once per .deb, which is noise rather than signal.
        print(f"\n{len(set(problems))} policy violation(s):\n", file=sys.stderr)
        for problem in sorted(set(problems)):
            print(f"  * {problem}", file=sys.stderr)
        return 1

    expected = RESTART_ONLY | START_ON_INSTALL
    if seen != expected:
        missing = ", ".join(sorted(expected - seen))
        print(f"\nno .deb supplied for: {missing}", file=sys.stderr)
        return 1

    print(f"\nsystemd start/restart policy OK for {len(seen)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
