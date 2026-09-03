# homelab-deploy-mcp

A narrow, whitelisted [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code, Codex, etc.) redeploy one specific docker compose stack on your homelab — nothing more.

It exposes exactly one tool, `redeploy_media_clip_makarr(branch, env_file)`, which:

1. Validates `branch` and `env_file` against an allowlist in `config.yaml`.
2. Opens a single SSH connection to your homelab host, pinned to a known host key fingerprint.
3. Runs a forced command on that host — a script installed ahead of time that re-validates the same allowlist independently, then does `git checkout <branch>` to pull in the requested code, and hands off to a `sudo`-restricted root script that activates the requested (pre-approved, host-side) env file and runs `docker compose up -d --force-recreate --build` **against a compose file that also lives on the host, never in the git branch**.
4. Closes the connection and returns stdout/stderr/exit code.

The branch only ever supplies application source code. The compose file, the env file contents, and which env files exist at all are fixed assets on the homelab host — nothing pushed to a branch can add a bind mount, flip `privileged: true`, or change an env var's value, because the deploy pipeline never reads any of that from the branch in the first place.

## Why it's built this way

- **Outbound only.** This server runs as a local subprocess on *your* machine (wherever you run Claude Code / Codex), not on the homelab. It makes an outbound SSH connection when a tool is called; your homelab never needs an inbound port opened, forwarded, or exposed on a VPN for this to work.
- **Three independent allowlists.** The branch/env-file check happens here in Python, again in the homelab-side script (`deploy/mediaclipmakarr-redeploy.sh`, which reads its own hardcoded list from `$SSH_ORIGINAL_COMMAND` rather than trusting whatever the client sends), and a third time inside the root-owned script that actually activates an env file (`deploy/docker-compose-up-root.sh`). Any one of these being buggy or bypassed still leaves the others in place.
- **No `docker` group membership.** Adding the deploy account to the `docker` group would be root-equivalent — anyone with docker-group access can do `docker run --privileged -v /:/host ... chroot /host`, no compose file involved. Instead, the deploy account only has ordinary filesystem access to the git checkout; the one moment it needs to touch docker, it does so through a `sudo` rule pinned to an exact, enumerated set of invocations of one root-owned script (`deploy/docker-compose-up-root.sh`). Even a full compromise of this account is capped at "can run one of these two exact commands as root," not "can talk to the docker daemon."
- **Compose and env files live on the host, never in the branch.** `docker-compose.yml` lives at `/opt/deploy/docker-compose.yml`, owned by root, installed once by a human — the deploy pipeline only ever reads it, never writes it, and it's never sourced from the git checkout. The one field in it that *does* point at the checkout is `build.context`, which is the actual "deploy this branch's code" mechanism. Env file contents work the same way: the deploy account can't even read `envfiles/*.env` (root-only, mode 600) — only the root-owned script copies a pre-approved one into place. A push to an allowed branch can change application code; it cannot add a bind mount, flip `privileged: true`, or change what an env var is set to, because none of that configuration is ever read from the branch.
- **Host key pinning.** The SSH client here doesn't use `known_hosts` or trust-on-first-use — it compares the presented host key's SHA256 fingerprint against the value you pin in `config.yaml` and aborts before running anything if it doesn't match.
- **No persistent access.** One SSH session per call, closed immediately after. No shell is left open, no session state is kept.
- **Deliberately narrow scope.** This does one thing. If you want to add read-only diagnostics (`docker ps`, `docker logs`) or `docker exec` later, do it as new, separately-reviewed tools with their own pinned `sudo` rules — don't fold arbitrary command execution into this one, and don't reach for `docker` group membership even then.

### If compose or env legitimately need to change

