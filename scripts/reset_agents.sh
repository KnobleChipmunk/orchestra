#!/usr/bin/env bash
# Reset the orchestra test/run state: kill the tmux session, clear agent
# state dirs, reset agent worktrees.
#
# HUMAN/BOOTSTRAP TOOL ONLY — agents must never invoke this (destructive-op
# rule). Refuses to touch dirty worktrees unless --force is given.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
AGENTS_DIR="$ORCHESTRA_AGENTS_DIR"
SESSION="$ORCHESTRA_TMUX_SESSION"
FORCE="${1:-}"

tmux kill-session -t "$SESSION" 2>/dev/null || true

for d in "$AGENTS_DIR"/*/; do
    name="$(basename "$d")"
    if [ -d "$d/.git" ] || [ -f "$d/.git" ]; then
        # Worktree: refuse if dirty unless --force.
        if [ -n "$(git -C "$d" status --porcelain)" ] && [ "$FORCE" != "--force" ]; then
            echo "REFUSING: worktree $name is dirty (rerun with --force to discard)" >&2
            exit 1
        fi
        git -C "$d" reset --hard -q
        git -C "$d" clean -fdq
        echo "reset worktree: $name"
    else
        rm -rf "$d"
        echo "cleared agent state: $name"
    fi
done
echo "RESET CLEAN"
