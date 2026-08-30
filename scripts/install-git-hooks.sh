#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
git -C "$repo_root" config core.hooksPath .githooks
printf 'Configured Git hooks for %s\n' "$repo_root"
printf '  core.hooksPath=%s\n' "$(git -C "$repo_root" config --get core.hooksPath)"
