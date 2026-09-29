#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: RESTORE_DATABASE_URL=postgresql://... $0 <backup.dump>" >&2
  exit 2
fi
: "${RESTORE_DATABASE_URL:?Set RESTORE_DATABASE_URL to a pre-created empty database}"
archive="$1"
[[ -r "$archive" ]] || { echo "backup is not readable: $archive" >&2; exit 2; }

empty_check="SELECT EXISTS (
  SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog','information_schema')
    AND c.relkind IN ('r','p','v','m','S','f')
)"
if command -v psql >/dev/null 2>&1; then
  has_objects="$(psql "$RESTORE_DATABASE_URL" -Atqc "$empty_check")"
else
  has_objects="$(docker compose exec -T postgres psql "$RESTORE_DATABASE_URL" -Atqc "$empty_check")"
fi
if [[ "$has_objects" == "t" ]]; then
  echo "restore target must be empty" >&2
  exit 1
fi
if command -v pg_restore >/dev/null 2>&1; then
  pg_restore --single-transaction --exit-on-error --no-owner --no-acl \
    --dbname="$RESTORE_DATABASE_URL" "$archive"
else
  docker compose exec -T postgres pg_restore --single-transaction --exit-on-error --no-owner --no-acl \
    --dbname="$RESTORE_DATABASE_URL" < "$archive"
fi
printf 'restored=%s\n' "$archive"
