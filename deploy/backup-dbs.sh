#!/usr/bin/env bash
# Backs up all data/*.db files using SQLite's online backup (safe during writes).
# Keeps the last 7 daily snapshots per database.
# Intended to run daily via cron: 0 3 * * * berries /opt/berries/deploy/backup-dbs.sh

set -euo pipefail

BERRIES_DIR="/opt/berries"
BACKUP_DIR="$BERRIES_DIR/data/backups"
KEEP=7

mkdir -p "$BACKUP_DIR"

DATE=$(date +%Y-%m-%d)

for db in "$BERRIES_DIR"/data/*.db; do
    name=$(basename "$db" .db)
    dest="$BACKUP_DIR/${name}_${DATE}.db"
    sqlite3 "$db" ".backup '$dest'"
    echo "Backed up $name → $dest"

    # Prune old backups for this db, keeping the most recent $KEEP
    ls -t "$BACKUP_DIR/${name}_"*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
done
