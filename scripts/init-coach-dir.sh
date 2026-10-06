#!/usr/bin/env bash
# Prepare a coach folder for the coach file tools: create it, make it a git repository,
# add a .gitignore and commit whatever .md files are already there.
# Safe to run again (e.g. after copying files in with rsync): it only commits new changes.
#
# Usage: scripts/init-coach-dir.sh <directory>
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <directory>" >&2
  exit 1
fi

dir="$1"
mkdir -p "$dir"
chmod 700 "$dir"
cd "$dir"

if [ ! -d .git ]; then
  git init -q
  echo "Initialised git repository in $(pwd)"
fi

if [ ! -f .gitignore ]; then
  cat > .gitignore <<'GITIGNORE'
# Only Markdown notes are tracked.
*
!*/
!*.md
!.gitignore
# Lock and temporary files written by the MCP server.
.coach.lock
.*.tmp
GITIGNORE
fi

git add -A
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git -c user.name="Claude Coach" -c user.email="coach@tetsuo.local" commit -qm "Import coach folder"
  echo "Committed: $(git log -1 --format='%h %s')"
fi
git ls-files
