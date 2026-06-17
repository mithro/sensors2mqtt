# Credential-bearing config file permissions — Design

**Date:** 2026-06-17
**Task:** #35

## Problem

Two sensors2mqtt config files hold credentials:

- `/etc/sensors2mqtt/env` — `MQTT_USER`/`MQTT_PASSWORD`, and (IPMI) `BMC_USER`/`BMC_PASS`.
- `/etc/sensors2mqtt/snmp.toml` — SNMP `community` / `write_community` strings (read and write auth tokens).

Neither is reliably restricted today:

- `debian/python3-sensors2mqtt.postinst` seeds `env` with `cp env.example
  /etc/sensors2mqtt/env` and **no `chmod`**, so the seeded file inherits a
  world-readable mode rather than `0600`. (The example has empty creds, so the
  exposure begins once an admin fills it in.)
- `snmp.toml` is **never created by the deb** — only `snmp.toml.example` ships to
  `/usr/share`. Admins hand-create `/etc/sensors2mqtt/snmp.toml`, which lands at
  the default umask mode. On ten64 it is `0644` (world-readable) and already
  contains live community strings — a real exposure to any local user.

All collector systemd units run as **root** (no `User=`), so `0600 root:root` is
the correct secure target; no group is required.

## Goal

Make credential files secure by default and fail loudly when they are not —
within the repository and packaging only. Specifically:

1. The deb creates `/etc/sensors2mqtt/env` as `0600` when it seeds it.
2. The collectors refuse to start when a config file they open is reachable
   beyond its owner.

Live host remediation (chmod on existing hosts) is **out of scope** here (see
"Out of scope") but has a hard rollout dependency (see "Rollout dependency").

## Design

### 1. Packaging — secure `env` at creation only

In `debian/python3-sensors2mqtt.postinst`, set the mode at creation, inside the
existing `if [ ! -f ]` guard so an already-present file is never touched
(matches the chosen "only when the deb creates the file" policy):

```sh
if [ ! -f /etc/sensors2mqtt/env ]; then
    cp /usr/share/sensors2mqtt/env.example /etc/sensors2mqtt/env
    chmod 0600 /etc/sensors2mqtt/env
fi
```

The `/usr/share/sensors2mqtt/env.example` template stays world-readable (it is an
example with empty credentials). The `/etc/sensors2mqtt` directory mode is left
at its default `0755`; with `0600` files this is sufficient. `snmp.toml` is not
created by the deb and is therefore not touched by packaging — it is covered by
the runtime guard below.

### 2. Runtime guard — refuse to start on insecure config

A new focused module `src/sensors2mqtt/security.py`:

```python
from __future__ import annotations

from pathlib import Path


class InsecureFilePermissionsError(Exception):
    """A credential-bearing file is accessible beyond its owner."""

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode
        super().__init__(
            f"{path} is group/other-accessible (mode {mode:#o}); "
            f"credential files must be 0600. Fix: chmod 0600 {path}"
        )


def ensure_secure_file(path: Path) -> None:
    """Raise InsecureFilePermissionsError if `path` is reachable beyond its owner.

    Enforces 0600/0400 (the ssh private-key rule: any bit in mode & 0o077 is a
    failure). Credential-bearing config files must not be group/world readable.
    """
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise InsecureFilePermissionsError(path, mode)
```

Wire it into the shared SNMP config loader. `load_config` is defined in
`src/sensors2mqtt/collector/snmp.py:257` and imported by `snmp_control.py`
(`from sensors2mqtt.collector.snmp import … load_config …`), so a single call
covers both the `snmp` and `snmp-control` collectors. Insert the check after
path resolution and before the file is opened (`snmp.py:281-282`), so it applies
to both an explicit `--config` path and the `DEFAULT_CONFIG_PATHS` fallback:

```python
    log.info("Loading config from %s", path)
    ensure_secure_file(path)            # <-- new
    with open(path, "rb") as f:
        data = tomllib.load(f)
```

