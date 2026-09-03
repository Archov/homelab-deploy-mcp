#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/deploy-executor.sh, owned by
# root:root, mode 700 — no account, regardless of homelab-deploy group
# membership, has any direct read/write/execute access to this file. It's
# only ever reached through the sudo rule in sudoers.d/homelab-deploy,
# which grants that to the whole group with any arguments and relies
# entirely on THIS script to validate them.
#
# This is the ONE place all privileged work happens, for EVERY configured
# target and EVERY agent's account: git operations against that target's
# checkout, and the docker compose build/up. No homelab-deploy-group
# account owns any of the directories below or can write to any of them,
# so even a full compromise of one agent's account — or of redeploy.sh,
# the forced-command script it invokes this through — cannot plant a file
# in a build context, read an env file's contents, or touch one target's
# files by asking for another.
#
# No eval, no bash -c, no string-built shell commands: every external
# command below is invoked with its arguments as separate argv entries, not
# interpolated into a command string a shell re-parses.
#
# Each target's fetch/reset/clean/env/compose sequence is serialized with a
# non-blocking flock on a per-target lock file, so two concurrent deploys
# of the same target can't interleave their git or docker operations —
# see the comment further down, right before it's acquired.
set -euo pipefail

# --- per-target configuration (host-side, authoritative) -------------------
#
# The four tables below are the real security boundary for
# target/branch/env-file/account selection. The MCP request only ever
# supplies a target NAME plus a branch and env-file NAME to look up here —
# never a path, never a docker argument. config.yaml has its own copy of
# the branch/env-file lists for fast client-side rejection, but THIS data
# is what actually gets enforced; keep them in sync by hand (or with the
# add-target wizard, which writes both from one place).
#
# Every target lives under a fixed directory layout at
# /opt/targets/<name>/:
#   repo/               git checkout (build context)
#   docker-compose.yml  host-owned compose file
#   envfiles/*.env       pre-approved candidate env files
#   active.env           written here by this script before each deploy
#
# ALLOWED_BRANCHES entries may be exact branch names or shell-glob
# patterns (e.g. "codex/*" allows any branch under that prefix) — see
# branch_matches_pattern below. ALLOWED_ENV_FILES stays exact-match only;
# there's no legitimate reason for an env-file name to need a wildcard,
# and env files are the more sensitive of the two (they're secrets, not
# just a source-code selector). ACCOUNT_BRANCH_PATTERNS is optional,
# per-account narrowing on top of ALLOWED_BRANCHES, keyed by the
# homelab-deploy-<agent> account name — see the assignments' own comments
# in targets.conf for the full explanation of each.
#
# The actual data lives in a SEPARATE file, /opt/deploy/targets.conf,
# sourced below rather than declared inline here — see
# targets.conf.example for the format and comments, and the README's
# "Adding a target" section (or `homelab-deploy-add-target`) for how to
# generate and install it. Splitting it out means this script — the
# reviewed, security-critical logic — never has to change just because a
# target was added; only the data file does.
#
# IMPORTANT: targets.conf is `source`d, not parsed as inert data — it runs
# as bash, as root, exactly like this script. It needs the exact same
# treatment: root-owned, mode 600, never writable by anything in the
# homelab-deploy group, installed only by a human (or the wizard's printed
# instructions) via the same install pattern used for this file. A wizard
# generating it nicely does not relax that requirement at all.
declare -A TARGET_DIR=()
declare -A ALLOWED_BRANCHES=()
declare -A ALLOWED_ENV_FILES=()
declare -A ACCOUNT_BRANCH_PATTERNS=()

TARGETS_CONF="/opt/deploy/targets.conf"
if [[ ! -f "$TARGETS_CONF" ]]; then
  echo "targets.conf not found at $TARGETS_CONF — see targets.conf.example" >&2
  exit 71
fi
# shellcheck source=targets.conf.example
source "$TARGETS_CONF"

# Cheap, early rejection of obviously-malformed input. This is NOT the real
# security boundary — the allowlist checks below are (exact-match for
# target/env-file, exact-match-or-glob for branch) — but it guarantees
# none of these values can start with `-` (so none can ever be
# mistaken for a flag by whatever it's passed to) and gives a clear error
# instead of an obscure git/docker failure on garbage input.
#
# TARGET_SYNTAX is lowercase-only (matching Docker Compose's own project-name
# character rules) on purpose, not just style: --project-name below is set
# to $target directly, and two target names differing only in case (e.g.
# "Foo" and "foo") would otherwise be free to collide on the same Compose
# project identity even though they live in separate directories.
# Bash associative array keys are already exact-match unique, so forbidding
# case variation here is what actually makes that uniqueness carry through
# to Compose.
TARGET_SYNTAX='^[a-z0-9][a-z0-9_-]*$'
BRANCH_SYNTAX='^[A-Za-z0-9_][A-Za-z0-9_./-]*$'
ENVFILE_SYNTAX='^[A-Za-z0-9_][A-Za-z0-9_.-]*\.env$'

usage() {
  echo "usage: $(basename "$0") <target> <branch> <env-file>" >&2
  exit 64
}

[[ "$#" -eq 3 ]] || usage

target="$1"
branch="$2"
envfile="$3"

