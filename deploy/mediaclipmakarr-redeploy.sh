#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/mediaclipmakarr-redeploy.sh,
# owned by root:root, mode 755 — mediaclipmakarr-deploy can read and
# execute this file but cannot write to it. If that account could edit
# this script, whoever controls it could rewrite what the forced SSH
# command actually does, which would defeat the entire point of forcing it.
#
# Wired up as the forced command for mediaclipmakarr-deploy's SSH key (see
# the authorized_keys snippet in the README). sshd runs this for every
# session under that key regardless of what the client asked to run, and
# puts whatever the client actually requested into $SSH_ORIGINAL_COMMAND.
#
# This script does almost nothing on purpose: parse that string, do a cheap
# sanity check, and hand off to the real, independently-validating,
# root-owned executor via sudo. It never touches the git checkout, the
# compose file, or any env file content itself — see
# mediaclipmakarr-deploy-executor.sh for where the actual work (and the
# real allowlist enforcement) happens.
set -euo pipefail

LOG_FILE="/var/log/mediaclipmakarr-deploy/deploy.log"

ALLOWED_BRANCHES=("main" "feature/make-clip-moc-update")
ALLOWED_ENV_FILES=("prod.env" "staging.env")
BRANCH_SYNTAX='^[A-Za-z0-9_][A-Za-z0-9_./-]*$'
ENVFILE_SYNTAX='^[A-Za-z0-9_][A-Za-z0-9_.-]*\.env$'

# `set -f` disables glob expansion before the split below — otherwise a
# crafted $SSH_ORIGINAL_COMMAND containing a metacharacter like `*` would
# expand against files in this script's CWD instead of being treated as a
# literal value. Word-splitting alone (no other shell syntax is
# interpreted here — no `;`, `&&`, backticks, or `$()` gets executed by
# `set --`) is sufficient since none of our values ever contain spaces.
set -f
# shellcheck disable=SC2086
set -- ${SSH_ORIGINAL_COMMAND:-}
set +f

if [[ "$#" -ne 4 || "$2" != "deploy" ]]; then
  echo "usage (via SSH_ORIGINAL_COMMAND): <script-path> deploy <branch> <env-file>" >&2
  exit 64
fi

branch="$3"
envfile="$4"

is_allowed() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

# This allowlist check is defense-in-depth, not the sole enforcement — the
# root-owned executor re-checks both values independently against its own
# hardcoded copy of this list before it does anything.
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

{
  echo "=== $(date -u +%FT%TZ) deploy requested branch=$branch env=$envfile ==="
  sudo /opt/deploy/mediaclipmakarr-deploy-executor.sh "$branch" "$envfile"
  echo "=== deploy finished with exit code $? ==="
} | tee -a "$LOG_FILE"
