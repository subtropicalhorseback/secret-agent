#!/bin/bash
# backup.sh - Chip nightly backup
# Backs up coordinator, skills, config, DBs, and logs to Syncthing folder
# Keeps last 7 daily tarballs

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
    "$WORKSPACE/SOUL.md" \
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
