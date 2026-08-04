#!/usr/bin/env bash
# Shared defaults for the operator shell scripts. Source it, don't run it:
#
#     source "$(dirname "$0")/config.sh"
#
# This is the bash half of src/orchestra/config.py. The two exist separately
# because these scripts must run with no venv on PATH (dashboard.sh is meant
# for `watch -n2`, and its test suite invokes it with PATH=/usr/bin:/bin), so
# shelling out to Python for a default is not an option.
#
# That makes the blackboard default the one value spelled twice in this
# repo. tests/test_config.py sources this file under a controlled $HOME and
# asserts it resolves to the same path orchestra.config does, so the pair
# fails loudly rather than drifting quietly. Change one, change the other.
#
# Assignments are `:-` so an operator's exported value always wins. Nothing
# here is exported: each script decides what its children see.
#
# Deliberately bash 3.2 compatible (no associative arrays) so this file at
# least loads on a stock macOS.

ORCHESTRA_AGENTS_DIR="${ORCHESTRA_AGENTS_DIR:-$HOME/orchestra-agents}"
ORCHESTRA_TMUX_SESSION="${ORCHESTRA_TMUX_SESSION:-orchestra}"
ORCHESTRA_NUDGE_CMD="${ORCHESTRA_NUDGE_CMD:-scripts/agent_msg.py}"
