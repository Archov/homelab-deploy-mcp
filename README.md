# homelab-deploy-mcp

A narrow, whitelisted [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code, Codex, etc.) redeploy any number of pre-configured docker compose projects on your homelab — and nothing else.

It exposes exactly one tool, `redeploy(target, branch, env_file)`, which:

1. Validates `target`, `branch`, and `env_file` against an allowlist in `config.yaml`.
2. Opens a single SSH connection to your homelab host, pinned to a known host key fingerprint, using whichever account and key belong to the calling agent — each agent gets its own, all of them members of one shared `homelab-deploy` group.
3. Runs a forced command on that host (`redeploy.sh`) — a thin, root-owned script that checks the arguments are at least well-formed, then hands off via `sudo` to a second root-owned script (`deploy-executor.sh`) that owns the real per-target table (paths, allowed branches, allowed env files) and does everything privileged: re-validates all three arguments against *that specific target's* configuration, resets *that target's* root-owned git checkout to exactly the requested branch's latest commit (removing any untracked or ignored files first), activates *that target's* requested pre-approved env file, and runs `docker compose up -d --force-recreate --build` against *that target's* own compose file.
4. Closes the connection and returns stdout/stderr/exit code.

`target` only ever selects among projects a human configured ahead of time on the homelab host — never a filesystem path, never a docker argument. Each target gets its own directory, its own compose file, its own env files, and its own branch allowlist; the executor keeps them completely separate, so a request scoped to one target can never touch another's checkout, compose file, or env file, and a branch or env file allowed for one target is rejected for any other it wasn't also explicitly allowed for.

## The threat model this is (and isn't) built for

This is a CYA measure against an agent going off-script — accidentally redeploying the wrong thing, running an unreviewed branch, or fat-fingering a docker invocation into something destructive — not a defense against a sophisticated attacker who already has an SSH private key. In that spirit, every account with any access here has *exactly* the same, narrow set of capabilities: run one root-owned executor with any arguments (which independently re-validates them against its own per-target table), nothing else. There is nothing to configure per account beyond adding it to the `homelab-deploy` group.

Within that, though, **every agent still gets its own Unix account and SSH key** — Claude gets one, Codex gets another, and so on — rather than everyone sharing a single identity. Since permissions come from group membership and not from anything tied to a specific account, this costs nothing extra to set up (see "Adding an agent" below) and buys two things a single shared account wouldn't: each agent's actions are individually attributable in the SSH and sudo logs, and revoking one agent's access (delete its key, or remove it from the group) doesn't touch any other agent's. What it doesn't do is isolate agents from *targets* — every account in the group can request a redeploy of every configured target; the host-side executor's table has no notion of *which* group member is asking, only whether the target/branch/env-file combination is valid at all. A narrower `config.yaml` (giving one agent a smaller `targets:` map than another) makes that agent's *own* MCP server refuse to attempt the rest — a real, useful default-deny for a well-behaved caller — but it's enforced client-side, in Python, not by the host. Anything with direct access to that agent's private key could SSH in and ask `redeploy.sh` for any target in the shared group's reach, same as any other member. If per-agent target restriction needs to be a hard boundary rather than a convenience, that's a case for per-agent accounts *outside* this shared group (with their own, separately-scoped sudoers rule and executor), not something this design provides today.

## Why it's built this way

