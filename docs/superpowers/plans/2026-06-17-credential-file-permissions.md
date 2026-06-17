# Credential-File Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sensors2mqtt credential files secure by default — the deb seeds `/etc/sensors2mqtt/env` as `0600`, and the SNMP collectors refuse to start when `snmp.toml` is group/world-readable.

**Architecture:** A new focused module `sensors2mqtt/security.py` provides `ensure_secure_file()` (the ssh-style `mode & 0o077` check). It is wired into the shared `load_config()` in `collector/snmp.py`, which both `snmp` and `snmp-control` use. Packaging sets the mode on the env file at creation time. `env` is secured by packaging only (the process never opens it — systemd injects the vars); `snmp.toml` is secured by the runtime guard (the collector opens it).

**Tech Stack:** Python 3 (stdlib `os`/`pathlib`), pytest, `uv` for all Python commands, Debian packaging (dh-python), ruff.

**Spec:** `docs/superpowers/specs/2026-06-17-credential-file-permissions-design.md`

**Pre-existing context (not a task):** ten64's `/etc/sensors2mqtt/snmp.toml` was already remediated to `0600` on 2026-06-17, so the refuse-to-start guard will not strand the only host currently running the snmp collectors.

---

## File Structure

- **Create** `src/sensors2mqtt/security.py` — `InsecureFilePermissionsError` + `ensure_secure_file(path)`. Single responsibility: credential-file permission checking.
- **Create** `tests/test_security.py` — unit tests for the helper.
- **Modify** `src/sensors2mqtt/collector/snmp.py` — import and call `ensure_secure_file` inside `load_config` (covers `snmp` and `snmp-control`).
- **Modify** `tests/test_snmp.py` — add guard tests + keep the existing fixture-based `TestConfigLoading` tests green under the new guard.
- **Modify** `debian/python3-sensors2mqtt.postinst` — `chmod 0600` the seeded env file at creation.
- **Modify** `README.md`, `docs/collectors.md`, `docs/getting-started.md` — document the `0600` requirement, refuse-to-start behavior, and an etckeeper advisory.

---

## Task 1: `ensure_secure_file` security helper

**Files:**
- Create: `src/sensors2mqtt/security.py`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_security.py`:

```python
"""Tests for credential-file permission checks."""

import os
from pathlib import Path

import pytest

from sensors2mqtt.security import InsecureFilePermissionsError, ensure_secure_file


def _file_with_mode(tmp_path: Path, mode: int) -> Path:
    p = tmp_path / "creds"
    p.write_text("secret")
    os.chmod(p, mode)
    return p


class TestEnsureSecureFile:
    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_owner_only_modes_pass(self, tmp_path, mode):
        path = _file_with_mode(tmp_path, mode)
        ensure_secure_file(path)  # must not raise

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
    def test_group_or_other_accessible_modes_raise(self, tmp_path, mode):
        path = _file_with_mode(tmp_path, mode)
        with pytest.raises(InsecureFilePermissionsError):
            ensure_secure_file(path)

    def test_error_message_has_path_mode_and_fix(self, tmp_path):
        path = _file_with_mode(tmp_path, 0o644)
        with pytest.raises(InsecureFilePermissionsError) as exc:
            ensure_secure_file(path)
        msg = str(exc.value)
        assert str(path) in msg
        assert "0o644" in msg
        assert "chmod 0600" in msg
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_security.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sensors2mqtt.security'`

- [ ] **Step 3: Create the module**

Create `src/sensors2mqtt/security.py`:

