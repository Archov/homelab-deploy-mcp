from __future__ import annotations

import base64
import hashlib

import paramiko

from homelab_deploy_mcp.ssh_client import _sha256_fingerprint


def test_sha256_fingerprint_matches_openssh_format() -> None:
    key = paramiko.RSAKey.generate(bits=2048)

    fingerprint = _sha256_fingerprint(key)

    expected_digest = hashlib.sha256(key.asbytes()).digest()
    expected = "SHA256:" + base64.b64encode(expected_digest).decode("ascii").rstrip("=")
    assert fingerprint == expected
    assert fingerprint.startswith("SHA256:")
    assert "=" not in fingerprint  # OpenSSH prints it without padding


def test_sha256_fingerprint_differs_for_different_keys() -> None:
    key_a = paramiko.RSAKey.generate(bits=2048)
    key_b = paramiko.RSAKey.generate(bits=2048)

    assert _sha256_fingerprint(key_a) != _sha256_fingerprint(key_b)
