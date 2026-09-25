#!/usr/bin/env bash
set -euo pipefail

echo "This archive is intended for a controlled production update."
echo "Do not overwrite .env, data/history.db, runtime/max_session, or the systemd unit/override."
echo "See UPDATE_SERVER_RU.md."
