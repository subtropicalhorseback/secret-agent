#!/bin/bash
# backup.sh - Chip nightly backup
# Backs up coordinator, skills, config, DBs, and logs to Syncthing folder
# Keeps last 7 daily tarballs
#
# SOUL.md lives at agents/chip/SOUL.md, not the workspace root. It is listed
# explicitly even though agents/ above already covers it: the duplicate costs
# a few KB, and it means the backup fails loudly if the identity file ever
# goes missing again rather than quietly shipping an archive without it.
# (It was listed as $WORKSPACE/SOUL.md and failed every night 15-20 Nov 2026.)

WORKSPACE="${CHIP_WORKSPACE:-$(cd "$(dirname "$(readlink -f "$0")")" && pwd)}"
BACKUP_DIR="$WORKSPACE/syncthing/running_backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
ARCHIVE="$BACKUP_DIR/chip_backup_$TIMESTAMP.tar.gz"
LOG="$WORKSPACE/data/logs/backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup..." >> "$LOG"

tar -czf "$ARCHIVE" \
    "$WORKSPACE/coordinator.py" \
    "$WORKSPACE/skills/" \
    "$WORKSPACE/agents/" \
    "$WORKSPACE/config/" \
    "$WORKSPACE/facts.md" \
    "$WORKSPACE/agents/chip/SOUL.md" \
    "$WORKSPACE/data/memory/" \
    "$WORKSPACE/data/reminders.json" \
    2>> "$LOG"

if [ $? -eq 0 ]; then
    SIZE=$(du -sh "$ARCHIVE" | cut -f1)
    echo "[$(date)] Backup complete: $ARCHIVE ($SIZE)" >> "$LOG"
else
    echo "[$(date)] Backup FAILED" >> "$LOG"
    exit 1
fi

# Keep last 7 backups
cd "$BACKUP_DIR"
ls -t chip_backup_*.tar.gz | tail -n +8 | xargs -r rm --
KEPT=$(ls chip_backup_*.tar.gz 2>/dev/null | wc -l)
echo "[$(date)] Retained $KEPT backup(s)" >> "$LOG"
