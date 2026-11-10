#!/usr/bin/env bash
# Backward-compatible entry point. Use the unified Ubuntu/Proot monitor installer.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$SCRIPT_DIR/start_monitoring.sh" "$@"
