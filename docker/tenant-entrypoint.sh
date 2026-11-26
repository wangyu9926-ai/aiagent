#!/bin/sh
# Fix ownership of the mounted Hermes home/uploads, then run as the tenant user.
set -e
if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes /opt/data /uploads 2>/dev/null || true
  chmod -R u+rwX /opt/data /uploads 2>/dev/null || true
  exec gosu hermes "$@"
fi
exec "$@"
