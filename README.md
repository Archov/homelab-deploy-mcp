# homelab-deploy-mcp

A narrow, whitelisted [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code, Codex, etc.) redeploy any number of pre-configured docker compose projects on your homelab — and nothing else.

It exposes exactly one tool, `redeploy(target, branch, env_file)`, which:

1. Validates `target`, `branch`, and `env_file` against an allowlist in `config.yaml`.
2. Opens a single SSH connection to your homelab host, pinned to a known host key fingerprint, using one shared account and key.
3. Runs a forced command on that host (`redeploy.sh`) — a thin, root-owned script that checks the arguments are at least well-formed, then hands off via `sudo` to a second root-owned script (`deploy-executor.sh`) that owns the real per-target table (paths, allowed branches, allowed env files) and does everything privileged: re-validates all three arguments against *that specific target's* configuration, resets *that target's* root-owned git checkout to exactly the requested branch's latest commit (removing any untracked or ignored files first), activates *that target's* requested pre-approved env file, and runs `docker compose up -d --force-recreate --build` against *that target's* own compose file.
4. Closes the connection and returns stdout/stderr/exit code.

`target` only ever selects among projects a human configured ahead of time on the homelab host — never a filesystem path, never a docker argument. Each target gets its own directory, its own compose file, its own env files, and its own branch allowlist; the executor keeps them completely separate, so a request scoped to one target can never touch another's checkout, compose file, or env file, and a branch or env file allowed for one target is rejected for any other it wasn't also explicitly allowed for.

## The threat model this is (and isn't) built for

This is a CYA measure against an agent going off-script — accidentally redeploying the wrong thing, running an unreviewed branch, or fat-fingering a docker invocation into something destructive — not a defense against a sophisticated attacker who already has your SSH private key. In that spirit it deliberately shares **one** SSH account and key across every configured target, rather than provisioning a separate account per project. The tradeoff that buys: far less to set up per new target (no new Unix account, no new key, no new sudoers/authorized_keys entry), at the cost of one property a fully-isolated-per-project design would have — if the shared key itself is ever compromised, that compromise reaches every configured target, not just one. It still doesn't reach anything *un*configured: the executor's per-target table is the only thing that says what's redeployable at all, and it's root-owned and untouched by the branch, the MCP request, or the shared account itself.

If that tradeoff stops being the right one for you — e.g. one target starts holding something you'd treat differently from the others — that's the point at which per-target accounts/keys are worth the extra setup; nothing here prevents layering that in later per target.

## Why it's built this way

- **Reuses SSH, adds no new listener.** This server runs as a local subprocess on *your* machine (wherever you run Claude Code / Codex), not on the homelab, and connects out over SSH when a tool is called. To be precise about what this does and doesn't buy you: your homelab **does** need SSH reachable from wherever this runs — that's not optional. What this design avoids is adding a *new*, bespoke inbound service or port on top of whatever SSH access you already have for administering the box.
- **Two independently-enforced checks, plus a syntax layer.** `config.yaml` (Python, client-side) rejects obviously-wrong requests before ever opening a connection, for fast feedback — but it's a mirror, not the enforcement. `redeploy.sh` (the forced-command script) checks the three arguments are syntactically sane before forwarding them, but deliberately does *not* keep its own copy of which targets/branches/env-files are actually allowed — that table exists in exactly one authoritative place: `deploy-executor.sh`, which independently re-validates all three against it before doing anything. A bug or bypass anywhere upstream of the executor still leaves that check in place.
- **No `docker` group membership.** Adding the deploy account to the `docker` group would be root-equivalent — anyone with docker-group access can do `docker run --privileged -v /:/host ... chroot /host`, no compose file involved. Instead, `homelab-deploy` has no meaningful filesystem access at all beyond executing its one forced-command script; the moment anything needs to touch docker or git, it happens inside the root-owned executor via a `sudo` rule pinned to that one script.
- **The deploy account cannot write to any target's git checkout.** Every target lives under `/opt/targets/<name>/`, owned by root; `homelab-deploy` has no access to any of it. Only the root-owned executor ever touches it — and it doesn't trust whatever state a checkout happens to be in: every run does `git fetch`, resolves `refs/remotes/origin/<branch>` fresh, `git reset --hard`s to exactly that commit, and then `git clean -fdx`s. That last step matters more than it looks: `reset --hard` alone does *not* remove untracked files, so without an explicit clean, a file planted in that directory by any other means would survive a reset and still get pulled into the image by a Dockerfile's `COPY .` — never having gone through the branch allowlist at all. `-x` also removes gitignored files, since a Dockerfile's `.dockerignore` is independent of `.gitignore`.
- **Compose and env files live on the host, never in a branch.** Each target's `docker-compose.yml` lives at `/opt/targets/<name>/docker-compose.yml`, owned by root, installed once by a human — the deploy pipeline only ever reads it, never writes it, and it's never sourced from that target's git checkout. The one field in it that *does* point at the checkout is `build.context`, which is the actual "deploy this branch's code" mechanism. Env file contents work the same way: `homelab-deploy` can't even read any target's `envfiles/*.env` (root-only, mode 600) — only the root-owned executor copies a pre-approved one into place as that target's `active.env`, outside the build context entirely, so a `docker build` never sees it.
- **Targets can't be confused with each other.** The executor looks up `TARGET_DIR`, `ALLOWED_BRANCHES`, and `ALLOWED_ENV_FILES` by the requested target name — a branch or env file allowed for one target is checked *only* against that target's own entry, never against another's. I verified this empirically with two sandboxed targets: a branch valid for one was correctly rejected when requested against the other, and a file planted in one target's checkout never appeared in the other's.
- **Nothing here ever builds a shell command out of these values.** No `eval`, no `bash -c` fed with request data, no string-interpolated commands. Every git/docker/install invocation takes `target`/`branch`/`env_file` as plain argv entries. Combined with an exact-match allowlist (not just a regex) as the real gate, there's no injection surface even where `sudo` itself is configured permissively (see the executor's own comments on why its `sudo` rule allows any arguments).
- **Host key pinning.** The SSH client here doesn't use `known_hosts` or trust-on-first-use — it compares the presented host key's SHA256 fingerprint against the value you pin in `config.yaml` and aborts before running anything if it doesn't match.
- **No persistent access.** One SSH session per call, closed immediately after. No shell is left open, no session state is kept.
- **Deliberately narrow scope.** This does one thing, for as many targets as you configure. If you want to add read-only diagnostics (`docker ps`, `docker logs`) or `docker exec` later, do it as new, separately-reviewed tools with their own pinned `sudo` rules — don't fold arbitrary command execution into this one, and don't reach for `docker` group membership even then.

