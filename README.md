# homelab-deploy-mcp

A narrow, whitelisted [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code, Codex, etc.) redeploy one specific docker compose stack on your homelab — nothing more.

It exposes exactly one tool, `redeploy_media_clip_makarr(branch, env_file)`, which:

1. Validates `branch` and `env_file` against an allowlist in `config.yaml`.
2. Opens a single SSH connection to your homelab host, pinned to a known host key fingerprint.
3. Runs a forced command on that host (`mediaclipmakarr-redeploy.sh`) — a thin, root-owned script that re-validates the same allowlist independently, then hands off via `sudo` to a second root-owned script (`mediaclipmakarr-deploy-executor.sh`) that does everything privileged: re-validates the arguments a *third* time, resets a root-owned git checkout to exactly the requested branch's latest commit (removing any untracked or ignored files first), activates the requested pre-approved env file, and runs `docker compose up -d --force-recreate --build` against a compose file that also lives on the host.
4. Closes the connection and returns stdout/stderr/exit code.

The branch only ever supplies application source code, and the `mediaclipmakarr-deploy` SSH account never has write access to that checkout, the compose file, or any env file — only the root-owned executor touches any of it. Nothing pushed to a branch can add a bind mount, flip `privileged: true`, change an env var's value, or plant a file that sneaks into the build some other way.

## Why it's built this way

- **Reuses SSH, adds no new listener.** This server runs as a local subprocess on *your* machine (wherever you run Claude Code / Codex), not on the homelab, and connects out over SSH when a tool is called. To be precise about what this does and doesn't buy you: your homelab **does** need SSH reachable from wherever this runs — that's not optional, it's how the whole thing works. What this design avoids is adding a *new*, bespoke inbound service or port (an HTTP webhook listener, a custom agent port, etc.) on top of whatever SSH access you already have for administering the box.
- **Three independent allowlists.** The branch/env-file check happens here in Python, again in the unprivileged forced-command script (`mediaclipmakarr-redeploy.sh`, which reads its own hardcoded list from `$SSH_ORIGINAL_COMMAND` rather than trusting whatever the client sends), and a third time inside the root-owned executor it invokes via `sudo` (`mediaclipmakarr-deploy-executor.sh`). Any one of these being buggy or bypassed still leaves the others in place.
- **No `docker` group membership.** Adding the deploy account to the `docker` group would be root-equivalent — anyone with docker-group access can do `docker run --privileged -v /:/host ... chroot /host`, no compose file involved. Instead, `mediaclipmakarr-deploy` has no meaningful filesystem access at all beyond executing its one forced-command script; the moment anything needs to touch docker or git, it happens inside the root-owned executor via a `sudo` rule pinned to that one script.
- **The deploy account cannot write to the git checkout Docker builds from.** `/opt/mediaclipmakarr` is owned by root; `mediaclipmakarr-deploy` has no access to it. Only the root-owned executor ever touches it — and it doesn't trust whatever state the checkout happens to be in: every run does `git fetch`, resolves `refs/remotes/origin/<branch>` fresh, `git reset --hard`s to exactly that commit, and then `git clean -fdx`s. That last step matters more than it looks: `reset --hard` alone does *not* remove untracked files, so without an explicit clean, a file planted in that directory by any other means would survive a reset and still get pulled into the image by a Dockerfile's `COPY .` — never having gone through the branch/allowlist checks at all. `-x` also removes gitignored files, since a Dockerfile's `.dockerignore` is independent of `.gitignore`. (I verified this empirically in a sandboxed repo — tampering with a tracked file and planting an untracked one, then confirming both were gone before the simulated build ran — rather than just asserting it works.)
- **Compose and env files live on the host, never in the branch.** `docker-compose.yml` lives at `/opt/deploy/docker-compose.yml`, owned by root, installed once by a human — the deploy pipeline only ever reads it, never writes it, and it's never sourced from the git checkout. The one field in it that *does* point at the checkout is `build.context`, which is the actual "deploy this branch's code" mechanism. Env file contents work the same way: `mediaclipmakarr-deploy` can't even read `envfiles/*.env` (root-only, mode 600) — only the root-owned executor copies a pre-approved one into place as `/opt/deploy/active.env`, outside the build context entirely, so a `docker build` never sees it (compose's `env_file:` only affects the running container, not the build step).
- **Nothing here ever builds a shell command out of these values.** No `eval`, no `bash -c` fed with request data, no string-interpolated commands. Every git/docker/install invocation takes the branch and env-file values as plain argv entries. Combined with an exact-match allowlist (not just a regex) as the real gate, there's no injection surface even where `sudo` itself is configured permissively (see the executor's own comments on why its `sudo` rule allows any arguments).
- **Host key pinning.** The SSH client here doesn't use `known_hosts` or trust-on-first-use — it compares the presented host key's SHA256 fingerprint against the value you pin in `config.yaml` and aborts before running anything if it doesn't match.
- **No persistent access.** One SSH session per call, closed immediately after. No shell is left open, no session state is kept.
- **Deliberately narrow scope.** This does one thing. If you want to add read-only diagnostics (`docker ps`, `docker logs`) or `docker exec` later, do it as new, separately-reviewed tools with their own pinned `sudo` rules — don't fold arbitrary command execution into this one, and don't reach for `docker` group membership even then.