- **Reuses SSH, adds no new listener.** This server runs as a local subprocess on *your* machine (wherever you run Claude Code / Codex), not on the homelab, and connects out over SSH when a tool is called. To be precise about what this does and doesn't buy you: your homelab **does** need SSH reachable from wherever this runs — that's not optional. What this design avoids is adding a *new*, bespoke inbound service or port on top of whatever SSH access you already have for administering the box.
- **Two independently-enforced checks, plus a syntax layer.** `config.yaml` (Python, client-side) rejects obviously-wrong requests before ever opening a connection, for fast feedback — but it's a mirror, not the enforcement. `redeploy.sh` (the forced-command script) checks the three arguments are syntactically sane before forwarding them, but deliberately does *not* keep its own copy of which targets/branches/env-files are actually allowed — that table exists in exactly one authoritative place: `deploy-executor.sh`, which independently re-validates all three against it before doing anything. A bug or bypass anywhere upstream of the executor still leaves that check in place.
- **No `docker` group membership, for any account.** Adding an account to the `docker` group would be root-equivalent — anyone with docker-group access can do `docker run --privileged -v /:/host ... chroot /host`, no compose file involved. Instead, no account in the `homelab-deploy` group has any meaningful filesystem access beyond executing the one forced-command script; the moment anything needs to touch docker or git, it happens inside the root-owned executor via a `sudo` rule pinned to that one script and granted to the group.
- **No account in the group can write to any target's git checkout.** Every target lives under `/opt/targets/<name>/`, owned by root; the group has no access to any of it, regardless of which member is asking. Only the root-owned executor ever touches it — and it doesn't trust whatever state a checkout happens to be in: every run does `git fetch`, resolves `refs/remotes/origin/<branch>` fresh, `git reset --hard`s to exactly that commit, and then `git clean -fdx`s. That last step matters more than it looks: `reset --hard` alone does *not* remove untracked files, so without an explicit clean, a file planted in that directory by any other means would survive a reset and still get pulled into the image by a Dockerfile's `COPY .` — never having gone through the branch allowlist at all. `-x` also removes gitignored files, since a Dockerfile's `.dockerignore` is independent of `.gitignore`.
- **Compose and env files live on the host, never in a branch.** Each target's `docker-compose.yml` lives at `/opt/targets/<name>/docker-compose.yml`, owned by root, installed once by a human — the deploy pipeline only ever reads it, never writes it, and it's never sourced from that target's git checkout. The one field in it that *does* point at the checkout is `build.context`, which is the actual "deploy this branch's code" mechanism. Env file contents work the same way: no group member can even read any target's `envfiles/*.env` (root-only, mode 600) — only the root-owned executor copies a pre-approved one into place as that target's `active.env`, outside the build context entirely, so a `docker build` never sees it.
- **Targets can't be confused with each other.** The executor looks up `TARGET_DIR`, `ALLOWED_BRANCHES`, and `ALLOWED_ENV_FILES` by the requested target name — a branch or env file allowed for one target is checked *only* against that target's own entry, never against another's. I verified this empirically with two sandboxed targets: a branch valid for one was correctly rejected when requested against the other, and a file planted in one target's checkout never appeared in the other's.
- **Concurrent deploys of the same target can't interleave.** Since any group member can trigger a deploy, two requests for the same target can genuinely race — two agents, or one retried. The executor takes a non-blocking `flock` on a per-target lock file before touching that target's checkout, and holds it (via the file descriptor `exec` inherits) through the final `docker compose` call, not just the git steps. A second concurrent request for the same target fails immediately with a clear "already in progress" message rather than silently interleaving its `reset`/`clean`/env-activation with the first one's, or sitting queued until it hits the caller's own SSH timeout with no explanation.
- **Nothing here ever builds a shell command out of these values.** No `eval`, no `bash -c` fed with request data, no string-interpolated commands. Every git/docker/install invocation takes `target`/`branch`/`env_file` as plain argv entries. Combined with an allowlist (not just a regex) as the real gate, there's no injection surface even where `sudo` itself is configured permissively (see the executor's own comments on why its `sudo` rule allows any arguments).
- **Branches can be allowed by exact name or by glob pattern; targets and env files can't.** An `allowed_branches` entry like `"codex/*"` allows any branch under that prefix — useful for an agent that creates its own branches on the fly rather than working off a small fixed set — matched with `fnmatch.fnmatchcase` on the Python side and bash's own `[[ == ]]` pattern matching in `deploy-executor.sh` (case-sensitive in both, deliberately: `fnmatch.fnmatch` case-folds on Windows via `os.path.normcase`, which would make a pattern match branches on a Windows-hosted MCP server that it wouldn't match on the Linux homelab host actually enforcing the same table — `fnmatchcase` avoids that split-brain). Target names and env-file names stay exact-match only on purpose — a target name doubles as a Compose project name and a filesystem path component, and an env file is secrets, not a source-code selector; neither has a legitimate reason to need a wildcard. I verified the glob path is genuinely safe against a subtler bug it could have introduced: splitting a pattern like `codex/*` via unquoted bash word-splitting would, without `set -f`, expand it against real files in the current working directory instead of keeping it literal — confirmed empirically that it still matches correctly even when run from a directory containing decoy files that would otherwise satisfy that glob.
- **Host key pinning.** The SSH client here doesn't use `known_hosts` or trust-on-first-use — it compares the presented host key's SHA256 fingerprint against the value you pin in `config.yaml` and aborts before running anything if it doesn't match.
- **No persistent access.** One SSH session per call, closed immediately after. No shell is left open, no session state is kept.
- **Deliberately narrow scope.** This does one thing, for as many targets as you configure. If you want to add read-only diagnostics (`docker ps`, `docker logs`) or `docker exec` later, do it as new, separately-reviewed tools with their own pinned `sudo` rules — don't fold arbitrary command execution into this one, and don't reach for `docker` group membership even then.

### If compose or env legitimately need to change

There is no tool for this, and there shouldn't be one. Each target's `docker-compose.yml` and the files under its `envfiles/` are edited directly on the homelab host by a human — never through the MCP server, never by an agent. If an agent using this tool determines that a deploy needs a new env var, port, or volume, its job is to say so and stop: tell the human what's needed and why, and wait for them to make the change on the host (and add a new allowed branch/env file to the two places it's enumerated — `config.yaml` and `deploy-executor.sh`'s tables — if that's what's needed; `redeploy.sh` and the sudoers rule don't need touching). Extending this tool to let an agent write compose/env configuration itself would undo the entire point of moving that configuration off the branch.