### If compose or env legitimately need to change

There is no tool for this, and there shouldn't be one. Each target's `docker-compose.yml` and the files under its `envfiles/` are edited directly on the homelab host by a human — never through the MCP server, never by an agent. If an agent using this tool determines that a deploy needs a new env var, port, or volume, its job is to say so and stop: tell the human what's needed and why, and wait for them to make the change on the host (and add a new allowed branch/env file to the two places it's enumerated — `config.yaml` and `deploy-executor.sh`'s tables — if that's what's needed; `redeploy.sh` and the sudoers rule don't need touching). Extending this tool to let an agent write compose/env configuration itself would undo the entire point of moving that configuration off the branch.

One honest caveat worth sitting with even after all of the above: this design controls *who can trigger* a build and *what a running container can access*, not what the build itself does. `docker compose ... --build` runs whatever `RUN` instructions are in a target's Dockerfile — inside an isolated build container, not directly on the host, but still executing code that came from the branch. The real remaining trust boundary is "every branch in a target's `allowed_branches` only ever contains code (and a Dockerfile) you'd trust to build," which is a statement about your GitHub branch protection, not about anything in this repo.

## Setup

### 1. Create the shared, restricted user on the homelab host

```bash
sudo useradd --system --create-home --shell /bin/bash homelab-deploy
```

One account, shared across every target you configure — see "The threat model this is (and isn't) built for" above. **Do not** add this account to the `docker` group.

### 2. Generate a dedicated SSH key pair (on the machine that will run this MCP server)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/homelab_deploy_ed25519 -N "" -C "homelab-deploy-mcp"
```

### 3. Install both scripts, root-owned

`homelab-deploy` must not be able to write to either of these — if it could edit `redeploy.sh`, it could rewrite what the key does; the executor is root-only and unreadable by the account entirely.

```bash
sudo mkdir -p /opt/deploy /opt/targets

sudo cp deploy/redeploy.sh /opt/deploy/redeploy.sh
sudo chown root:root /opt/deploy/redeploy.sh
sudo chmod 755 /opt/deploy/redeploy.sh

sudo cp deploy/deploy-executor.sh /opt/deploy/deploy-executor.sh
sudo chown root:root /opt/deploy/deploy-executor.sh
sudo chmod 700 /opt/deploy/deploy-executor.sh
```

Before or after copying it, edit `deploy-executor.sh`'s three associative arrays (`TARGET_DIR`, `ALLOWED_BRANCHES`, `ALLOWED_ENV_FILES`) to list your real targets — see "Adding a target" below.

### 4. Lock the SSH key to the forced command

As the `homelab-deploy` user, set up `~/.ssh/authorized_keys`:

```
restrict,command="/opt/deploy/redeploy.sh" ssh-ed25519 AAAA...your-public-key... homelab-deploy-mcp
```

`restrict` (OpenSSH 7.2+) disables port/X11/agent forwarding, pty allocation, and `~/.ssh/rc` execution all at once, and — unlike listing those individually — automatically covers anything OpenSSH adds to that list in later versions. `command=` means SSH always runs that script for this key, no matter what the client asks to run — the client's actual request lands in `$SSH_ORIGINAL_COMMAND` for the script to parse.

If the machine running this MCP server has a stable address (a fixed LAN IP, or a Tailscale/VPN address), add `from=` to reject the key from anywhere else, even with the private key in hand:

```
restrict,command="/opt/deploy/redeploy.sh",from="192.168.1.0/24" ssh-ed25519 AAAA... homelab-deploy-mcp
```

Skip `from=` if that machine's address isn't stable — a wrong or stale value here just breaks the connection, it doesn't fail open.

### 5. Set up each target

Repeat this per project. Everything here is owned by `root`, not by `homelab-deploy`:

```bash
target=mediaclipmakarr   # change per target

