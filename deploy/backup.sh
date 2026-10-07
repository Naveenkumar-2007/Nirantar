#!/bin/sh
# Nightly logical backups of every Nirantar database (custom format, compressed), kept BACKUP_KEEP_DAYS days.
# Copy /backups off the server too (e.g. rclone to object storage) — a backup on the same disk is not a backup.
set -e
KEEP="${BACKUP_KEEP_DAYS:-14}"
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  for db in nirantar nirantar_lake keycloak mlflow; do
    pg_dump -h postgres -U nirantar_owner -Fc -f "/backups/${db}-${stamp}.dump" "$db" \
      && echo "backup ok ${db} ${stamp}" || echo "backup FAILED ${db} ${stamp}"
  done
  find /backups -name '*.dump' -mtime +"$KEEP" -delete
  sleep 86400
done