One honest caveat worth sitting with even after all of the above: this design controls *who can trigger* a build and *what a running container can access*, not what the build itself does. `docker compose ... --build` runs whatever `RUN` instructions are in a target's Dockerfile — inside an isolated build container, not directly on the host, but still executing code that came from the branch. The real remaining trust boundary is "every branch in a target's `allowed_branches` only ever contains code (and a Dockerfile) you'd trust to build," which is a statement about your GitHub branch protection, not about anything in this repo. A glob entry widens that statement from a short, individually-chosen list to an entire prefix: `"codex/*"` trusts *anything* pushed under `codex/`, by anyone able to push there, not just branches an agent itself created — make sure whatever can create a `codex/*` branch on your remote is exactly as trusted as a branch you'd have added to the list by name.

## Setup

### 1. Create the permission-bearing group

```bash
sudo groupadd --system homelab-deploy
```

This group, not any individual account, is what `sudoers` and the filesystem permissions below actually grant access to. **Do not** add it (or any account in it) to the `docker` group.

### 2. Create one Unix account per agent, each in that group

Repeat this for every agent you want to give access — Claude Code, Codex, whatever else:

```bash
agent=claude   # change per agent: claude, codex, ...

sudo useradd --system --create-home --shell /bin/bash --groups homelab-deploy "homelab-deploy-$agent"
```

Each account keeps its own default primary group (however your distro handles that) — `homelab-deploy` is a *supplementary* group here, which is all that's needed for the `sudoers`/file-permission rules below to apply.

### 3. Generate a dedicated SSH key pair per agent

Also on the machine that will run each agent's own copy of this MCP server:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/homelab_deploy_claude_ed25519 -N "" -C "homelab-deploy-mcp (claude)"
```

One key per agent — never share a private key between two agents. If two agents happen to run on the same machine, that's still two separate keypairs, one per agent's own config.

### 4. Install both scripts, group-readable, owned by root

No account should be able to write to either of these — if one could edit `redeploy.sh`, whoever controls that account could rewrite what every key in the group does; the executor is root-only and unreadable by the group entirely.

```bash
sudo mkdir -p /opt/deploy /opt/targets
sudo chgrp homelab-deploy /opt/deploy
sudo chmod 750 /opt/deploy

sudo cp deploy/redeploy.sh /opt/deploy/redeploy.sh
sudo chown root:homelab-deploy /opt/deploy/redeploy.sh
sudo chmod 750 /opt/deploy/redeploy.sh   # group can read+execute; nobody outside it can even see it

