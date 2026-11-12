#!/usr/bin/env bash
# new_client.sh -- scaffold a new client knowledge base in 3 commands.
#
# Usage:
#   ./scripts/new_client.sh myclient "You are an advisor for ..." [port]
#
# Creates clients/myclient/ with a system prompt, empty crisis config
# (disabled), an empty vault, and prints the exact ingest + build + run
# commands. The vault is markdown-only; ingest PDFs with ingest/ingest_pdfs.py.
set -euo pipefail

NAME="${1:?usage: new_client.sh <name> <system-prompt-first-line> [port]}"
PROMPT="${2:?pass the first line of the system prompt}"
PORT="${3:-8001}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HERE/clients/$NAME"

if [[ -e "$DIR" ]]; then
  echo "ERROR: $DIR already exists" >&2
  exit 1
fi

mkdir -p "$DIR/vault/Notes" "$DIR/index"

cat > "$DIR/system_prompt.md" <<EOF
$PROMPT

Answer ONLY from the provided context. If the context does not contain the
answer, say so plainly rather than guessing. Every substantive claim should
trace to the context you were given.
EOF

cat > "$DIR/crisis.json" <<'EOF'
{
  "patterns": [],
  "inject_source": "",
  "system_addendum": ""
}
EOF
# empty patterns = crisis path disabled; delete crisis.json when you add real
# patterns, or keep this file and fill it in.

cat > "$DIR/README.md" <<EOF
# Client: $NAME

- system_prompt.md -- persona and guardrails (edit this first)
- crisis.json -- safety-trigger regexes + the vault note to inject; empty patterns = disabled
- vault/ -- markdown knowledge base (ingest PDFs, then rebuild the index)
- index/ -- bm25.pkl lives here (gitignored)

Rebuild after any vault change:
  cd $HERE && python3 -c 'import sys; sys.path.insert(0, "kb"); import retrieval; retrieval.build_index("$DIR/vault", "$DIR/index/bm25.pkl")'

Run (port $PORT):
  cd $HERE && ADVISOR_CLIENT=$DIR ADVISOR_PORT=$PORT python3 kb/kb_server.py
EOF

echo "Created $DIR"
echo
echo "Next steps:"
echo "  1. Edit $DIR/system_prompt.md (tone, terminology, referral rules)"
echo "  2. Ingest PDFs:   python3 $HERE/ingest/ingest_pdfs.py <pdfs> -o $DIR/vault/Notes"
echo "  3. Build index:    python3 $HERE/scripts/build_index.py $NAME"
echo "  4. Run:            ./scripts/run_client.sh $NAME $PORT"
