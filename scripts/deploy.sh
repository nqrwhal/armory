#!/usr/bin/env bash
# Deploy the current repo to the watcher-host watcher and restart it.
# Server state (.env, armory.state.json, armory.db, venv) is never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${1:-watcher-host}"
REMOTE_DIR="${2:-\$HOME/armory}"

echo "==> rsync code -> ${HOST}:${REMOTE_DIR}"
rsync -av --delete \
  --exclude .venv --exclude .env \
  --exclude armory.state.json --exclude "armory.state.json.*" \
  --exclude armory.db --exclude "armory.db-*" \
  --exclude gafshub.cookies.json \
  --exclude .git --exclude __pycache__ --exclude "*.pyc" \
  --exclude .zcode --exclude .pytest_cache \
  ./ "${HOST}:${REMOTE_DIR}/"

echo "==> install (editable) + restart service"
ssh "$HOST" "cd ${REMOTE_DIR} && .venv/bin/python -m pip install -q -e . && systemctl --user restart armory && sleep 3 && systemctl --user is-active armory"

echo "==> first cycle output"
ssh "$HOST" "journalctl --user -u armory -n 5 --no-pager"
echo "deployed."