```python
"""Permission checks for credential-bearing files."""

from __future__ import annotations

from pathlib import Path


class InsecureFilePermissionsError(Exception):
    """Raised when a credential-bearing file is accessible beyond its owner."""

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode
        super().__init__(
            f"{path} is group/other-accessible (mode {mode:#o}); "
            f"credential files must be 0600. Fix: chmod 0600 {path}"
        )


def ensure_secure_file(path: Path) -> None:
    """Raise InsecureFilePermissionsError if ``path`` is reachable beyond its owner.

    Enforces 0600/0400 (the ssh private-key rule: any bit set in
    ``mode & 0o077`` is a failure). Credential files must not be group- or
    world-readable.
    """
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise InsecureFilePermissionsError(path, mode)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_security.py -v`
Expected: PASS — 8 passed (2 + 5 parametrized + 1 message test).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/sensors2mqtt/security.py tests/test_security.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/sensors2mqtt/security.py tests/test_security.py
git commit -m "feat(security): add ensure_secure_file credential-permission guard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Wire the guard into `load_config`

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py` (top-level imports; `load_config` at lines 257-283)
- Test: `tests/test_snmp.py` (`TestConfigLoading` class, ~line 171)

Background: `load_config(path)` is the single loader used by both collectors
(`snmp_control.py` does `from sensors2mqtt.collector.snmp import … load_config`).
It resolves `path` (explicit `--config` or the `DEFAULT_CONFIG_PATHS` fallback),
then opens the file at line 282. The guard goes between path resolution and the
`open`. The committed fixture `tests/fixtures/snmp_test.toml` is checked out
group/world-readable, so the existing `TestConfigLoading` tests need the fixture
tightened to `0600` first (git records only the exec bit, so `chmod 0600` leaves
no diff).

- [ ] **Step 1: Write the failing guard tests**

In `tests/test_snmp.py`, first add `import os` to the imports at the top of the
file (it currently imports only `Path`, `MagicMock`/`patch`, and `pytest`).

Then add these two tests to the `TestConfigLoading` class (after
`test_no_config_raises`):

```python
    def test_load_config_insecure_perms_raises(self, tmp_path):
        """A group/world-readable config must refuse to load."""
        from sensors2mqtt.security import InsecureFilePermissionsError

        cfg = tmp_path / "snmp.toml"
        cfg.write_text(
            '[switches.test-m4300]\n'
            'model = "m4300"\n'
            'host = "test-m4300.example.com"\n'
            'community = "public"\n'
        )
        os.chmod(cfg, 0o644)
        with pytest.raises(InsecureFilePermissionsError):
            load_config(cfg)

    def test_load_config_secure_perms_loads(self, tmp_path):
        """A 0600 config loads normally."""
        cfg = tmp_path / "snmp.toml"
        cfg.write_text(
            '[switches.test-m4300]\n'
            'model = "m4300"\n'
            'host = "test-m4300.example.com"\n'
            'community = "public"\n'
        )
        os.chmod(cfg, 0o600)
        switches = load_config(cfg)
        assert len(switches) == 1
        assert switches[0].name == "test-m4300"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_snmp.py::TestConfigLoading::test_load_config_insecure_perms_raises -v`
Expected: FAIL — `InsecureFilePermissionsError` not raised (the guard does not exist yet); the import inside the test resolves because `security.py` exists from Task 1.

- [ ] **Step 3: Add the autouse fixture that keeps the existing tests green**

Add this fixture at the top of the `TestConfigLoading` class body (before
`test_load_config`):

```python
    @pytest.fixture(autouse=True)
    def _secure_shared_fixture(self):
        # load_config now refuses group/world-readable files. The committed
        # fixture checks out 0664; tighten it to 0600 for these tests. git
        # tracks only the executable bit, so this leaves no diff.
        os.chmod(CONFIG_FILE, 0o600)
