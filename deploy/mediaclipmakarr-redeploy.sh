#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/mediaclipmakarr-redeploy.sh
# and wired up as a *forced command* for a dedicated SSH user (see the
# authorized_keys snippet in the README). sshd runs this script for every
# session under that key regardless of what the client asked to run, and
# puts whatever the client actually requested into $SSH_ORIGINAL_COMMAND.
#
# That means the allowlist below is enforced here even if the MCP server
# (or the machine it runs on) is ever compromised — the homelab host never
# trusts the caller, it only trusts its own copy of this list.
#
# This account is NOT in the `docker` group — that would be root-equivalent
# (anyone with docker-group access can `docker run --privileged -v /:/host
# ... chroot /host`, no compose file required). Instead, git/filesystem
# operations below run as this unprivileged user, and only the final
# docker-compose-up-root.sh script runs as root, via a `sudo` rule pinned
# to that exact script path (see sudoers.d/mediaclipmakarr-deploy).
#
# This account never reads or writes docker-compose.yml or any env file
# content — it only checks out source code. Selecting and activating an env
# file happens entirely inside docker-compose-up-root.sh, as root, so this
# account can't even read the env file's secrets, let alone edit compose
# config to add a bind mount or drop a capability restriction.
set -euo pipefail

REPO_DIR="/opt/mediaclipmakarr"
LOG_FILE="/var/log/mediaclipmakarr-deploy.log"

ALLOWED_BRANCHES=("main" "feature/make-clip-moc-update")
ALLOWED_ENV_FILES=("prod.env" "staging.env")

# Branch names and env filenames never contain spaces, so plain
# word-splitting is sufficient (and avoids pulling in extra parsing tools).
# `set -f` first disables glob expansion for that split — otherwise a
# crafted $SSH_ORIGINAL_COMMAND containing a metacharacter like `*` would
# expand against files in this script's CWD instead of being treated as a
# literal value.
set -f
# shellcheck disable=SC2086
set -- ${SSH_ORIGINAL_COMMAND:-}
set +f

if [[ "$#" -ne 3 ]]; then
  echo "usage (via SSH_ORIGINAL_COMMAND): <script-path> <branch> <env-file>" >&2
  exit 64
fi

branch="$2"
envfile="$3"

is_allowed() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

if ! is_allowed "$branch" "${ALLOWED_BRANCHES[@]}"; then
  echo "branch not allowed: $branch" >&2
  exit 65
fi

if ! is_allowed "$envfile" "${ALLOWED_ENV_FILES[@]}"; then
  echo "env file not allowed: $envfile" >&2
  exit 66
fi

{
  echo "=== $(date -u +%FT%TZ) deploying branch=$branch env=$envfile ==="
  cd "$REPO_DIR"
  git fetch origin
  git checkout "$branch"
  git reset --hard "origin/$branch"
  sudo /opt/deploy/docker-compose-up-root.sh "$envfile"
  echo "=== deploy finished with exit code $? ==="
} | tee -a "$LOG_FILE"