Effect: an `snmp` or `snmp-control` collector started against a group/world
readable `snmp.toml` raises `InsecureFilePermissionsError` and exits non-zero,
with a message naming the file, its mode, and the `chmod 0600` fix.

### 3. Why the guard covers `snmp.toml` but not `env`

The collectors *open* `snmp.toml` (`--config`), so the runtime guard has a
reliable path and mode to check. They never open `env`: systemd reads it via
`EnvironmentFile=` and injects only the resulting environment variables, so the
Python process has no handle on the env file's path or mode. `env` is therefore
secured at the packaging layer (`0600` at seed) and by gdoc2netcfg (which already
deploys it `0600`), not by the runtime guard. This split (decided during
brainstorming) keeps the guard free of hardcoded `/etc` paths and dev-run
false-positives.

## Rollout dependency (safety — not optional)

"Refuse to start" is a **breaking change** for any host whose `snmp.toml` is
still group/world readable. Every merge to `main` auto-publishes to PyPI and the
apt repo, and ten64 runs `unattended-upgrades`; an upgrade + restart on such a
host would make `sensors2mqtt-snmp` / `sensors2mqtt-snmp-control` refuse to start
and take switch monitoring down.

ten64 was the only host running the snmp collectors and its `snmp.toml` was
`0644`; it was **remediated to `0600` on 2026-06-17** (ahead of this guard
shipping), so the imminent hazard is cleared. Any *future* snmp host must have
its `/etc/sensors2mqtt/snmp.toml` set to `0600` before this version reaches it.
Remediation is ops (tracked under #5), not part of this code change; the natural
gate remains that #35 will not merge without explicit approval.

## Testing / Verification

New `tests/test_security.py` (TDD):

- `ensure_secure_file` returns for `0o600` and `0o400`.
- `ensure_secure_file` raises `InsecureFilePermissionsError` for `0o644`,
  `0o640`, `0o604`, `0o660`, `0o666`.
- The error message contains the path, the octal mode, and `chmod 0600`.

Extend `tests/test_snmp.py`:

- `load_config` raises `InsecureFilePermissionsError` for a `0o644` `snmp.toml`
  fixture (use `tmp_path` + `chmod`).
- `load_config` still loads switches for a `0o600` fixture.

All use real temp files and `os.chmod`; no mocks. `make test` + `make lint`.

Packaging (spot-check, per the issue-#13 precedent — no CI assertion):

- Build with `DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b`.
- Fresh-install `python3-sensors2mqtt` in a clean chroot/container; assert
  `stat -c %a /etc/sensors2mqtt/env` is `600`.
- Re-run configure with an existing `env` present at a different mode; confirm
  it is left untouched.

## Docs

- `README.md`: in the config section, state that `env` and `snmp.toml` must be
  `0600` and that the snmp collectors refuse to start otherwise.
- `docs/collectors.md` and `docs/getting-started.md`: the `apt install` → create
  `snmp.toml` flow gains a `chmod 0600 /etc/sensors2mqtt/snmp.toml` step and a
  note on the refuse-to-start behavior + fix.
- Operator advisory: on hosts running etckeeper, `/etc/sensors2mqtt/*` is
  committed into `/etc/.git`, so credentials enter that history; recommend
  excluding the directory if that is undesirable.

## Out of scope (tracked elsewhere)

- Live fleet remediation: chmod ten64's `snmp.toml`, verify `env` on
  big_storage (BMC creds) and the RPi/SDR hosts — ops under #5 / #34.
- The etckeeper-exclusion decision (host policy; advisory only here).
- The adjacent "unknown model is silently skipped" bug in `load_config`
  (`snmp.py:288-291`) — task #36.

## Open items / risks

- The packaging `chmod` is shell and not unit-tested; covered by the build +
  install spot-check above.
- The runtime guard changes start-up behavior for misconfigured hosts; the
  rollout dependency above is the mitigation.
