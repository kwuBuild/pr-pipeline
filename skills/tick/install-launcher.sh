#!/usr/bin/env bash
# Setup's idempotent installation primitive. No plugin/config/target writes.
set -euo pipefail

if [[ "${HOME:-}" != /* ]]; then
  echo 'FAIL: launcher setup requires HOME to be an absolute directory.' >&2
  exit 3
fi
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
destination="$HOME/.claude/pr-pipeline/bin"
mkdir -p "$destination"
staged="$(mktemp "$destination/.pr-pipeline.XXXXXX")"
trap 'rm -f "$staged"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp "$source_dir/pr-pipeline.sh" "$staged"
chmod 755 "$staged"
mv -f "$staged" "$destination/pr-pipeline"
printf 'Installed %s\n' "$destination/pr-pipeline"
