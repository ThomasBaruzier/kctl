#!/bin/sh
set -eu
CDPATH= cd -- "$(dirname -- "$0")"
exec uv run kctl "$@"
