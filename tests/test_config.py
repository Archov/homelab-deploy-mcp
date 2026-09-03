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
  remote_script: "/opt/deploy/redeploy.sh"

targets:
  mediaclipmakarr:
    allowed_branches: ["main"]
    allowed_env_files: ["prod.env"]
  otherproject:
    allowed_branches: ["main", "dev"]
    allowed_env_files: ["prod.env", "staging.env"]
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
    assert config.ssh.remote_script == "/opt/deploy/redeploy.sh"
    assert set(config.targets) == {"mediaclipmakarr", "otherproject"}
    assert config.targets["mediaclipmakarr"].allowed_branches == ("main",)
    assert config.targets["mediaclipmakarr"].allowed_env_files == ("prod.env",)
    assert config.targets["otherproject"].allowed_branches == ("main", "dev")


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_missing_ssh_key_file_raises(tmp_path: Path) -> None:
    missing_key = tmp_path / "no-such-key"
    config_path = write_config(tmp_path, missing_key, VALID_YAML)

    with pytest.raises(ConfigError, match="SSH key not found"):
        load_config(config_path)


def test_missing_remote_script_raises(tmp_path: Path, fake_key: Path) -> None:
    body = VALID_YAML.replace('remote_script: "/opt/deploy/redeploy.sh"', "")
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError, match="remote_script"):
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


def test_no_targets_raises(tmp_path: Path, fake_key: Path) -> None:
    body = """
ssh:
  host: "10.0.0.5"
  user: "deploy"
  key_path: "{key_path}"
  host_key_fingerprint: "SHA256:abc123"
  remote_script: "/opt/deploy/redeploy.sh"

targets: {{}}
"""
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError, match="at least one target"):
        load_config(config_path)


def test_invalid_target_name_raises(tmp_path: Path, fake_key: Path) -> None:
    body = VALID_YAML.replace("mediaclipmakarr:", "bad target name:")
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError, match="invalid target name"):
        load_config(config_path)


def test_uppercase_target_name_raises(tmp_path: Path, fake_key: Path) -> None:
    # Target names double as Docker Compose project names on the host side,
    # and Compose project names are lowercase-only. Two names differing
    # only in case (e.g. "Foo"/"foo") would otherwise be free to collide on
    # the same Compose project identity despite living in separate
    # directories -- reject the mixed-case one before that's possible.
    body = VALID_YAML.replace("mediaclipmakarr:", "MediaClipMakarr:")
    config_path = write_config(tmp_path, fake_key, body)

    with pytest.raises(ConfigError, match="invalid target name"):
        load_config(config_path)
