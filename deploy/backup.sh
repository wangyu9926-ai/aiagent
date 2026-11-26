#!/usr/bin/env bash
# Consistent backup of the portal DB volume, both tenant homes, and uploads.
# The portal/tenants are stopped briefly so SQLite files are not copied mid-write.
# WARNING: the archive contains tenant .env/API keys and the portal encryption
# key. Keep it offline/encrypted; do not commit it (backups/ is gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/docker-compose.portal.yml"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$BACKUP_DIR"

if command -v cygpath >/dev/null 2>&1; then
  HOST_ROOT="$(cygpath -m "$ROOT")"
  HOST_BACKUP="$(cygpath -m "$BACKUP_DIR")"
else
  HOST_ROOT="$ROOT"
  HOST_BACKUP="$BACKUP_DIR"
fi

for volume in hermes-portal-data hermes-visitor-data hermes-business-data; do
  docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
done

restore_services() {
  docker compose -f "$COMPOSE" start portal tenant-visitor tenant-business >/dev/null 2>&1 || true
}
trap restore_services EXIT

echo "Stopping portal and tenants for a consistent snapshot..."
docker compose -f "$COMPOSE" stop portal tenant-visitor tenant-business >/dev/null

echo "Archiving volumes and uploads..."
docker run --rm --entrypoint sh \
  -v hermes-portal-data:/data:ro \
  -v hermes-visitor-data:/visitor:ro \
  -v hermes-business-data:/business:ro \
  -v "$HOST_ROOT/portal/.e2e-data/uploads:/uploads:ro" \
  -v "$HOST_BACKUP:/backup" \
  hermes-portal:local \
  -c "tar czf /backup/hermes-backup-$STAMP.tar.gz -C / data visitor business uploads"

ARCHIVE="$BACKUP_DIR/hermes-backup-$STAMP.tar.gz"
echo "Archive: $ARCHIVE"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ARCHIVE" | tee "$ARCHIVE.sha256"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$ARCHIVE" | tee "$ARCHIVE.sha256"
fi
echo "Backup complete."