There is no tool for this, and there shouldn't be one. `docker-compose.yml` and the files under `envfiles/` are edited directly on the homelab host by a human — never through the MCP server, never by an agent. If an agent using this tool determines that a deploy needs a new env var, port, or volume, its job is to say so and stop: tell the human what's needed and why, and wait for them to make the change on the host (and add a new allowed env file to all four places it's enumerated — `config.yaml`, `mediaclipmakarr-redeploy.sh`, `docker-compose-up-root.sh`, and the sudoers file — if that's what's needed). Extending this tool to let an agent write compose/env configuration itself would undo the entire point of moving that configuration off the branch.

One honest caveat worth sitting with even after all of the above: this design controls *who can trigger* a build and *what a running container can access*, not what the build itself does. `docker compose ... --build` runs whatever `RUN` instructions are in the Dockerfile on the branch you deploy, as root, by design — that's what building an image is. The real remaining trust boundary is "every branch in `allowed_branches` only ever contains code you'd let run as root," which is a statement about your GitHub branch protection, not about anything in this repo. Keep that list to branches only you (or people you fully trust) can push to.

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
sudo mkdir -p /opt/deploy
sudo cp deploy/mediaclipmakarr-redeploy.sh /opt/deploy/mediaclipmakarr-redeploy.sh
sudo chmod 750 /opt/deploy/mediaclipmakarr-redeploy.sh
sudo chown mediaclipmakarr-deploy:mediaclipmakarr-deploy /opt/deploy/mediaclipmakarr-redeploy.sh

# Clone your repo where this account can read/write it. It only ever
# needs source code here — no compose file, no env file, no docker access.
sudo git clone <your-repo-url> /opt/mediaclipmakarr
sudo chown -R mediaclipmakarr-deploy:mediaclipmakarr-deploy /opt/mediaclipmakarr
```

Edit **`ALLOWED_BRANCHES`** and **`ALLOWED_ENV_FILES`** at the top of `mediaclipmakarr-redeploy.sh` to match what you actually want allowed — this list is the real security boundary, independent of `config.yaml`.

Then, as the `mediaclipmakarr-deploy` user, set up `~/.ssh/authorized_keys`:

```
command="/opt/deploy/mediaclipmakarr-redeploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...your-public-key... homelab-deploy-mcp
```

The `command=` clause means SSH always runs that script for this key, no matter what the client asks to run — the client's actual request lands in `$SSH_ORIGINAL_COMMAND` for the script to parse and re-validate.

### 3b. Install the compose file, env files, and the docker capability this account actually needs

This is the piece that replaces `usermod -aG docker`, and where compose/env configuration is pinned to the host instead of the branch. Everything in this step is owned by `root`, not by `mediaclipmakarr-deploy`:

```bash
# The compose file: copy the example, then edit it for your real
# ports/volumes/service definitions before this goes anywhere near a
# running deploy. See deploy/docker-compose.example.yml's own comments.
sudo cp deploy/docker-compose.example.yml /opt/deploy/docker-compose.yml
sudo chown root:root /opt/deploy/docker-compose.yml
sudo chmod 644 /opt/deploy/docker-compose.yml

# Pre-approved env files, named exactly as they appear in
# config.yaml's allowed_env_files AND in ALLOWED_ENV_FILES inside
# docker-compose-up-root.sh:
sudo mkdir -p /opt/deploy/envfiles
sudo cp /path/to/your/prod.env /opt/deploy/envfiles/prod.env
sudo chown -R root:root /opt/deploy/envfiles
sudo chmod 600 /opt/deploy/envfiles/*.env
# mediaclipmakarr-deploy gets no access to this directory at all — only
# the root-owned script below ever reads these files.

# The root-owned script that activates an env file and runs compose:
sudo cp deploy/docker-compose-up-root.sh /opt/deploy/docker-compose-up-root.sh
sudo chown root:root /opt/deploy/docker-compose-up-root.sh
sudo chmod 750 /opt/deploy/docker-compose-up-root.sh
```

`docker-compose-up-root.sh` must stay owned by `root` and **not** be writable by `mediaclipmakarr-deploy` — if that account could edit the script, granting sudo on it would be meaningless, since it could edit it to do anything before running it. Edit its `ALLOWED_ENV_FILES` array to match your real list, keeping it in sync with `config.yaml`, `mediaclipmakarr-redeploy.sh`, and the sudoers file below.

Then install the sudoers rule that lets `mediaclipmakarr-deploy` run *only* the exact, enumerated invocations of that script, as root, with no password:

```bash
sudo visudo -cf deploy/sudoers.d/mediaclipmakarr-deploy   # validate syntax first
sudo install -m 440 -o root -g root deploy/sudoers.d/mediaclipmakarr-deploy /etc/sudoers.d/mediaclipmakarr-deploy
```

If you add or rename an allowed env file, update the `Cmnd_Alias` lines in that sudoers file to match — sudo matches the full command line exactly, so `docker-compose-up-root.sh newenv.env` won't work until it's listed there too.

Always validate with `visudo -cf` before installing anything into `/etc/sudoers.d/` — a syntax error there can break `sudo` for the whole system.

Confirm it: as `mediaclipmakarr-deploy`, `sudo -l` should show exactly the two allowed commands, running `sudo /opt/deploy/docker-compose-up-root.sh prod.env` should work without a password prompt, and `sudo /opt/deploy/docker-compose-up-root.sh anything-else` (or `sudo docker ps`) should be refused.

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
