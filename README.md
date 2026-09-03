# homelab-deploy-mcp

A narrow, whitelisted [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code, Codex, etc.) redeploy one specific docker compose stack on your homelab — nothing more.

It exposes exactly one tool, `redeploy_media_clip_makarr(branch, env_file)`, which:

1. Validates `branch` and `env_file` against an allowlist in `config.yaml`.
2. Opens a single SSH connection to your homelab host, pinned to a known host key fingerprint.
3. Runs a forced command on that host — a script installed ahead of time that re-validates the same allowlist independently, then does `git checkout <branch>`, swaps in the requested env file, and runs `docker compose up -d --force-recreate --build` **as root via a single-purpose `sudo` rule** (see below — the deploy account is never added to the `docker` group).
4. Closes the connection and returns stdout/stderr/exit code.

## Why it's built this way

- **Outbound only.** This server runs as a local subprocess on *your* machine (wherever you run Claude Code / Codex), not on the homelab. It makes an outbound SSH connection when a tool is called; your homelab never needs an inbound port opened, forwarded, or exposed on a VPN for this to work.
- **Two independent allowlists.** The branch/env-file check happens here in Python *and* again in the homelab-side script (`deploy/mediaclipmakarr-redeploy.sh`), which reads its own hardcoded list from `$SSH_ORIGINAL_COMMAND` rather than trusting whatever the client sends. If this server or the machine it runs on is ever compromised, the homelab-side script still refuses anything off-list.
- **No `docker` group membership.** Adding the deploy account to the `docker` group would be root-equivalent — anyone with docker-group access can do `docker run --privileged -v /:/host ... chroot /host`, no compose file involved. Instead, the deploy account only has ordinary filesystem access to the repo checkout and env files; the one moment it needs to touch docker, it does so through a `sudo` rule pinned to a single, argument-free, root-owned script (`deploy/docker-compose-up-root.sh`). Even a full compromise of this account is capped at "can run that one exact command as root," not "can talk to the docker daemon."
- **Host key pinning.** The SSH client here doesn't use `known_hosts` or trust-on-first-use — it compares the presented host key's SHA256 fingerprint against the value you pin in `config.yaml` and aborts before running anything if it doesn't match.
- **No persistent access.** One SSH session per call, closed immediately after. No shell is left open, no session state is kept.
- **Deliberately narrow scope.** This does one thing. If you want to add read-only diagnostics (`docker ps`, `docker logs`) or `docker exec` later, do it as new, separately-reviewed tools with their own pinned `sudo` rules — don't fold arbitrary command execution into this one, and don't reach for `docker` group membership even then.

One honest caveat worth sitting with: this whole design controls *who can trigger* a build, not what the build does. `docker compose ... --build` runs whatever `RUN` instructions are in the Dockerfile on the branch you deploy, as root, by design — that's what building an image is. The real trust boundary is "every branch in `allowed_branches` only ever contains code you'd let run as root," which is a statement about your GitHub branch protection, not about anything in this repo. Keep that list to branches only you (or people you fully trust) can push to.

## Setup

### 1. Create a dedicated, restricted user on the homelab host

```bash
sudo useradd --system --create-home --shell /bin/bash mediaclipmakarr-deploy
```

Using a dedicated account (not your own login, not root) means the SSH key this server holds can only ever do the one thing you've wired up for that account — even if the key leaks, the blast radius is capped. **Do not** add this account to the `docker` group — see "Why it's built this way" above for why, and step 3b below for the actual mechanism this uses instead.

### 2. Generate a dedicated SSH key pair (on the machine that will run this MCP server)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mediaclipmakarr_deploy_ed25519 -N "" -C "homelab-deploy-mcp"
```

Copy the **public** key to the homelab host's `mediaclipmakarr-deploy` user, but don't just append it normally — install it with a forced command (see next step) so SSH itself refuses anything except the one script, regardless of what this server ever sends.

### 3. Install the deploy script and lock the key to it

On the homelab host:

```bash
sudo mkdir -p /opt/deploy/envfiles
sudo cp deploy/mediaclipmakarr-redeploy.sh /opt/deploy/mediaclipmakarr-redeploy.sh
sudo chmod 750 /opt/deploy/mediaclipmakarr-redeploy.sh
sudo chown mediaclipmakarr-deploy:mediaclipmakarr-deploy /opt/deploy/mediaclipmakarr-redeploy.sh