sudo cp deploy/deploy-executor.sh /opt/deploy/deploy-executor.sh
sudo chown root:root /opt/deploy/deploy-executor.sh
sudo chmod 700 /opt/deploy/deploy-executor.sh   # root-only; reached only via sudo, regardless of caller
```

Before or after copying it, edit `deploy-executor.sh`'s three associative arrays (`TARGET_DIR`, `ALLOWED_BRANCHES`, `ALLOWED_ENV_FILES`) to list your real targets — see "Adding a target" below.

### 5. Lock each agent's SSH key to the forced command

As each `homelab-deploy-<agent>` user, set up `~/.ssh/authorized_keys` with that agent's own public key:

```
restrict,command="/opt/deploy/redeploy.sh" ssh-ed25519 AAAA...claude's-public-key... homelab-deploy-mcp-claude
```

`restrict` (OpenSSH 7.2+) disables port/X11/agent forwarding, pty allocation, and `~/.ssh/rc` execution all at once, and — unlike listing those individually — automatically covers anything OpenSSH adds to that list in later versions. `command=` means SSH always runs that script for this key, no matter what the client asks to run — the client's actual request lands in `$SSH_ORIGINAL_COMMAND` for the script to parse. Every agent's key gets the exact same `command=` — what varies per agent is only the account and key, never the forced command itself.

If the machine running a given agent has a stable address (a fixed LAN IP, or a Tailscale/VPN address), add `from=` to that agent's line to reject its key from anywhere else, even with the private key in hand:

```
restrict,command="/opt/deploy/redeploy.sh",from="192.168.1.0/24" ssh-ed25519 AAAA... homelab-deploy-mcp-claude
```

Skip `from=` if that machine's address isn't stable — a wrong or stale value here just breaks the connection, it doesn't fail open.

### 6. Set up each target

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
# No account in the homelab-deploy group gets any access to this — only
# the root-owned executor ever reads it.
```

### 7. Install the sudoers rule

```bash
sudo visudo -cf deploy/sudoers.d/homelab-deploy   # validate syntax first
sudo install -m 440 -o root -g root deploy/sudoers.d/homelab-deploy /etc/sudoers.d/homelab-deploy
```

Always validate with `visudo -cf` before installing anything into `/etc/sudoers.d/` — a syntax error there can break `sudo` for the whole system. This rule grants the *group* (`%homelab-deploy`), not any specific account, so it doesn't need touching when you add a target, or an agent, later.

Confirm it: as any `homelab-deploy-<agent>` account, `sudo -l` should show exactly one allowed command (the executor, with any arguments), running `sudo /opt/deploy/deploy-executor.sh mediaclipmakarr main prod.env` (or whatever you configured) should work without a password prompt, and `sudo docker ps` (or anything not that exact script) should be refused.

### 8. Create the log location

Every agent's account writes to the same log file, so this needs to be genuinely group-writable, not owned by one account:

```bash
sudo mkdir -p /var/log/homelab-deploy
sudo chown root:homelab-deploy /var/log/homelab-deploy
sudo chmod 2770 /var/log/homelab-deploy
```

The `2` sets the setgid bit on the directory, so any log file created inside it inherits the `homelab-deploy` group regardless of which agent's account creates it first — without that, only the account that happened to create the file first would necessarily be able to write to it again later. `redeploy.sh` also sets `umask 007` before creating anything, so the file itself comes out group-writable too (a setgid directory alone fixes the *group*, not the *permission bits*, on a newly created file).

### 9. Get the host key fingerprint

