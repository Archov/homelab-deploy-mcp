from __future__ import annotations

from pathlib import Path

import pytest

from homelab_deploy_mcp.config import ConfigError, load_config

VALID_YAML = """
ssh:
  host: "10.0.0.5"
  user: "deploy"
  key_path: "{key_path}"
  host_key_fingerprint: "SHA256:abc123"

mediaclipmakarr:
  remote_script: "/opt/deploy/redeploy.sh"
  allowed_branches: ["main"]
  allowed_env_files: ["prod.env"]
"""


@pytest.fixture
def fake_key(tmp_path: Path) -> Path:
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("not a real key, just needs to exist")
    return key_path


def write_config(tmp_path: Path, key_path: Path, body: str) -> Path:
    config_path = tmp_path / "config.yaml"
    # Use forward slashes even on Windows: YAML double-quoted scalars treat
    # backslashes as escape sequences, so a raw Windows path would break parsing.
    config_path.write_text(body.format(key_path=key_path.as_posix()))
    return config_path


def test_load_config_happy_path(tmp_path: Path, fake_key: Path) -> None:
    config_path = write_config(tmp_path, fake_key, VALID_YAML)

    config = load_config(config_path)

    assert config.ssh.host == "10.0.0.5"
    assert config.ssh.port == 22  # default
    assert config.ssh.key_path == fake_key
    assert config.mediaclipmakarr.allowed_branches == ("main",)
    assert config.mediaclipmakarr.allowed_env_files == ("prod.env",)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_missing_ssh_key_file_raises(tmp_path: Path) -> None:
    missing_key = tmp_path / "no-such-key"
    config_path = write_config(tmp_path, missing_key, VALID_YAML)

    with pytest.raises(ConfigError, match="SSH key not found"):
        load_config(config_path)


def test_bad_fingerprint_format_raises(tmp_path: Path, fake_key: Path) -> None:
    body = VALID_YAML.replace('"SHA256:abc123"', '"not-a-fingerprint"')
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError, match="host_key_fingerprint"):
        load_config(config_path)


def test_empty_allowed_branches_raises(tmp_path: Path, fake_key: Path) -> None:
    body = VALID_YAML.replace('allowed_branches: ["main"]', "allowed_branches: []")
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError, match="allowed_branches"):
        load_config(config_path)


def test_missing_section_raises(tmp_path: Path, fake_key: Path) -> None:
    body = "ssh:\n  host: '10.0.0.5'\n"
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError):
        load_config(config_path)
