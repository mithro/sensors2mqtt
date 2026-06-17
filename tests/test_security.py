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
    @pytest.mark.parametrize("mode", [0o600, 0o400, 0o700, 0o500])
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
