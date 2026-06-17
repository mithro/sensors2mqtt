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
            f"credential files must not be group- or world-accessible. "
            f"Fix: chmod 0600 {path}"
        )


def ensure_secure_file(path: Path) -> None:
    """Raise InsecureFilePermissionsError if ``path`` is reachable beyond its owner.

    Enforces 0600/0400 (the ssh private-key rule: any bit set in
    ``mode & 0o077`` is a failure). Credential files must not be group- or
    world-readable. Note that ``path.stat()`` follows symlinks, so the mode of
    the symlink target is what gets checked, not the symlink itself.
    """
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise InsecureFilePermissionsError(path, mode)
