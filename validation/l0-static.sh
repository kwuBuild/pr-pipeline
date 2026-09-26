#!/usr/bin/env bash
#
# L0 - static. Clone one commit into a fresh directory and run the whole suite
# there, so nothing this checkout holds (a venv, caches, untracked files, a
# developer's config) can take part.
#
#   LADDER_WORK=/abs/scratch validation/l0-static.sh [<ref>]
#
# The suite is what carries L0's two independence assertions, so CI runs them
# too: no shipped file names a developer home or this repository
# (test_no_shipped_file_binds_to_a_machine_or_this_repository), and setup plus
# a tick open no file inside the target tree except pipeline state
# (test_setup_and_tick_open_no_file_in_the_target_tree_but_pipeline_state).
#
# PYTHON names an interpreter >= 3.11 with pytest installed. The default is the
# checkout's .venv, then python3.13. Run with host process visibility: under a
# sandbox that blocks `ps`, the process start-time tests fail (see README).

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

ladder_require git
source_repo="$(git -C "$here" rev-parse --show-toplevel)" || ladder_die "not inside a git checkout"
ref="${1:-HEAD}"
sha="$(git -C "$source_repo" rev-parse --verify "$ref^{commit}")" || ladder_die "unknown ref: $ref"
work="$(ladder_work)"

py="${PYTHON:-}"
if [ -z "$py" ]; then
  if [ -x "$source_repo/.venv/bin/python" ]; then
    py="$source_repo/.venv/bin/python"
  else
    py="python3.13"
  fi
fi
"$py" -c 'import sys, pytest; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || ladder_die "$py is not Python >= 3.11 with pytest; set PYTHON"

clone="$(mktemp -d "$work/l0-clone.XXXXXX")"
ladder_note "cloning $sha into $clone"
git clone --quiet --no-checkout "$source_repo" "$clone" || ladder_die "clone failed"
git -C "$clone" checkout --quiet --detach "$sha" || ladder_die "checkout of $sha failed"
# A clone of a local path gets that path as `origin`. A clone from GitHub, as
# in CI, gets the GitHub URL, and test_gh_snapshots.py relies on it: it runs
# `gh` in the checkout, and gh resolves the repository from the remote.
if origin_url="$(git -C "$source_repo" remote get-url origin 2>/dev/null)"; then
  git -C "$clone" remote set-url origin "$origin_url"
fi

untracked="$(git -C "$clone" status --porcelain --ignored)"
ladder_assert "clone holds only tracked files" "$untracked" test -z "$untracked"

log="$clone.pytest.log"
ladder_note "running the suite with $py; log: $log"
(cd "$clone" && "$py" -m pytest skills/ -q -p no:cacheprovider) >"$log" 2>&1
status=$?
summary="$(tail -n 1 "$log")"
ladder_check "suite passes from a clean clone of $sha ($summary)" "$status" "see $log"

ladder_finish "L0"
