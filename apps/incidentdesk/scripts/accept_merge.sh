#!/usr/bin/env bash
# Accept a reviewed ticket branch into the current integration branch (docs/CI_GUARDRAILS.md).
#
#   scripts/accept_merge.sh <ticket-branch>
#
# Run it from the integration branch (for example fresh-start). It:
#   1. refuses if the working tree is dirty, or if you are on main/master;
#   2. runs `git merge --no-ff <branch>`; on a conflict it stops and leaves the merge for you (never auto-resolves);
#   3. runs scripts/check_all.sh against the integration branch as it was before the merge;
#   4. leaves the checked merge local for owner review; never pushes.
# It never pushes; publication belongs to the owner. If a check fails, nothing is pushed; the local merge commit
# stays so you can look, and `git reset --hard ORIG_HEAD` (your decision) undoes it.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "usage: scripts/accept_merge.sh <ticket-branch>" >&2
  exit 2
fi
ticket="$1"
integration="$(git symbolic-ref --short HEAD)"

case "$integration" in
  main | master) echo "refusing: current branch is $integration. Check out the integration branch first." >&2; exit 2 ;;
esac
if [ "$ticket" = "$integration" ] || [ "$ticket" = main ] || [ "$ticket" = master ]; then
  echo "refusing: $ticket is not a ticket branch to merge into $integration." >&2
  exit 2
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "refusing: the working tree is not clean. Commit or stash your changes first." >&2
  git status --short --untracked-files=no >&2
  exit 1
fi
git rev-parse --verify --quiet "$ticket^{commit}" > /dev/null || { echo "no such branch: $ticket" >&2; exit 2; }

before="$(git rev-parse HEAD)"

# Untracked files here that the ticket branch adds would block the merge with a vague git error
# (seen 2026-10-16: a lane wrote its result file into this checkout instead of its worktree).
collisions="$(git diff --name-only --diff-filter=A "$before...$ticket" | while IFS= read -r path; do
  [ -e "$path" ] && ! git ls-files --error-unmatch "$path" > /dev/null 2>&1 && echo "$path"
done || true)"
if [ -n "$collisions" ]; then
  echo "refusing: these untracked files here would be overwritten by $ticket. Compare each with the branch copy" >&2
  echo "(git show $ticket:<path>) and move it aside; nothing was merged:" >&2
  echo "$collisions" >&2
  exit 1
fi

echo "Merging $ticket into $integration (was $before)"
if ! git merge --no-ff "$ticket" -m "Merge branch '$ticket' into $integration"; then
  echo "MERGE CONFLICT: stopping. Resolve it yourself (or run 'git merge --abort'). Nothing was pushed." >&2
  exit 1
fi

if ! bash scripts/check_all.sh --base "$before"; then
  echo "A check failed. Nothing was pushed. The merge commit is local only." >&2
  exit 1
fi

echo "All checks passed. Merge remains local; owner publishes."

# Remove the ticket's worktree so its old status file can't be mistaken for a new one. Only when the worktree
# holds nothing but lane status files beyond the merged commit; anything else is left for you to look at.
wt_path="$(git worktree list --porcelain | awk -v b="refs/heads/$ticket" '/^worktree /{p=substr($0,10)} $0=="branch " b {print p}')"
if [ -n "$wt_path" ]; then
  leftovers="$(git -C "$wt_path" status --porcelain | grep -v '\.agent_bridge/lanes/[^/]*\.status$' || true)"
  if [ -z "$leftovers" ]; then
    git worktree remove --force "$wt_path" && echo "Removed worktree $wt_path."
  else
    echo "Kept worktree $wt_path: it has changes beyond status files:" >&2
    echo "$leftovers" >&2
  fi
fi
echo "accept_merge: done."