# Put your pre-approved env files here, named exactly as they appear in
# config.yaml's allowed_env_files:
sudo cp /path/to/your/prod.env /opt/deploy/envfiles/prod.env
sudo chown -R mediaclipmakarr-deploy:mediaclipmakarr-deploy /opt/deploy/envfiles
sudo chmod 600 /opt/deploy/envfiles/*.env
```

Edit **`ALLOWED_BRANCHES`** and **`ALLOWED_ENV_FILES`** at the top of `mediaclipmakarr-redeploy.sh` to match what you actually want allowed — this list is the real security boundary, independent of `config.yaml`.

Then, as the `mediaclipmakarr-deploy` user, set up `~/.ssh/authorized_keys`:

```
command="/opt/deploy/mediaclipmakarr-redeploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...your-public-key... homelab-deploy-mcp
```

The `command=` clause means SSH always runs that script for this key, no matter what the client asks to run — the client's actual request lands in `$SSH_ORIGINAL_COMMAND` for the script to parse and re-validate.

Also make sure `REPO_DIR` in the script points at an existing clone of your repo that this user can read/write (the clone itself, and its `.env`, need to be writable by `mediaclipmakarr-deploy` — the docker step is handled separately below).

### 3b. Grant the one docker capability this account actually needs

This is the piece that replaces `usermod -aG docker`. Install the root-owned script that actually calls `docker compose`:

```bash
sudo cp deploy/docker-compose-up-root.sh /opt/deploy/docker-compose-up-root.sh
sudo chown root:root /opt/deploy/docker-compose-up-root.sh
sudo chmod 750 /opt/deploy/docker-compose-up-root.sh
```

It must stay owned by `root` and **not** be writable by `mediaclipmakarr-deploy` — if that account could edit the script, granting sudo on it would be meaningless, since it could edit it to do anything before running it.

Then install the sudoers rule that lets `mediaclipmakarr-deploy` run *only* that exact script, as root, with no password:

```bash
sudo visudo -cf deploy/sudoers.d/mediaclipmakarr-deploy   # validate syntax first
sudo install -m 440 -o root -g root deploy/sudoers.d/mediaclipmakarr-deploy /etc/sudoers.d/mediaclipmakarr-deploy
```

Always validate with `visudo -cf` before installing anything into `/etc/sudoers.d/` — a syntax error there can break `sudo` for the whole system.

Confirm it: as `mediaclipmakarr-deploy`, `sudo -l` should show exactly one allowed command, and running `sudo /opt/deploy/docker-compose-up-root.sh` should work without a password prompt while `sudo docker ps` (or anything else) should be refused.

### 4. Get the host key fingerprint

From the homelab host itself (or any connection you already trust — don't take this from the client side):

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

### 5. Install this package

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
```

### 6. Configure

```bash
cp config.example.yaml config.yaml
```

Fill in `config.yaml` with your host, the `mediaclipmakarr-deploy` user, the private key path from step 2, the fingerprint from step 4, and your actual allowed branches/env files (matching what you put in the script in step 3).

`config.yaml` is gitignored — never commit it.

### 7. Register it with your MCP client

For Claude Code, add to `.mcp.json` (or your global MCP config):

```json
{
  "mcpServers": {
    "homelab-deploy": {
      "command": "/absolute/path/to/homelab-deploy-mcp/.venv/bin/python",
      "args": ["-m", "homelab_deploy_mcp.server"],
      "env": {
        "HOMELAB_DEPLOY_MCP_CONFIG": "/absolute/path/to/homelab-deploy-mcp/config.yaml"
      }
    }
  }
}
```

Adjust the python path for Windows (`...\\.venv\\Scripts\\python.exe`) if applicable. Restart your MCP client after adding this.

### 8. Test it

Ask your agent to call `redeploy_media_clip_makarr` with a branch and env file from your allowlist, and check `/var/log/mediaclipmakarr-deploy.log` on the homelab host to confirm it ran.

You can also test the SSH path directly, bypassing MCP entirely, to confirm the forced-command setup works before wiring up the agent side:

```bash
ssh -i ~/.ssh/mediaclipmakarr_deploy_ed25519 mediaclipmakarr-deploy@your-homelab-host \
  /opt/deploy/mediaclipmakarr-redeploy.sh main prod.env
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover config validation and the host-key fingerprint helper — both pure logic, no network. There's no automated test for the actual SSH path; verify that against a real (or throwaway test) host per step 8 above.
