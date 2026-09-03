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

import fnmatch
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


def _branch_allowed(branch: str, patterns: tuple[str, ...]) -> bool:
    """True if `branch` matches any of `patterns`.

    Patterns are shell-glob style ("codex/*" matches any branch under that
    prefix) and matched with fnmatchcase, not fnmatch — the latter
    case-folds via os.path.normcase on Windows, which would make a pattern
    like "codex/*" also match "Codex/Foo" on a Windows-hosted MCP server
    but not on the Linux homelab host actually enforcing the same table,
    a divergence from git's own case-sensitive branch names that fnmatch
    alone would introduce silently. A plain name with no wildcard
    characters (e.g. "main") only matches that exact branch, unchanged
    from before this existed.
    """
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in patterns)


@mcp.tool()
def redeploy(target: str, branch: str, env_file: str) -> str:
    """Rebuild and restart a configured docker compose stack on the homelab server.

    `target` selects which project to deploy, from the `targets:` map in
    config.yaml. This tool never accepts a filesystem path or a docker
    argument directly — every path (the checkout, the compose file, the
    env files) is resolved host-side, inside the privileged executor, from
    a per-target table it owns. Runs, on the homelab host: resets that
    target's root-owned git checkout to the requested branch (removing any
    untracked/ignored cruft first), activates the requested pre-approved
    env file, then runs `docker compose up -d --force-recreate --build`
    against that target's own compose file — none of which ever comes from
    the branch itself.

    All three arguments are validated against the allowlist in config.yaml
    before anything runs, and validated again independently — twice — on
    the homelab host itself: once (syntax only) by the unprivileged forced
    SSH command, and again (against the real, host-side allowlist) by the
    root-owned executor it invokes via sudo. A bug here can't bypass either
    of those.

    Args:
        target: Which configured project to redeploy. Must be a key under
            config.yaml's `targets:` map.
        branch: Git branch to deploy. Must match one of that target's
            `allowed_branches` — either exactly, or against a glob
            pattern there (e.g. "codex/*").
        env_file: Name of a pre-existing env file on the homelab host.
            Must be one of that target's `allowed_env_files`.
    """
    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        raise ToolError(f"Server misconfigured ({CONFIG_PATH}): {exc}") from exc

    target_config = config.targets.get(target)
    if target_config is None:
        available = ", ".join(sorted(config.targets)) or "(none configured)"
        raise ToolError(f"target '{target}' is not configured. Available targets: {available}")

    if not _branch_allowed(branch, target_config.allowed_branches):
        allowed = ", ".join(target_config.allowed_branches)
        raise ToolError(
            f"branch '{branch}' is not allowed for target '{target}'. Allowed branches: {allowed}"
        )
    if env_file not in target_config.allowed_env_files:
        allowed = ", ".join(target_config.allowed_env_files)
        raise ToolError(
            f"env_file '{env_file}' is not allowed for target '{target}'. Allowed env files: {allowed}"
        )

    try:
        result = run_remote_command(
            config.ssh, [config.ssh.remote_script, "deploy", target, branch, env_file]
        )
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
