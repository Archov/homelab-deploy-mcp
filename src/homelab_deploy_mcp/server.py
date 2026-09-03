"""MCP server entrypoint.

Runs over stdio, meant to be launched by an MCP client (Claude Code, Codex,
etc.) as a local subprocess. It never listens on any port itself — the only
network activity it initiates is an outbound SSH connection to your homelab
host when a tool is invoked. That still requires your homelab to have SSH
reachable from wherever this runs; this design just reuses whatever SSH
access you already have for administering the box rather than adding a new
bespoke inbound listener/port the way a webhook- or HTTP-based tool would.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import ConfigError, load_config
from .ssh_client import SshCommandError, run_remote_command

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"
CONFIG_PATH = Path(os.environ.get("HOMELAB_DEPLOY_MCP_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()

mcp = MCPServer("homelab-deploy")


@mcp.tool()
def redeploy_media_clip_makarr(branch: str, env_file: str) -> str:
    """Rebuild and restart the MediaClipMakarr docker compose stack on the homelab server.

    Runs, on the homelab host: resets a root-owned git checkout to the
    requested branch (removing any untracked/ignored cruft first), activates
    the requested pre-approved env file, then runs
    `docker compose up -d --force-recreate --build` against a compose file
    that also lives on the host, not in the branch.

    Both arguments are validated against the allowlist in config.yaml
    before anything runs, and validated again independently — twice — on
    the homelab host itself: once by the unprivileged forced SSH command,
    and again by the root-owned executor it invokes via sudo. A bug here
    can't bypass either of those.

    Args:
        branch: Git branch to deploy. Must be one of
            config.yaml's mediaclipmakarr.allowed_branches.
        env_file: Name of a pre-existing env file on the homelab host to
            copy in as .env for this deploy. Must be one of config.yaml's
            mediaclipmakarr.allowed_env_files.
    """
    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        raise ToolError(f"Server misconfigured ({CONFIG_PATH}): {exc}") from exc
    target = config.mediaclipmakarr

    if branch not in target.allowed_branches:
        allowed = ", ".join(target.allowed_branches)
        raise ToolError(f"branch '{branch}' is not allowed. Allowed branches: {allowed}")
    if env_file not in target.allowed_env_files:
        allowed = ", ".join(target.allowed_env_files)
        raise ToolError(f"env_file '{env_file}' is not allowed. Allowed env files: {allowed}")

    try:
        result = run_remote_command(config.ssh, [target.remote_script, "deploy", branch, env_file])
    except SshCommandError as exc:
        raise ToolError(f"Could not reach the homelab host: {exc}") from exc

    summary = (
        f"exit_code={result.exit_code}\n\n"
        f"--- stdout ---\n{result.stdout}\n\n"
        f"--- stderr ---\n{result.stderr}"
    )
    if result.exit_code != 0:
        raise ToolError(f"Deploy script exited non-zero.\n{summary}")
    return summary


def main() -> None:
    try:
        load_config(CONFIG_PATH)
    except ConfigError as exc:
        print(f"homelab-deploy-mcp: invalid configuration ({CONFIG_PATH}): {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    mcp.run()


if __name__ == "__main__":
    main()
