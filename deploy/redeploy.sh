#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/redeploy.sh, owned by
# root:root, mode 755 — the shared homelab-deploy account can read and
# execute this file but cannot write to it. If that account could edit
# this script, whoever controls it could rewrite what the forced SSH
# command actually does, which would defeat the entire point of forcing it.
#
# Wired up as the forced command for homelab-deploy's SSH key (see the
# authorized_keys snippet in the README) — the same key and account for
# every configured target. sshd runs this for every session under that key
# regardless of what the client asked to run, and puts whatever the client
# actually requested into $SSH_ORIGINAL_COMMAND.
#
# This script does almost nothing on purpose: parse that string, do a
# cheap syntax check, and hand off to the real, independently-validating,
# root-owned executor via sudo. It does NOT keep its own copy of which
# targets/branches/env-files are actually allowed — that table lives once,
# in deploy-executor.sh, and this script never touches a git checkout,
# compose file, or env file itself.
set -euo pipefail

LOG_FILE="/var/log/homelab-deploy/deploy.log"

TARGET_SYNTAX='^[A-Za-z0-9_-]+$'
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

if [[ "$#" -ne 5 || "$2" != "deploy" ]]; then
  echo "usage (via SSH_ORIGINAL_COMMAND): <script-path> deploy <target> <branch> <env-file>" >&2
  exit 64
fi

target="$3"
branch="$4"
envfile="$5"

# Syntax sanity only. Whether this target/branch/env-file combination is
# actually allowed is decided by deploy-executor.sh's own table, checked
# again independently after sudo hands off to it below.
if ! [[ "$target" =~ $TARGET_SYNTAX ]]; then
  echo "malformed target: $target" >&2
  exit 65
fi
if ! [[ "$branch" =~ $BRANCH_SYNTAX ]]; then
  echo "malformed branch: $branch" >&2
  exit 66
fi
if ! [[ "$envfile" =~ $ENVFILE_SYNTAX ]]; then
  echo "malformed env file: $envfile" >&2
  exit 67
fi

{
  echo "=== $(date -u +%FT%TZ) deploy requested target=$target branch=$branch env=$envfile ==="
  sudo /opt/deploy/deploy-executor.sh "$target" "$branch" "$envfile"
  echo "=== deploy finished with exit code $? ==="
} | tee -a "$LOG_FILE"
