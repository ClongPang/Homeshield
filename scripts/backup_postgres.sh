#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?Set DATABASE_URL before running the backup}"
backup_dir="${BACKUP_DIR:-./backups}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$backup_dir/homeshield-${stamp}.dump"

if command -v pg_dump >/dev/null 2>&1; then
  pg_dump --format=custom --no-owner --no-acl --file="$archive" "$DATABASE_URL"
else
  docker compose exec -T postgres pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL" > "$archive"
fi
test -s "$archive"
find "$backup_dir" -type f -name 'homeshield-*.dump' -mtime "+$retention_days" -delete
printf 'backup=%s retention_days=%s\n' "$archive" "$retention_days"
