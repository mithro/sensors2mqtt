#!/usr/bin/env python3
"""Derive the Debian package version from git, matching the Python version.

The Python package version comes from git tags via hatch-vcs (``pyproject.toml``
``[tool.hatch.version] source = "vcs"`` with ``version_scheme = "post-release"``,
which uses ``git describe`` under the hood). This script makes the Debian package
version use the *same* git-derived version, so the two can never drift apart.

Background: issue #2 — a hand-maintained ``debian/changelog`` was stuck at
``0.1.0`` while a deployed build was ``0.2.0``; because ``0.1.0 < 0.2.0``,
apt/unattended-upgrades refused to move hosts to the new build. Deriving the
version from git removes the hand-maintained number entirely.

With ``post-release`` versioning the value is a real release that increments each
commit: at a tag it is e.g. ``0.3.0``; N commits later ``0.3.0.postN``. Those are
already valid, correctly-sorting Debian versions, so no PEP 440 -> Debian
conversion is needed.

NOTE: the version only rises above the legacy ``0.2.0`` deb build *and* the
``0.1.devN`` builds already on PyPI once the repo is tagged above ``v0.2``
(e.g. ``git tag v0.3`` -> ``0.3`` / ``0.3.postN``, which beats both). The scheme
alone does not — the tag sets MAJOR.MINOR.

Usage::

    python3 packaging/deb-version.py                   # print the version
    python3 packaging/deb-version.py --write-changelog # regenerate debian/changelog
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "debian" / "changelog"
PYPROJECT = REPO / "pyproject.toml"
DEFAULT_MAINTAINER = "Tim Ansell <tim@mithis.com>"


def _scm_options() -> dict:
    """hatch-vcs raw-options from pyproject, so the scheme is single-sourced
    (the .deb version then matches the Python/PyPI version exactly)."""
    try:
        data = tomllib.loads(PYPROJECT.read_text())
        return dict(data["tool"]["hatch"]["version"]["raw-options"])
    except Exception:  # noqa: BLE001
        return {"version_scheme": "post-release", "local_scheme": "no-local-version"}


def _changelog_version() -> str:
    """Fallback when git/setuptools_scm is unavailable (e.g. a tarball build)."""
    out = subprocess.run(
        ["dpkg-parsechangelog", "-l", str(CHANGELOG), "-SVersion"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out.split("-", 1)[0]


def version() -> str:
    """The git-derived version (same as hatch-vcs); valid as a Debian version."""
    try:
        from setuptools_scm import get_version

        return get_version(root=str(REPO), **_scm_options())
    except Exception:  # noqa: BLE001
        return _changelog_version()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _maintainer() -> str:
    try:
        m = re.search(r"^ -- (.+?)  ", CHANGELOG.read_text(), re.M)
        if m:
            return m.group(1)
    except FileNotFoundError:
        pass
    return DEFAULT_MAINTAINER


def write_changelog() -> None:
    """Regenerate debian/changelog with the git-derived version + commit date."""
    try:
        describe = _git("describe", "--tags", "--always", "--long")
        date = _git("log", "-1", "--format=%cd", "--date=rfc2822")
    except Exception:  # noqa: BLE001
        describe, date = "unknown", "Thu, 01 Jan 1970 00:00:00 +0000"
    CHANGELOG.write_text(
        f"sensors2mqtt ({version()}) unstable; urgency=medium\n\n"
        f"  * Automated build from git ({describe}).\n\n"
        f" -- {_maintainer()}  {date}\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive the package version from git")
    ap.add_argument(
        "--write-changelog", action="store_true",
        help="regenerate debian/changelog for the git-derived version",
    )
    args = ap.parse_args()
    if args.write_changelog:
        write_changelog()
    else:
        print(version())


if __name__ == "__main__":
    main()
