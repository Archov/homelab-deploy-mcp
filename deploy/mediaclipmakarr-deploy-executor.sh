#!/usr/bin/env bash
#
# Installed on the homelab host at
# /opt/deploy/mediaclipmakarr-deploy-executor.sh, owned by root:root, mode
# 700 — mediaclipmakarr-deploy has NO direct read/write/execute access to
# this file. It only ever reaches it through the sudo rule in
# sudoers.d/mediaclipmakarr-deploy, which grants that with any arguments and
# relies entirely on THIS script to validate them — see that file's
# comments for why that split is intentional.
#
# This is also the only thing that ever touches /opt/mediaclipmakarr (the
# git checkout Docker builds from) or /opt/deploy/envfiles (the
# pre-approved env files). mediaclipmakarr-deploy owns neither directory
# and cannot write to either, so even a full compromise of that account —
# or of the forced-command script it invokes — cannot plant a file in the
# build context or read an env file's contents. It can only ask this
# script, via sudo, to run with two arguments that get independently
# re-validated here before anything happens.
#
# No eval, no bash -c, no string-built shell commands: every external
# command below is invoked with its arguments as separate argv entries, not
# interpolated into a command string a shell re-parses — so there is
# nothing here for a crafted branch or env-file value to inject into.
set -euo pipefail

REPO_DIR="/opt/mediaclipmakarr"
COMPOSE_FILE="/opt/deploy/docker-compose.yml"
PROJECT_DIR="/opt/deploy"
ENV_DIR="/opt/deploy/envfiles"
ACTIVE_ENV="/opt/deploy/active.env"

ALLOWED_BRANCHES=("main" "feature/make-clip-moc-update")
ALLOWED_ENV_FILES=("prod.env" "staging.env")

# Cheap, early rejection of obviously-malformed input. This is NOT the real
# security boundary — the exact-match allowlist checks below are — but it
# guarantees neither value can start with `-` (so it can never be mistaken
# for a flag by whatever it's passed to) and gives a clear error instead of
# an obscure git/docker failure on garbage input.
BRANCH_SYNTAX='^[A-Za-z0-9_][A-Za-z0-9_./-]*$'
ENVFILE_SYNTAX='^[A-Za-z0-9_][A-Za-z0-9_.-]*\.env$'

usage() {
  echo "usage: $(basename "$0") <branch> <env-file>" >&2
  exit 64
}

[[ "$#" -eq 2 ]] || usage

branch="$1"
envfile="$2"

is_allowed() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

if ! [[ "$branch" =~ $BRANCH_SYNTAX ]]; then
  echo "malformed branch: $branch" >&2
  exit 65
fi
if ! is_allowed "$branch" "${ALLOWED_BRANCHES[@]}"; then
  echo "branch not allowed: $branch" >&2
  exit 65
fi

if ! [[ "$envfile" =~ $ENVFILE_SYNTAX ]]; then
  echo "malformed env file: $envfile" >&2
  exit 66
fi
if ! is_allowed "$envfile" "${ALLOWED_ENV_FILES[@]}"; then
  echo "env file not allowed: $envfile" >&2
  exit 66
fi

cd "$REPO_DIR"
git fetch origin
if ! git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
  echo "branch not found on origin: $branch" >&2
  exit 67
fi
# Reset directly to the resolved remote-tracking ref rather than checking
# out a local branch — this account never trusts, and never depends on,
# whatever local branch state a previous run left behind.
git reset --hard "refs/remotes/origin/$branch"
# Untracked and ignored files survive `reset --hard` — without this, a file
# planted here by some other means (or left over from a previous run)
# would still be picked up by a Dockerfile's `COPY .` and end up in the
# built image, never having gone through the branch/allowlist checks above
# at all. `-x` also removes gitignored files, since a Dockerfile's
# .dockerignore is independent of .gitignore.
git clean -fdx

install -m 600 -o root -g root "$ENV_DIR/$envfile" "$ACTIVE_ENV"

exec docker compose -f "$COMPOSE_FILE" --project-directory "$PROJECT_DIR" up -d --force-recreate --build
