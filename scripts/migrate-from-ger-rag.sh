#!/usr/bin/env bash
# Migrate existing GER-RAG data to the new GaOTTT layout.
#
# Strategy: COPY (not move) so the legacy data remains as a backup.
# After GaOTTT is stable for you, delete ~/.local/share/ger-rag/ manually.
#
# Usage:  scripts/migrate-from-ger-rag.sh
#
# Honors the same env vars as the runtime:
#   GAOTTT_DATA_DIR (override new location)
#   GER_RAG_DATA_DIR (override legacy location, also accepted by config.py)

set -euo pipefail

SRC="${GER_RAG_DATA_DIR:-${HOME}/.local/share/ger-rag}"
DST="${GAOTTT_DATA_DIR:-${HOME}/.local/share/gaottt}"

if [[ ! -d "$SRC" ]]; then
    echo "No legacy GER-RAG data at $SRC — nothing to migrate."
    exit 0
fi

if [[ ! -f "$SRC/ger_rag.db" ]]; then
    echo "Legacy directory $SRC exists but has no ger_rag.db. Skipping."
    exit 0
fi

mkdir -p "$DST"

# Refuse to overwrite an existing GaOTTT DB silently.
if [[ -f "$DST/gaottt.db" ]]; then
    echo "ERROR: $DST/gaottt.db already exists."
    echo "Refusing to overwrite. Move or delete it first, or set GAOTTT_DATA_DIR."
    exit 1
fi

echo "Copying GER-RAG data:"
echo "  src: $SRC"
echo "  dst: $DST"

# cp -p preserves timestamps/perms.
cp -p "$SRC/ger_rag.db"        "$DST/gaottt.db"
[[ -f "$SRC/ger_rag.faiss" ]]     && cp -p "$SRC/ger_rag.faiss"     "$DST/gaottt.faiss"
[[ -f "$SRC/ger_rag.faiss.ids" ]] && cp -p "$SRC/ger_rag.faiss.ids" "$DST/gaottt.faiss.ids"

# WAL companion files (live SQLite). Copy if present.
[[ -f "$SRC/ger_rag.db-wal" ]] && cp -p "$SRC/ger_rag.db-wal" "$DST/gaottt.db-wal"
[[ -f "$SRC/ger_rag.db-shm" ]] && cp -p "$SRC/ger_rag.db-shm" "$DST/gaottt.db-shm"

# Mark the legacy dir so the runtime warning can stop appearing if you
# move on without deleting.
date -u +%Y-%m-%dT%H:%M:%SZ > "$SRC/.migrated-to-gaottt-at"

echo
echo "Migration complete."
echo "Legacy data preserved at: $SRC"
echo "  → safe to delete after you confirm GaOTTT works (e.g. \`rm -rf $SRC\`)."
