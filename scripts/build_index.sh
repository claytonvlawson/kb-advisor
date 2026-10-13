#!/usr/bin/env bash
# build_index.sh -- rebuild a client's BM25 index from its vault.
# Usage: ./scripts/build_index.sh <client-name>
set -euo pipefail
NAME="${1:?usage: build_index.sh <client-name>}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HERE/clients/$NAME"
[[ -d "$DIR/vault" ]] || { echo "ERROR: no vault at $DIR/vault" >&2; exit 1; }
mkdir -p "$DIR/index"
cd "$HERE"
python3 -c 'import sys; sys.path.insert(0, "kb"); import retrieval; retrieval.build_index(sys.argv[1] + "/vault", sys.argv[1] + "/index/bm25.pkl")' "$DIR"
echo "Index built: $DIR/index/bm25.pkl"