```

- [ ] **Step 4: Add the import and the guard call in `snmp.py`**

Add to the module-level imports near the top of
`src/sensors2mqtt/collector/snmp.py` (alongside the other stdlib /
`from sensors2mqtt …` imports):

```python
from sensors2mqtt.security import ensure_secure_file
```

Then change the body of `load_config` (currently lines 281-283) from:

```python
    log.info("Loading config from %s", path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
```

to:

```python
    log.info("Loading config from %s", path)
    ensure_secure_file(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
```

- [ ] **Step 5: Run the affected tests to verify they pass**

Run: `uv run pytest tests/test_snmp.py::TestConfigLoading -v`
Expected: PASS — the two new tests pass, and all pre-existing `TestConfigLoading`
tests (`test_load_config`, `test_config_node_ids`, `test_config_hosts_are_dns`,
`test_config_sensors_populated`, `test_load_missing_config_raises`,
`test_write_community_loaded`, `test_no_config_raises`) still pass.

Note: `test_load_missing_config_raises` and `test_no_config_raises` still expect
`FileNotFoundError`; with the guard, `ensure_secure_file` calls `path.stat()`
which raises `FileNotFoundError` for a missing path before `open` would — same
exception type, so those tests remain green.

- [ ] **Step 6: Run the full suite + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all tests pass; no lint errors.

- [ ] **Step 7: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): refuse to start when snmp.toml is group/world-readable

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Package — seed `/etc/sensors2mqtt/env` as 0600

**Files:**
- Modify: `debian/python3-sensors2mqtt.postinst`

The postinst seeds env via `cp` with no `chmod`, so the seeded file is
world-readable. Set the mode at creation only (inside the existing `if`), so an
already-present file (e.g. one placed by gdoc2netcfg) is never re-moded.

- [ ] **Step 1: Edit the postinst**

Change `debian/python3-sensors2mqtt.postinst` from:

```bash
if [ "$1" = "configure" ]; then
    mkdir -p /etc/sensors2mqtt
    if [ ! -f /etc/sensors2mqtt/env ]; then
        cp /usr/share/sensors2mqtt/env.example /etc/sensors2mqtt/env
    fi
fi
```

to:

```bash
if [ "$1" = "configure" ]; then
    mkdir -p /etc/sensors2mqtt
    if [ ! -f /etc/sensors2mqtt/env ]; then
        cp /usr/share/sensors2mqtt/env.example /etc/sensors2mqtt/env
        chmod 0600 /etc/sensors2mqtt/env
    fi
fi
```

- [ ] **Step 2: Verify the cp+chmod sequence yields 0600 (simulation)**

Run:
```bash
mkdir -p ./tmp/etctest
cp deploy/env.example ./tmp/etctest/env
chmod 0600 ./tmp/etctest/env
stat -c '%a %n' ./tmp/etctest/env
rm -rf ./tmp/etctest
```
Expected: `600 ./tmp/etctest/env`

- [ ] **Step 3: Confirm the shipped postinst carries the chmod (build spot-check)**

Run:
```bash
DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b
deb=$(ls ../python3-sensors2mqtt_*_all.deb | tail -1)
dpkg-deb -e "$deb" ./tmp/debcontrol
grep -n "chmod 0600 /etc/sensors2mqtt/env" ./tmp/debcontrol/postinst
rm -rf ./tmp/debcontrol
```
Expected: the `grep` prints the `chmod 0600 /etc/sensors2mqtt/env` line from the
packaged postinst (confirming it ships).

If `dpkg-buildpackage` fails because build dependencies are missing on this host,
skip the build; the CI "Debian Packages" workflow builds the real package on
merge. Steps 1-2 plus the source edit are sufficient pre-merge evidence. Note in
the commit/PR that the build spot-check was deferred to CI if so.

- [ ] **Step 4: Commit**

```bash
git add debian/python3-sensors2mqtt.postinst
git commit -m "build(deb): create /etc/sensors2mqtt/env as 0600 when seeding

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Documentation

**Files:**
- Modify: `README.md` (snmp install flow, ~lines 39-41)
- Modify: `docs/collectors.md` (snmp install flow, ~lines 47-51; SNMP section ~59-60)
- Modify: `docs/getting-started.md` (snmp install flow, ~lines 29-31)

- [ ] **Step 1: README — add chmod to the snmp flow**

In `README.md`, the snmp install block currently reads:

```bash
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo editor /etc/sensors2mqtt/snmp.toml
sudo systemctl start sensors2mqtt-snmp    # or sensors2mqtt-snmp-control
```

Change it to:

```bash
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo chmod 0600 /etc/sensors2mqtt/snmp.toml   # holds SNMP community strings
sudo editor /etc/sensors2mqtt/snmp.toml
sudo systemctl start sensors2mqtt-snmp    # or sensors2mqtt-snmp-control
```

Then add this note immediately after that code block:

```markdown
> The snmp collectors refuse to start if `/etc/sensors2mqtt/snmp.toml` is
> group- or world-readable (it contains SNMP community strings). The seeded
> `/etc/sensors2mqtt/env` is created `0600` automatically.
```

- [ ] **Step 2: docs/collectors.md — add chmod + note**

In `docs/collectors.md`, the snmp block (lines 47-51) currently has:

```bash
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo editor /etc/sensors2mqtt/snmp.toml                       # add switch definitions
```

Insert a chmod line between them:

```bash
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo chmod 0600 /etc/sensors2mqtt/snmp.toml                   # holds community strings
sudo editor /etc/sensors2mqtt/snmp.toml                       # add switch definitions
```

Then under the SNMP section near line 59-60 ("Requires a configuration file …"),
add:

```markdown
The config file holds SNMP community strings, so it must be `0600`; the snmp and
snmp-control collectors refuse to start otherwise (the startup error names the
file and the `chmod 0600` fix).
```

- [ ] **Step 3: docs/getting-started.md — add chmod**

In `docs/getting-started.md`, the snmp block (lines 29-31) currently has:

```bash
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo editor /etc/sensors2mqtt/snmp.toml
sudo systemctl start sensors2mqtt-snmp
```

Change it to:

```bash
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo chmod 0600 /etc/sensors2mqtt/snmp.toml
sudo editor /etc/sensors2mqtt/snmp.toml
sudo systemctl start sensors2mqtt-snmp
```

- [ ] **Step 4: etckeeper advisory**

At the end of `docs/getting-started.md`, add a short section:

```markdown
## A note on etckeeper

If a host runs etckeeper, files under `/etc/sensors2mqtt/` (including `env` and
`snmp.toml`) are committed into `/etc/.git`, so their credentials enter that
git history. If that is undesirable on a given host, exclude the directory from
etckeeper (e.g. add `/etc/sensors2mqtt` to `/etc/.gitignore`).
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/collectors.md docs/getting-started.md
git commit -m "docs: document 0600 requirement and refuse-to-start for config files

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Run `uv run pytest -q` — all tests pass.
- [ ] Run `uv run ruff check src tests` — no errors.
- [ ] Confirm `git status` is clean (no stray `./tmp` artifacts).
- [ ] Confirm the branch contains the four commits and is not on `main`.
