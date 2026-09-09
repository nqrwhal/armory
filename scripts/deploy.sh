#!/usr/bin/env bash
# Update a remote git-clone deployment of armory and restart its watcher.
# Usage: scripts/deploy.sh <ssh-host> [remote-dir]
# The remote keeps its own .env / armory.db / armory.state.json (gitignored).
set -euo pipefail

HOST="${1:?usage: deploy.sh <ssh-host> [remote-dir]}"
REMOTE_DIR="${2:-\$HOME/armory}"

ssh "$HOST" "cd ${REMOTE_DIR} && git pull --ff-only && systemctl --user restart armory && sleep 3 && systemctl --user is-active armory && journalctl --user -u armory -n 5 --no-pager"
