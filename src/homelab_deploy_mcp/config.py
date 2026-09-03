"""Loads and validates config.yaml.

Kept deliberately strict: every field is required (no silent defaults for
anything security-relevant), and the allowlists are validated to be
non-empty so a typo in config.yaml fails loudly at startup rather than
quietly allowing everything or nothing.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when config.yaml is missing or has invalid values."""


@dataclasses.dataclass(frozen=True)
class SshConfig:
    host: str
    port: int
    user: str
    key_path: Path
    host_key_fingerprint: str
    connect_timeout_seconds: float
    command_timeout_seconds: float


@dataclasses.dataclass(frozen=True)
class DeployTargetConfig:
    remote_script: str
    allowed_branches: tuple[str, ...]
    allowed_env_files: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class AppConfig:
    ssh: SshConfig
    mediaclipmakarr: DeployTargetConfig


def _require(mapping: dict[str, Any], key: str, section: str) -> Any:
    value = mapping.get(key)
    if value in (None, ""):
        raise ConfigError(f"Missing required config key: {section}.{key}")
    return value


def _require_list(mapping: dict[str, Any], key: str, section: str) -> tuple[str, ...]:
    value = _require(mapping, key, section)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{section}.{key} must be a list of strings")
    if not value:
        raise ConfigError(f"{section}.{key} must not be empty")
    return tuple(value)


def load_config(config_path: Path) -> AppConfig:
    if not config_path.is_file():
        raise ConfigError(
            f"Config file not found at {config_path}. Copy config.example.yaml to "
            "config.yaml next to it and fill in your own homelab details."
        )

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    ssh_raw = _require(raw, "ssh", "<root>")
    if not isinstance(ssh_raw, dict):
        raise ConfigError("ssh must be a mapping")

    target_raw = _require(raw, "mediaclipmakarr", "<root>")
    if not isinstance(target_raw, dict):
        raise ConfigError("mediaclipmakarr must be a mapping")

    key_path = Path(_require(ssh_raw, "key_path", "ssh")).expanduser()
    if not key_path.is_file():
        raise ConfigError(f"SSH key not found at {key_path} (ssh.key_path)")

    fingerprint = _require(ssh_raw, "host_key_fingerprint", "ssh")
    if not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:"):
        raise ConfigError(
            "ssh.host_key_fingerprint must be the SHA256 fingerprint string "
            "OpenSSH prints, e.g. 'SHA256:AbCdEf...'. Get it from the homelab "
            "host with: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256"
        )

    ssh = SshConfig(
        host=_require(ssh_raw, "host", "ssh"),
        port=int(ssh_raw.get("port", 22)),
        user=_require(ssh_raw, "user", "ssh"),
        key_path=key_path,
        host_key_fingerprint=fingerprint,
        connect_timeout_seconds=float(ssh_raw.get("connect_timeout_seconds", 15)),
        command_timeout_seconds=float(ssh_raw.get("command_timeout_seconds", 600)),
    )

    target = DeployTargetConfig(
        remote_script=_require(target_raw, "remote_script", "mediaclipmakarr"),
        allowed_branches=_require_list(target_raw, "allowed_branches", "mediaclipmakarr"),
        allowed_env_files=_require_list(target_raw, "allowed_env_files", "mediaclipmakarr"),
    )

    return AppConfig(ssh=ssh, mediaclipmakarr=target)