sudo mkdir -p "/opt/targets/$target/envfiles"

sudo git clone <your-repo-url> "/opt/targets/$target/repo"

# Copy the example, then edit it for this target's real ports/volumes/service
# definitions before this goes anywhere near a running deploy. See
# deploy/docker-compose.example.yml's own comments.
sudo cp deploy/docker-compose.example.yml "/opt/targets/$target/docker-compose.yml"
sudo chown root:root "/opt/targets/$target/docker-compose.yml"
sudo chmod 644 "/opt/targets/$target/docker-compose.yml"

# Pre-approved env files, named exactly as they appear in this target's
# entry in config.yaml's targets map AND deploy-executor.sh's
# ALLOWED_ENV_FILES:
sudo cp /path/to/your/prod.env "/opt/targets/$target/envfiles/prod.env"
sudo chown -R root:root "/opt/targets/$target"
sudo chmod 600 "/opt/targets/$target"/envfiles/*.env
# homelab-deploy gets no access to any of this — only the root-owned
# executor ever reads it.
```

### 6. Install the sudoers rule

```bash
sudo visudo -cf deploy/sudoers.d/homelab-deploy   # validate syntax first
sudo install -m 440 -o root -g root deploy/sudoers.d/homelab-deploy /etc/sudoers.d/homelab-deploy
```

Always validate with `visudo -cf` before installing anything into `/etc/sudoers.d/` — a syntax error there can break `sudo` for the whole system. This rule doesn't need touching when you add a target later — it grants the executor with any arguments and relies on the executor's own table (see the sudoers file's own comments for why that's safe here).

Confirm it: as `homelab-deploy`, `sudo -l` should show exactly one allowed command (the executor, with any arguments), running `sudo /opt/deploy/deploy-executor.sh mediaclipmakarr main prod.env` (or whatever you configured) should work without a password prompt, and `sudo docker ps` (or anything not that exact script) should be refused.

### 7. Create the log location

```bash
sudo mkdir -p /var/log/homelab-deploy
sudo chown homelab-deploy:homelab-deploy /var/log/homelab-deploy
sudo chmod 750 /var/log/homelab-deploy
```

### 8. Get the host key fingerprint

From the homelab host itself (or any connection you already trust — don't take this from the client side):

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

### 9. Install this package

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
```

### 10. Configure

```bash
cp config.example.yaml config.yaml
```

Fill in `config.yaml`: your host, the `homelab-deploy` user, the private key path from step 2, `ssh.remote_script` (`/opt/deploy/redeploy.sh`), the fingerprint from step 8, and a `targets:` entry for each project — matching what you put in `deploy-executor.sh`'s tables in step 3.

`config.yaml` is gitignored — never commit it.

### 11. Register it with your MCP client

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

### 12. Test it

Ask your agent to call `redeploy` with a target/branch/env file from your allowlist, and check `/var/log/homelab-deploy/deploy.log` on the homelab host to confirm it ran.

You can also test the SSH path directly, bypassing MCP entirely:

```bash
ssh -i ~/.ssh/homelab_deploy_ed25519 homelab-deploy@your-homelab-host \
  /opt/deploy/redeploy.sh deploy mediaclipmakarr main prod.env
```

## Adding a target

This is the whole point of the multi-target design — it should be small:

1. `config.yaml`: add an entry under `targets:` with that project's `allowed_branches`/`allowed_env_files`.
2. `deploy-executor.sh` on the homelab host: add one line to each of `TARGET_DIR`, `ALLOWED_BRANCHES`, `ALLOWED_ENV_FILES`.
3. On the host: `sudo mkdir -p /opt/targets/<name>/envfiles`, clone the repo to `/opt/targets/<name>/repo`, install a `docker-compose.yml` there, and drop in the pre-approved env files — same as step 5 above.

Nothing else. The shared account, key, `authorized_keys` entry, and sudoers rule already cover it.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover config validation (including the multi-target schema) and the host-key fingerprint helper — both pure logic, no network. There's no automated test for the actual SSH/git/docker path; the logic in `deploy/*.sh` was exercised during development against a sandboxed setup with two independent fake targets and stubbed `sudo`/`docker`/`install` — including deliberately tampering with a tracked file and planting an untracked one to confirm `reset --hard` + `clean -fdx` remove both before a build would run, and deliberately requesting one target's allowed branch against the other target to confirm the per-target tables don't leak into each other — but that isn't part of this repo's automated suite. Verify the real path against a real (or throwaway test) host per step 12 above.