### If compose or env legitimately need to change

There is no tool for this, and there shouldn't be one. `docker-compose.yml` and the files under `envfiles/` are edited directly on the homelab host by a human — never through the MCP server, never by an agent. If an agent using this tool determines that a deploy needs a new env var, port, or volume, its job is to say so and stop: tell the human what's needed and why, and wait for them to make the change on the host (and add a new allowed env file to the three places it's enumerated — `config.yaml`, `mediaclipmakarr-redeploy.sh`, and `mediaclipmakarr-deploy-executor.sh` — if that's what's needed; the `sudo` rule doesn't need touching, since it's not pinned to specific argument values). Extending this tool to let an agent write compose/env configuration itself would undo the entire point of moving that configuration off the branch.

One honest caveat worth sitting with even after all of the above: this design controls *who can trigger* a build and *what a running container can access*, not what the build itself does. `docker compose ... --build` runs whatever `RUN` instructions are in the Dockerfile on the branch you deploy — inside an isolated build container, not directly on the host, but still executing code that came from the branch. The real remaining trust boundary is "every branch in `allowed_branches` only ever contains code (and a Dockerfile) you'd trust to build," which is a statement about your GitHub branch protection, not about anything in this repo. Consider adding a `CODEOWNERS` entry requiring your review on any change to `Dockerfile`, specifically, for extra scrutiny on that file without slowing down everything else.

## Setup

### 1. Create a dedicated, restricted user on the homelab host

```bash
sudo useradd --system --create-home --shell /bin/bash mediaclipmakarr-deploy
```

Using a dedicated account (not your own login, not root) means the SSH key this server holds can only ever do the one thing you've wired up for that account — even if the key leaks, the blast radius is capped. **Do not** add this account to the `docker` group — see "Why it's built this way" above for why.

### 2. Generate a dedicated SSH key pair (on the machine that will run this MCP server)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mediaclipmakarr_deploy_ed25519 -N "" -C "homelab-deploy-mcp"
```

### 3. Install both scripts and the git checkout, all root-owned

`mediaclipmakarr-deploy` must not be able to write to *any* of this — if it could edit the forced-command script, it could rewrite what the key does; if it could write to the checkout, it could plant something in the build context regardless of what the git allowlist logic does.

```bash
sudo mkdir -p /opt/deploy

# The forced-command entrypoint: root-owned, deploy account gets read+execute only.
sudo cp deploy/mediaclipmakarr-redeploy.sh /opt/deploy/mediaclipmakarr-redeploy.sh
sudo chown root:root /opt/deploy/mediaclipmakarr-redeploy.sh
sudo chmod 755 /opt/deploy/mediaclipmakarr-redeploy.sh

# The privileged executor: root-owned, deploy account gets NO direct access —
# it's only ever reached through sudo, which doesn't need the invoking user
# to have their own read/execute bits on the target.
sudo cp deploy/mediaclipmakarr-deploy-executor.sh /opt/deploy/mediaclipmakarr-deploy-executor.sh
sudo chown root:root /opt/deploy/mediaclipmakarr-deploy-executor.sh
sudo chmod 700 /opt/deploy/mediaclipmakarr-deploy-executor.sh

# The git checkout Docker builds from: also root-owned. mediaclipmakarr-deploy
# never touches this — only the executor does, as root, on every deploy.
sudo git clone <your-repo-url> /opt/mediaclipmakarr
```

Edit **`ALLOWED_BRANCHES`** and **`ALLOWED_ENV_FILES`** at the top of *both* `mediaclipmakarr-redeploy.sh` and `mediaclipmakarr-deploy-executor.sh` to match what you actually want allowed, keeping the two in sync with each other and with `config.yaml`.

### 4. Lock the SSH key to the forced command

As the `mediaclipmakarr-deploy` user, set up `~/.ssh/authorized_keys`:

```
restrict,command="/opt/deploy/mediaclipmakarr-redeploy.sh" ssh-ed25519 AAAA...your-public-key... homelab-deploy-mcp
```

`restrict` (OpenSSH 7.2+) disables port/X11/agent forwarding, pty allocation, and `~/.ssh/rc` execution all at once, and — unlike listing those individually — automatically covers anything OpenSSH adds to that list in later versions. `command=` means SSH always runs that script for this key, no matter what the client asks to run — the client's actual request lands in `$SSH_ORIGINAL_COMMAND` for the script to parse and re-validate.

If the machine running this MCP server has a stable address (a fixed LAN IP, or a Tailscale/VPN address), add `from=` to reject the key from anywhere else, even with the private key in hand:

```
restrict,command="/opt/deploy/mediaclipmakarr-redeploy.sh",from="192.168.1.0/24" ssh-ed25519 AAAA... homelab-deploy-mcp
```

Skip `from=` if that machine's address isn't stable (a laptop that roams networks, a cloud-hosted agent runtime) — a wrong or stale value here just breaks the connection, it doesn't fail open.

### 5. Install the compose file, env files, and the sudoers rule

Everything in this step is owned by `root`, not by `mediaclipmakarr-deploy`:

```bash
# The compose file: copy the example, then edit it for your real
# ports/volumes/service definitions before this goes anywhere near a
# running deploy. See deploy/docker-compose.example.yml's own comments.
sudo cp deploy/docker-compose.example.yml /opt/deploy/docker-compose.yml
sudo chown root:root /opt/deploy/docker-compose.yml
sudo chmod 644 /opt/deploy/docker-compose.yml

# Pre-approved env files, named exactly as they appear in config.yaml's
# allowed_env_files and in both scripts' ALLOWED_ENV_FILES arrays:
sudo mkdir -p /opt/deploy/envfiles
sudo cp /path/to/your/prod.env /opt/deploy/envfiles/prod.env
sudo chown -R root:root /opt/deploy/envfiles
sudo chmod 600 /opt/deploy/envfiles/*.env
# mediaclipmakarr-deploy gets no access to this directory at all — only
# the root-owned executor ever reads these files.
```

Then install the sudoers rule:

```bash
sudo visudo -cf deploy/sudoers.d/mediaclipmakarr-deploy   # validate syntax first
sudo install -m 440 -o root -g root deploy/sudoers.d/mediaclipmakarr-deploy /etc/sudoers.d/mediaclipmakarr-deploy
```

Always validate with `visudo -cf` before installing anything into `/etc/sudoers.d/` — a syntax error there can break `sudo` for the whole system.

Confirm it: as `mediaclipmakarr-deploy`, `sudo -l` should show exactly one allowed command (the executor, with any arguments), running `sudo /opt/deploy/mediaclipmakarr-deploy-executor.sh main prod.env` (or whatever you set as allowed) should work without a password prompt, and `sudo docker ps` (or anything not that exact script) should be refused.

### 6. Create the log location

`mediaclipmakarr-deploy` needs to be able to write its own log — `/var/log` itself typically isn't writable by non-root accounts, so give it a directory it owns:

```bash
sudo mkdir -p /var/log/mediaclipmakarr-deploy
sudo chown mediaclipmakarr-deploy:mediaclipmakarr-deploy /var/log/mediaclipmakarr-deploy
sudo chmod 750 /var/log/mediaclipmakarr-deploy
```

### 7. Get the host key fingerprint

From the homelab host itself (or any connection you already trust — don't take this from the client side):

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

### 8. Install this package

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
```

### 9. Configure

```bash
cp config.example.yaml config.yaml
```

Fill in `config.yaml` with your host, the `mediaclipmakarr-deploy` user, the private key path from step 2, the fingerprint from step 7, and your actual allowed branches/env files (matching what you put in both scripts in step 3).

`config.yaml` is gitignored — never commit it.

### 10. Register it with your MCP client

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

### 11. Test it

Ask your agent to call `redeploy_media_clip_makarr` with a branch and env file from your allowlist, and check `/var/log/mediaclipmakarr-deploy/deploy.log` on the homelab host to confirm it ran.

You can also test the SSH path directly, bypassing MCP entirely, to confirm the forced-command and sudo setup work before wiring up the agent side:

```bash
ssh -i ~/.ssh/mediaclipmakarr_deploy_ed25519 mediaclipmakarr-deploy@your-homelab-host \
  /opt/deploy/mediaclipmakarr-redeploy.sh deploy main prod.env
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover config validation and the host-key fingerprint helper — both pure logic, no network. There's no automated test for the actual SSH/git/docker path; the logic in `deploy/*.sh` was exercised against a sandboxed git repo with stubbed `sudo`/`docker`/`install` during development (including deliberately tampering with a tracked file and planting an untracked one, to confirm `reset --hard` + `clean -fdx` actually removes both before a build would run), but that isn't part of this repo's automated suite — verify the real path against a real (or throwaway test) host per step 11 above.
