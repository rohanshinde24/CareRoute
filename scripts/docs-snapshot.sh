#!/usr/bin/env bash
# Snapshot the engineering documents into their local-only history.
#
# HANDOFF.md, SPEC.md and ROADMAP.md are deliberately excluded from the code
# repository (.gitignore allows only README.md), so that a push can never carry
# them. They still need version history, so they live in a second repository
# that shares this working tree and has no remote.
#
# Usage: scripts/docs-snapshot.sh "message"
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docs_git="$root/.docs-history.git"
message="${1:-Docs snapshot}"

if git --git-dir="$docs_git" remote -v | grep -q .; then
  echo "refusing to continue: the docs repository has a remote and must not have one" >&2
  exit 1
fi

# -f is required because the shared working tree's .gitignore excludes *.md.
git --git-dir="$docs_git" --work-tree="$root" add -f HANDOFF.md SPEC.md ROADMAP.md
if git --git-dir="$docs_git" --work-tree="$root" diff --cached --quiet; then
  echo "no documentation changes to snapshot"
  exit 0
fi
git --git-dir="$docs_git" --work-tree="$root" commit -q -m "$message"
git --git-dir="$docs_git" --work-tree="$root" log --oneline -1
