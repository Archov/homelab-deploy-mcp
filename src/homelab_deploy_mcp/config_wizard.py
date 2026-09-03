"""Interactive generator for config.yaml.

Run as `homelab-deploy-init` after installing this package. Writes a
config.yaml (or a differently-named file, e.g. config-claude.yaml for a
multi-agent setup — see the README) and then validates it through the
REAL load_config() before declaring success, so it can never hand you
something the loader itself would reject. This wizard makes no changes
to the homelab host — everything here is local file generation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from ._wizard_common import die, prompt, prompt_int, prompt_list, prompt_yes_no
from .config import BRANCH_PATTERN_RE, ConfigError, ENVFILE_NAME_RE, TARGET_NAME_RE, load_config

ENVFILE_NAME_HINT = "must end in .env with no path separators, e.g. prod.env"
BRANCH_PATTERN_HINT = "letters/digits/_/./-  and * ? [ ] for glob patterns, no spaces or shell characters"

# Matches the bare token ssh_client.py's _sha256_fingerprint() produces
# (base64, no padding) so it can be pulled out of whatever the user pastes
# -- including the full `ssh-keygen -lf ... -E sha256` output line, e.g.
# "256 SHA256:AbCd...xyz user@host (ED25519)", not just the bare token.
FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]+")


def _generate_key(key_path: Path) -> None:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        die(
            "ssh-keygen not found on PATH — install OpenSSH client tools, "
            "or generate the key yourself and re-run this wizard."
        )
        return  # unreachable, die() raises; keeps type-checkers happy
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    # List-form argv, not shell=True and not a shell string -- no shell
    # interpretation happens here regardless of what key_path contains, so
    # there's nothing to escape or quote. The rule appears to flag any
    # subprocess call with a non-literal argument without distinguishing
    # safe argv-list calls from actual shell-string calls.
    subprocess.run(
        [
            ssh_keygen,
            "-t",
            "ed25519",
            "-f",
            str(key_path),
            "-N",
            "",
            "-C",
            "homelab-deploy-mcp",
        ],
        check=True,
    )
    print(f"Generated {key_path} and {key_path}.pub.")
    print(
        f"Copy the PUBLIC key ({key_path}.pub) into the target account's "
        "~/.ssh/authorized_keys on the homelab host — see the README's "
        "'Lock each agent's SSH key to the forced command' step. This "
        "wizard has no access to that host and can't do that part for you."
    )


def _collect_ssh_section() -> dict:
    print("\n--- SSH connection ---")
    host = prompt("Homelab server hostname or IP")
    port = prompt_int("SSH port", default=22, min_value=1, max_value=65535)
    user = prompt("Unix account for this agent (e.g. homelab-deploy-claude)")

    key_path_str = prompt(
        "Path to this agent's private SSH key", default="~/.ssh/homelab_deploy_ed25519"
    )
    key_path = Path(key_path_str).expanduser()
    if not key_path.is_file():
        print(f"\n{key_path} doesn't exist yet.")
        if prompt_yes_no("Generate a new ed25519 keypair there now?", default=True):
            _generate_key(key_path)
        else:
            print(
                "Continuing — but validation below will fail until that "
                "file exists, and the tool won't work until the matching "
                "public key is installed on the homelab host."
            )

    print(
        "\nHost key fingerprint — get this by running on the homelab host "
        "itself (not from here, and not over a connection you don't "
        "already trust):\n"
        "  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256"
    )
    while True:
        raw_fingerprint = prompt("Paste that fingerprint (SHA256:...)")
        match = FINGERPRINT_RE.search(raw_fingerprint)
        if match:
            fingerprint = match.group(0)
            break
        print("  couldn't find a SHA256:<hash> token — paste the whole line ssh-keygen printed")

    connect_timeout = prompt_int("Connect timeout seconds", default=15, min_value=1)
    command_timeout = prompt_int(
        "Command timeout seconds (docker builds can be slow)", default=600, min_value=1
    )
    remote_script = prompt(
        "Path to redeploy.sh on the homelab host", default="/opt/deploy/redeploy.sh"
    )

    return {
        "host": host,
        "port": port,
        "user": user,
        "key_path": key_path_str,
        "host_key_fingerprint": fingerprint,
        "connect_timeout_seconds": connect_timeout,
        "command_timeout_seconds": command_timeout,
        "remote_script": remote_script,
    }


def _collect_targets() -> dict:
    print("\n--- Targets ---")
    print("At least one target is required. Each needs a name matching the")
    print("same lowercase rule the homelab host enforces, plus the branches")
    print("and env files this agent is allowed to request for it.")
    targets: dict[str, dict] = {}
    while True:
        while True:
            name = prompt("Target name (lowercase, e.g. mediaclipmakarr)")
            if TARGET_NAME_RE.match(name):
                break
            print(f"  must match {TARGET_NAME_RE.pattern} (lowercase letters/digits/_/-)")
        branches = prompt_list(
            "Allowed branches (comma-separated; globs like codex/* are fine)",
            validator=BRANCH_PATTERN_RE,
            item_hint=BRANCH_PATTERN_HINT,
        )
        env_files = prompt_list(
            "Allowed env file names (comma-separated)",
            validator=ENVFILE_NAME_RE,
            item_hint=ENVFILE_NAME_HINT,
        )
        targets[name] = {"allowed_branches": branches, "allowed_env_files": env_files}
        if not prompt_yes_no("Add another target?", default=False):
            break
    return targets


def main() -> None:
    print("homelab-deploy-mcp config wizard")
    print("Generates one agent's config.yaml. Run this once per agent if")
    print("you're setting up more than one (see the README's 'Adding an")
    print("agent' section) — each gets its own file.\n")

    output_path = Path(prompt("Config file to write", default="config.yaml"))
    if output_path.exists() and not prompt_yes_no(
        f"{output_path} already exists. Overwrite it?", default=False
    ):
        print("Aborted.")
        return

    ssh_section = _collect_ssh_section()
    targets_section = _collect_targets()

    document = {"ssh": ssh_section, "targets": targets_section}
    output_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    print(f"\nWrote {output_path}.")

    try:
        load_config(output_path)
    except ConfigError as exc:
        print(f"\nWARNING: {output_path} was written but failed validation: {exc}", file=sys.stderr)
        print("Fix the issue above and re-run, or edit the file by hand.", file=sys.stderr)
        raise SystemExit(1) from exc

    print("Validated OK against the real config loader.")
    print("\nNext steps:")
    print(f"  1. Make sure {ssh_section['user']} exists on the homelab host and is")
    print("     in the homelab-deploy group (README 'Setup' steps 1-2).")
    print(f"  2. Install this agent's public key into {ssh_section['user']}'s")
    print("     authorized_keys on the host (README step 5).")
    print("  3. Make sure every target listed here also exists in the host's")
    print("     targets.conf (see `homelab-deploy-add-target`).")
    print(f"  4. Point HOMELAB_DEPLOY_MCP_CONFIG at {output_path} in this")
    print("     agent's MCP client config (README step 12).")


if __name__ == "__main__":
    main()
