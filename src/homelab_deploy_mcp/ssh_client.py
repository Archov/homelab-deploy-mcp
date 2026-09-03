"""A minimal, defensive SSH command runner.

Design choices worth calling out:

- We never consult a known_hosts file and never trust-on-first-use. We
  accept whatever host key is presented at the transport layer, then
  immediately compare its SHA256 fingerprint against the value pinned in
  config.yaml *before* sending any command. If it doesn't match, we close
  the connection without running anything. This is equivalent in spirit to
  `ssh -o StrictHostKeyChecking=yes` with a fixed known_hosts entry, but
  self-contained so config.yaml is the single source of truth.
- Every argument is passed through shlex.quote before being joined into the
  command string sent over the SSH exec channel. Callers are expected to
  have already validated arguments against an allowlist (see server.py),
  but quoting here is cheap defense-in-depth against the arguments ever
  containing shell metacharacters.
- No persistent connection: one SSH session per tool call, closed
  immediately after. Nothing is cached or kept open between calls.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
import socket
from dataclasses import dataclass

import paramiko

from .config import SshConfig


class SshCommandError(RuntimeError):
    """Raised when the connection, host key check, or command dispatch fails."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


def _sha256_fingerprint(key: paramiko.PKey) -> str:
    """Match the 'SHA256:...' format `ssh-keygen -lf ... -E sha256` prints."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class _AcceptAnyHostKey(paramiko.MissingHostKeyPolicy):
    """Accept the key at the transport layer so connect() can complete.

    This is NOT the actual trust decision — run_remote_command() verifies
    the fingerprint immediately after connecting, before doing anything
    else, and aborts the session if it doesn't match config.yaml.
    """

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        return None


def run_remote_command(config: SshConfig, argv: list[str]) -> CommandResult:
    command = " ".join(shlex.quote(part) for part in argv)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_AcceptAnyHostKey())
    try:
        try:
            client.connect(
                hostname=config.host,
                port=config.port,
                username=config.user,
                key_filename=str(config.key_path),
                timeout=config.connect_timeout_seconds,
                allow_agent=False,
                look_for_keys=False,
            )
        except (paramiko.SSHException, OSError) as exc:
            raise SshCommandError(f"Failed to connect to {config.host}:{config.port}: {exc}") from exc

        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise SshCommandError("SSH transport did not become active")

        remote_key = transport.get_remote_server_key()
        actual_fingerprint = _sha256_fingerprint(remote_key)
        if actual_fingerprint != config.host_key_fingerprint:
            raise SshCommandError(
                "Host key fingerprint mismatch — refusing to run any command. "
                f"Expected {config.host_key_fingerprint}, got {actual_fingerprint}. "
                "Either the host key legitimately changed (update config.yaml "
                "after verifying out-of-band) or something is intercepting "
                "this connection."
            )

        try:
            _stdin, stdout, stderr = client.exec_command(
                command, timeout=config.command_timeout_seconds
            )
            _stdin.close()
            exit_code = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")
            return CommandResult(exit_code=exit_code, stdout=stdout_text, stderr=stderr_text)
        except socket.timeout as exc:
            raise SshCommandError(
                f"Command timed out after {config.command_timeout_seconds}s: {command}"
            ) from exc
    finally:
        client.close()