# `set -f` around the splits below matters, not just style: $haystack is a
# space-separated string being split into words via unquoted expansion,
# and without noglob a word like "codex/*" would itself be expanded
# against real files/directories in the current working directory (if any
# happened to match) instead of staying the literal token these functions
# need to compare against. `[[ ... ]]`'s own pattern matching further down
# is unaffected by `set -f` either way — this is purely about the splitting
# step.
word_in_list() {
  local needle="$1" haystack="$2" item
  set -f
  for item in $haystack; do
    [[ "$item" == "$needle" ]] && { set +f; return 0; }
  done
  set +f
  return 1
}

# Same as word_in_list, but each entry in $haystack is matched as a
# shell-glob PATTERN against $needle rather than compared literally — this
# is what lets an ALLOWED_BRANCHES entry like "codex/*" allow any branch
# under that prefix. A plain entry with no glob metacharacters (e.g.
# "main") still only matches that exact branch, since `[[ str == pattern
# ]]` degrades to a literal comparison when the pattern has nothing to
# expand.
branch_matches_pattern() {
  local needle="$1" haystack="$2" pattern
  set -f
  for pattern in $haystack; do
    [[ "$needle" == $pattern ]] && { set +f; return 0; }
  done
  set +f
  return 1
}

# A target's mere presence as a key in TARGET_DIR *is* the target
# allowlist — there's no separate list to fall out of sync with it.
if ! [[ "$target" =~ $TARGET_SYNTAX ]] || [[ -z "${TARGET_DIR[$target]+set}" ]]; then
  echo "unknown target: $target" >&2
  exit 65
fi

if ! [[ "$branch" =~ $BRANCH_SYNTAX ]] || ! branch_matches_pattern "$branch" "${ALLOWED_BRANCHES[$target]-}"; then
  echo "branch not allowed for $target: $branch" >&2
  exit 66
fi

# $SUDO_USER is set by sudo itself, from the real invoking account's UID —
# not something the caller can override via its own environment, since
# sudo resets the environment by default (this repo's sudoers rule doesn't
# add remote_script or SUDO_USER to env_keep). Falls back to empty if this
# script is ever run directly as root rather than through sudo (e.g. local
# testing), which simply skips the restriction below, same as an account
# with no entry in the table. The `-n "$calling_account"` guard matters,
# not just style: with it empty, `${ACCOUNT_BRANCH_PATTERNS[$calling_account]}`
# is an empty array subscript, which bash treats as a "bad array
# subscript" error under `set -u` and aborts the whole script instead of
# skipping the restriction as intended — confirmed this crashes without
# the guard before adding it.
calling_account="${SUDO_USER:-}"
if [[ -n "$calling_account" ]] \
  && [[ -n "${ACCOUNT_BRANCH_PATTERNS[$calling_account]+set}" ]] \
  && ! branch_matches_pattern "$branch" "${ACCOUNT_BRANCH_PATTERNS[$calling_account]}"; then
  echo "branch '$branch' is not allowed for account '$calling_account' (target '$target' would otherwise allow it)" >&2
  exit 70
fi

if ! [[ "$envfile" =~ $ENVFILE_SYNTAX ]] || ! word_in_list "$envfile" "${ALLOWED_ENV_FILES[$target]-}"; then
  echo "env file not allowed for $target: $envfile" >&2
  exit 67
fi

base_dir="${TARGET_DIR[$target]}"
repo_dir="$base_dir/repo"
compose_file="$base_dir/docker-compose.yml"
env_dir="$base_dir/envfiles"
active_env="$base_dir/active.env"
lock_file="$base_dir/.deploy.lock"

# Serialize the whole fetch/reset/clean/env/compose sequence per target.
# Without this, two concurrent deploys of the SAME target (two agents, or
# one agent firing twice) could interleave: one's `reset --hard` landing
# between the other's `clean` and `install`, or its docker build running
# against the wrong branch or env file entirely. Non-blocking on purpose —
# fail the second request immediately with a clear reason rather than
# have it sit waiting and possibly hit the caller's own SSH timeout with
# no explanation. The lock is tied to this open file descriptor, which
# `exec` below inherits, so it stays held through the docker compose
# command too, not just up to that point in the script.
exec 200>"$lock_file"
if ! flock -n 200; then
  echo "a deploy for target '$target' is already in progress" >&2
  exit 69
fi

cd "$repo_dir"
git fetch origin
if ! git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
  echo "branch not found on origin for $target: $branch" >&2
  exit 68
fi
# Reset directly to the resolved remote-tracking ref rather than checking
# out a local branch — this never trusts, or depends on, whatever local
# branch state a previous run left behind.
git reset --hard "refs/remotes/origin/$branch"
# Untracked and ignored files survive `reset --hard` — without this, a file
# planted here by some other means (or left over from a previous run)
# would still be picked up by a Dockerfile's `COPY .` and end up in the
# built image, never having gone through the allowlist checks above at
# all. `-x` also removes gitignored files, since a Dockerfile's
# .dockerignore is independent of .gitignore.
git clean -fdx

install -m 600 -o root -g root "$env_dir/$envfile" "$active_env"

# --project-name is set explicitly, not left to Compose's own default (the
# --project-directory basename, lowercased): that default is what a
# same-after-lowercasing target name could collide on. Tying it directly to
# $target — already validated lowercase and unique as a TARGET_DIR key —
# makes project identity collision-free by construction instead of by
# convention.
exec docker compose -f "$compose_file" --project-directory "$base_dir" --project-name "$target" up -d --force-recreate --build
