#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/redeploy.sh, owned by
# root:homelab-deploy, mode 750 — only accounts in the homelab-deploy
# GROUP can read and execute this file (see the README's group-based
# setup), and none of them can write to it. If any of them could edit
# this script, whoever controls that account could rewrite what the
# forced SSH command actually does, which would defeat the entire point
# of forcing it.
#
# Wired up as the forced command for every agent's SSH key (see the
# authorized_keys snippet in the README) — each agent gets its own Unix
# account and key for a distinct identity in the logs, but every one of
# those accounts is a member of the same homelab-deploy group and reaches
# this same script. sshd runs this for every session under any of those
# keys regardless of what the client asked to run, and puts whatever the
# client actually requested into $SSH_ORIGINAL_COMMAND.
#
# This script does almost nothing on purpose: parse that string, do a
# cheap syntax check, and hand off to the real, independently-validating,
# root-owned executor via sudo. It does NOT keep its own copy of which
# targets/branches/env-files are actually allowed — that table lives once,
# in deploy-executor.sh, and this script never touches a git checkout,
# compose file, or env file itself.
set -euo pipefail

# Several different accounts append to the same log file below. A file
# created with the default umask wouldn't be group-writable, so whichever
# account happens to create it first would lock the others out of it.
# `007` keeps owner/group read-write and denies "other" entirely, so any
# new file this script creates stays writable by the whole group (which,
# combined with the log directory's setgid bit — see the README — is what
# keeps it group-owned too) and inaccessible to anyone outside it.
umask 007

LOG_FILE="/var/log/homelab-deploy/deploy.log"

# TARGET_SYNTAX is lowercase-only, matching deploy-executor.sh's copy of
# this pattern — see that script's comment on why (Docker Compose project
# names are lowercase-only, and target names double as project names).
TARGET_SYNTAX='^[a-z0-9][a-z0-9_-]*$'
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

# A brace group's own exit status is that of its LAST command — if that
# were the trailing `echo` below (which always succeeds), the group would
# always report success regardless of whether `sudo` actually failed, and
# with `pipefail` only inspecting each pipeline STAGE's own final status
# (not commands buried inside one), a failed deploy would still make this
# whole script exit 0 to the SSH caller. Capturing `sudo`'s status
# immediately and `exit`-ing the group with it explicitly is what makes
# that status the group's own, so `pipefail` has a real failure to
# propagate. `2>&1` on the sudo call sends deploy-executor.sh's stderr
# through this same pipe too, so its actual error output lands in
# $LOG_FILE instead of only the generic "finished with exit code N" line.
{
  echo "=== $(date -u +%FT%TZ) deploy requested target=$target branch=$branch env=$envfile ==="
  sudo /opt/deploy/deploy-executor.sh "$target" "$branch" "$envfile" 2>&1
  status=$?
  echo "=== deploy finished with exit code $status ==="
  exit "$status"
} | tee -a "$LOG_FILE"
exit "${PIPESTATUS[0]}"
