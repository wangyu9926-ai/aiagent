#!/usr/bin/env bash
# Destructive restore of a backup made by deploy/backup.sh.
# Usage: ./deploy/restore.sh <archive.tar.gz> [--yes]
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <archive.tar.gz> [--yes]" >&2
  exit 2
fi

ARCHIVE="$1"
ASSUME_YES="${2:-}"
if [ ! -f "$ARCHIVE" ]; then
  echo "Archive not found: $ARCHIVE" >&2
  exit 2
fi
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/docker-compose.portal.yml"

if command -v cygpath >/dev/null 2>&1; then
  HOST_ROOT="$(cygpath -m "$ROOT")"
  HOST_ARCHIVE="$(cygpath -m "$ARCHIVE")"
else
  HOST_ROOT="$ROOT"
  HOST_ARCHIVE="$ARCHIVE"
fi

if [ "$ASSUME_YES" != "--yes" ]; then
  echo "This will REPLACE the portal DB, both tenant homes, and uploads."
  printf 'Type RESTORE to continue: '
  read -r answer
  [ "$answer" = "RESTORE" ] || { echo "Aborted."; exit 1; }
fi

for volume in hermes-portal-data hermes-visitor-data hermes-business-data; do
  docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
done

restore_services() {
  docker compose -f "$COMPOSE" start portal tenant-visitor tenant-business >/dev/null 2>&1 || true
}
trap restore_services EXIT

echo "Stopping portal and tenants..."
docker compose -f "$COMPOSE" stop portal tenant-visitor tenant-business >/dev/null

echo "Restoring volumes and uploads..."
docker run --rm --entrypoint sh \
  -v hermes-portal-data:/data \
  -v hermes-visitor-data:/visitor \
  -v hermes-business-data:/business \
  -v "$HOST_ROOT/portal/.e2e-data/uploads:/uploads" \
  -v "$HOST_ARCHIVE:/backup/archive.tar.gz:ro" \
  hermes-portal:local \
  -c 'for d in /data /visitor /business /uploads; do find "$d" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; done; tar xzf /backup/archive.tar.gz -C /'

echo "Restore complete. Services are starting."
