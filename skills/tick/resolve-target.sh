#!/usr/bin/env bash
# Shared by tick.py and status.sh; target resolution needs no interpreter or gh.
set -euo pipefail

export LC_ALL=C

fail() { printf 'target: %s\n' "$*" >&2; exit 2; }

valid_repo() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$ ]] &&
    [[ "${1#*/}" != . && "${1#*/}" != .. ]]
}

valid_remote() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$ ]] &&
    [[ "$1" != *..* && "$1" != *. && "$1" != *.lock ]]
}

same_repo() {
  [ "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')" ]
}

url_repo() {
  local path
  case "$1" in
    git@github.com:*) path=${1#git@github.com:} ;;
    https://github.com/*) path=${1#https://github.com/} ;;
    ssh://git@github.com/*) path=${1#ssh://git@github.com/} ;;
    ssh://git@github.com:22/*) path=${1#ssh://git@github.com:22/} ;;
    git://github.com/*) path=${1#git://github.com/} ;;
    *) return 1 ;;
  esac
  path=${path%.git}
  valid_repo "$path" || return 1
  printf '%s' "$path"
}

# Check Git's effective URLs in the caller's cwd, including conditional includes,
# worktree config and inherited GIT_CONFIG_* overrides. Never re-anchor here.
check_remote() {
  local expected=$1 remote=$2 urls repo url
  valid_repo "$expected" || fail 'expected target must be owner/name'
  valid_remote "$remote" || fail "unsafe remote name: $remote"
  urls=$(git remote get-url --all "$remote") || fail "cannot read remote $remote"
  repo=$(url_repo "$urls") || fail "unsupported fetch destination for $remote"
  same_repo "$repo" "$expected" || fail "fetch destination disagrees with target $expected for $remote"
  urls=$(git remote get-url --push --all "$remote") || fail "cannot read push destinations for $remote"
  while IFS= read -r url; do
    repo=$(url_repo "$url") || fail "unsupported push destination for $remote"
    same_repo "$repo" "$expected" || fail "push destination disagrees with target $expected for $remote"
  done <<< "$urls"
}

if [ "$#" -eq 3 ] && [ "$1" = --check-remote ]; then
  check_remote "$2" "$3"
  exit 0
fi
[ "$#" -eq 0 ] || fail 'usage: resolve-target.sh [--check-remote owner/name remote]'

# Match the controller's marker-space anchor, including when invoked from a
# linked worktree whose worktree-specific Git configuration rewrites URLs.
common_dir=$(git rev-parse --git-common-dir) || fail 'run inside a Git repository'
cd "$(dirname "$common_dir")" || fail 'cannot enter the main checkout'
remotes=$(git remote) || fail 'cannot read Git remotes; run inside a Git repository'
[ -n "$remotes" ] || fail 'no remote; configure a GitHub remote for this repository'
candidates=$(printf '%s\n' "$remotes" | paste -sd ',' - | sed 's/,/, /g')
override=${PIPELINE_REPO:-}
if [ -n "$override" ]; then
  valid_repo "$override" || fail 'PIPELINE_REPO must be owner/name'
fi

selected_remote=
selected_repo=
matches=0
while IFS= read -r remote; do
  # This name reaches exact engine permissions and shell prompt text.
  valid_remote "$remote" ||
    fail "unsafe remote name: $remote; candidates: $candidates"
  urls=$(git remote get-url --all "$remote") || fail "cannot read remote $remote"
  repo=$(url_repo "$urls") || continue
  if [ -n "$override" ] && ! same_repo "$repo" "$override"; then
    continue
  fi
  selected_remote=$remote
  selected_repo=${override:-$repo}
  matches=$((matches + 1))
done <<< "$remotes"

[ "$matches" -eq 1 ] || fail "expected one matching GitHub remote (PIPELINE_REPO=${override:-unset}); candidates: $candidates"
# With no override, multiple distinct remotes cannot silently select a target.
if [ -z "$override" ] && [[ "$remotes" == *$'\n'* ]]; then
  fail "multiple remotes require PIPELINE_REPO; candidates: $candidates"
fi

check_remote "$selected_repo" "$selected_remote"
printf '%s\n%s\n' "$selected_repo" "$selected_remote"
