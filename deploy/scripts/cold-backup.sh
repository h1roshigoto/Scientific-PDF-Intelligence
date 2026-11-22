#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
    printf 'Usage: %s /absolute/path/compose.env backup-name\n' "$0" >&2
    exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
compose=(docker compose --env-file "$1" -f compose.yaml -f compose.production.yaml)
"${compose[@]}" config --quiet
trap '"${compose[@]}" up -d --wait api worker proxy' EXIT
"${compose[@]}" stop proxy
"${compose[@]}" stop worker api
"${compose[@]}" run --rm --no-deps backup create "$2"
