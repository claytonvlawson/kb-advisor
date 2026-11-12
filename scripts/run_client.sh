#!/usr/bin/env bash
# run_client.sh -- run one client's advisor server locally.
# Usage: ./scripts/run_client.sh <client-name> [port]
set -euo pipefail
NAME="${1:?usage: run_client.sh <client-name> [port]}"
PORT="${2:-8001}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HERE/clients/$NAME"
[[ -f "$DIR/index/bm25.pkl" ]] || { echo "ERROR: no index at $DIR/index/bm25.pkl -- run ./scripts/build_index.sh $NAME" >&2; exit 1; }
cd "$HERE"
exec env ADVISOR_CLIENT="$DIR" ADVISOR_PORT="$PORT" \
  OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
  python3 kb/kb_server.py
