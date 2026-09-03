#!/usr/bin/env bash
#
# Installed on the homelab host at /opt/deploy/docker-compose-up-root.sh,
# owned by root:root, mode 750 — NOT writable by mediaclipmakarr-deploy.
# This is the ONLY thing that account can ever run as root (see
# sudoers.d/mediaclipmakarr-deploy), and it takes no arguments at all, so
# there's nothing for a caller to inject even via sudo's argument passing.
#
# Keeping this script tiny and argument-free is the point: the sudoers
# rule below matches this exact bare path, so the entire "what can this
# account do as root" surface is these three lines.
set -euo pipefail

cd /opt/mediaclipmakarr
exec docker compose up -d --force-recreate --build
