#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/docker-compose-up-root.sh,
# owned by root:root, mode 750 — NOT writable by mediaclipmakarr-deploy.
# This is the ONLY thing that account can ever run as root (see
# sudoers.d/mediaclipmakarr-deploy), and its one argument (which pre-approved
# env file to activate) is independently re-validated here against its own
# hardcoded list, then re-validated a THIRD time by sudoers itself, which
# only permits these exact two invocations — see sudoers.d/mediaclipmakarr-deploy.
#
# Deliberately reads NOTHING from the git checkout except the source code to
# build from (via `build.context` in docker-compose.yml, below). The compose
# file and the env file contents are both fixed, root-owned assets on this
# host — never sourced from, or overwritten by, anything in the git branch.
# That's the point: a compromised or malicious push to an allowed branch can
# change the application code, but cannot add a bind mount, flip
# `privileged: true`, or smuggle a different value into an env var, because
# none of that configuration ever comes from the branch.
set -euo pipefail

COMPOSE_FILE="/opt/deploy/docker-compose.yml"
PROJECT_DIR="/opt/deploy"
ENV_DIR="/opt/deploy/envfiles"
ACTIVE_ENV="/opt/deploy/active.env"

ALLOWED_ENV_FILES=("prod.env" "staging.env")

envfile="${1:?envfile required}"

is_allowed() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

if ! is_allowed "$envfile" "${ALLOWED_ENV_FILES[@]}"; then
  echo "env file not allowed: $envfile" >&2
  exit 66
fi

install -m 600 -o root -g root "$ENV_DIR/$envfile" "$ACTIVE_ENV"

exec docker compose -f "$COMPOSE_FILE" --project-directory "$PROJECT_DIR" up -d --force-recreate --build