From the homelab host itself (or any connection you already trust — don't take this from the client side):

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

This is the same for every agent — it's a property of the host, not the connecting account.

### 10. Install this package

Once per agent, since each agent runs its own copy of this MCP server:

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
```

(If several agents run on the same machine, they can share one `.venv` and installed package — it's the `config.yaml` each points at, below, that actually differs per agent.)

### 11. Configure — one `config.yaml` per agent

```bash
cp config.example.yaml config-claude.yaml   # one file per agent; name them however you like
```

Fill in each agent's own copy: your host, *that agent's* `homelab-deploy-<agent>` user and private key path from steps 2–3, `ssh.remote_script` (`/opt/deploy/redeploy.sh` — same for every agent), the fingerprint from step 9 (same for every agent), and a `targets:` entry for each project you want that agent able to reach — matching what you put in `deploy-executor.sh`'s tables in step 4. Give two agents different `targets:` maps if you want one of them restricted to fewer projects (see the caveat on that in "The threat model..." above).

Keep each of these gitignored — never commit them.

### 12. Register each agent's config with its MCP client

For Claude Code, add to `.mcp.json` (or your global MCP config):

```json
{
  "mcpServers": {
    "homelab-deploy": {
      "command": "/absolute/path/to/homelab-deploy-mcp/.venv/bin/python",
      "args": ["-m", "homelab_deploy_mcp.server"],
      "env": {
        "HOMELAB_DEPLOY_MCP_CONFIG": "/absolute/path/to/homelab-deploy-mcp/config-claude.yaml"
      }
    }
  }
}
```

Adjust the python path for Windows (`...\\.venv\\Scripts\\python.exe`) if applicable, and point `HOMELAB_DEPLOY_MCP_CONFIG` at that agent's own config file. Restart the MCP client after adding this.

### 13. Test it

Ask the agent to call `redeploy` with a target/branch/env file from its allowlist, and check `/var/log/homelab-deploy/deploy.log` on the homelab host to confirm it ran (and which account it ran as).

You can also test the SSH path directly, bypassing MCP entirely:

```bash
ssh -i ~/.ssh/homelab_deploy_claude_ed25519 homelab-deploy-claude@your-homelab-host \
  /opt/deploy/redeploy.sh deploy mediaclipmakarr main prod.env
```

## Adding a target

This is the whole point of the multi-target design — it should be small:

1. `config.yaml` (each agent's copy that should be able to reach it): add an entry under `targets:` with that project's `allowed_branches`/`allowed_env_files`.
2. `deploy-executor.sh` on the homelab host: add one line to each of `TARGET_DIR`, `ALLOWED_BRANCHES`, `ALLOWED_ENV_FILES`.
3. On the host: `sudo mkdir -p /opt/targets/<name>/envfiles`, clone the repo to `/opt/targets/<name>/repo`, install a `docker-compose.yml` there, and drop in the pre-approved env files — same as step 6 above.

Nothing else. The group, its accounts and keys, and the sudoers rule already cover it.

## Adding an agent

Also small, and doesn't touch anything target-related:

1. Create the account and add it to the group: `sudo useradd --system --create-home --shell /bin/bash --groups homelab-deploy homelab-deploy-<agent>` (step 2 above).
2. Generate that agent's own key pair (step 3 above).
3. Add that key to the new account's `~/.ssh/authorized_keys` with the same `restrict,command="/opt/deploy/redeploy.sh"` clause every other agent uses (step 5 above).
4. Give it its own `config.yaml` pointing at its own account/key (step 11 above), and register that with the agent's MCP client (step 12 above).

Nothing on the homelab host's privileged side — `redeploy.sh`, `deploy-executor.sh`, the sudoers rule — needs to change. That's the entire benefit of authorizing by group membership instead of by username.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover config validation (including the multi-target schema) and the host-key fingerprint helper — both pure logic, no network. There's no automated test for the actual SSH/git/docker path; the logic in `deploy/*.sh` was exercised during development against a sandboxed setup with two independent fake targets and stubbed `sudo`/`docker`/`install` — including deliberately tampering with a tracked file and planting an untracked one to confirm `reset --hard` + `clean -fdx` remove both before a build would run, and deliberately requesting one target's allowed branch against the other target to confirm the per-target tables don't leak into each other — but that isn't part of this repo's automated suite. Verify the real path against a real (or throwaway test) host per step 13 above.

The `umask 007` in `redeploy.sh` (for the shared, multi-account-writable log file — see setup step 8) is standard, well-documented POSIX behavior I'm confident is correct, but I couldn't get a trustworthy empirical check of it in this repo's own development environment: it's Windows/MSYS2 Git Bash, which doesn't faithfully emulate real Linux file-creation permission bits (a quick `umask 007; touch f; stat f` test here came back wrong in a way traceable to that emulation gap, not to the logic). Worth actually checking on the real host after step 8 — e.g. have two different group accounts each `tee -a` a line into the log and confirm both succeed.

The same environment gap applies to the per-target `flock` in `deploy-executor.sh`: `flock` isn't installed in this sandbox at all, so I could only verify the surrounding script logic (with a stub that always "succeeds") still behaves correctly, not real lock contention. `flock` ships standard on essentially every real Linux distribution and this is its textbook `exec N>file; flock -n N` usage, but it's worth confirming directly on the real host — fire two concurrent `deploy-executor.sh` invocations at the same target and confirm the second one fails fast with "already in progress" instead of interleaving with the first.
