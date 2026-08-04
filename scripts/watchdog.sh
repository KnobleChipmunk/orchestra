#!/usr/bin/env bash
set -euo pipefail

# Long-running blackboard watchdog: catches stalled workers and unread
# reports that LEAD's own turn-end inbox check might miss between turns.
#
# Run this in a **tmux window named `watchdog`** (NOT cron). It needs a
# persistent process that sleeps between cycles and nudges LEAD's live tmux
# pane; cron's one-shot, non-interactive invocations don't fit that shape.
#
# Usage:
#   scripts/watchdog.sh          # run forever: check, sleep 60s, repeat
#   scripts/watchdog.sh --once   # run exactly one check cycle and exit
#
# Env:
#   ORCHESTRA_AGENTS_DIR  blackboard root (default /home/user/agents)
#   ORCHESTRA_NUDGE_CMD   command to invoke for nudges, called as
#                         "$ORCHESTRA_NUDGE_CMD lead <reason>"
#                         (default scripts/agent_msg.py)

AGENTS_DIR="${ORCHESTRA_AGENTS_DIR:-/home/user/agents}"
NUDGE_CMD="${ORCHESTRA_NUDGE_CMD:-scripts/agent_msg.py}"
STALE_WORKING_S=600
STALE_REPORT_S=300
CYCLE_INTERVAL_S=60

declare -A WARNED

# Extract a top-level string field from a status.json without depending on
# jq. Copied from scripts/dashboard.sh's helper (not sourced: that script
# prints a table, this one only needs the parsing logic).
json_field() {
    local file="$1" key="$2"
    grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$file" 2>/dev/null \
        | head -1 \
        | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/'
}

# Seconds elapsed between ISO-8601 timestamp $1 and epoch $2; 0 if
# unparseable or in the future. Same date-diff pattern as dashboard.sh.
age_seconds() {
    local ts="$1" now_epoch="$2" ts_epoch diff
    ts_epoch="$(date -d "$ts" +%s 2>/dev/null || echo "$now_epoch")"
    diff=$(( now_epoch - ts_epoch ))
    (( diff < 0 )) && diff=0
    printf '%d' "$diff"
}

# Leading `[<iso8601>]` of the most recent DONE:/BLOCKED: line from agent
# $1 in the unprocessed tail of lead's inbox (i.e. after the last
# "processed-through" marker, per the inbox-hygiene convention in
# docs/LEAD.md / orchestra.inbox). Empty if none.
last_unprocessed_report_ts() {
    local name="$1" lead_inbox="$2" marker_line tail_content
    [[ -f "$lead_inbox" ]] || return 0

    marker_line="$(grep -n '^<!-- processed-through:' "$lead_inbox" | tail -1 | cut -d: -f1)" || true

    if [[ -n "$marker_line" ]]; then
        tail_content="$(tail -n "+$((marker_line + 1))" "$lead_inbox")"
    else
        tail_content="$(cat "$lead_inbox")"
    fi

    printf '%s\n' "$tail_content" \
        | grep -E "^\[[^]]+\] ${name^^}: (DONE|BLOCKED):" \
        | tail -1 \
        | sed -E 's/^\[([^]]+)\].*/\1/' || true
    return 0
}

nudge() {
    $NUDGE_CMD lead "$1"
}

# Nudge at most once per (agent, condition) pair per run of this process.
warn_once() {
    local name="$1" cond="$2" reason="$3" key="${1}:${2}"
    [[ -n "${WARNED[$key]:-}" ]] && return 0
    WARNED[$key]=1
    nudge "$reason"
}

check_agent() {
    local dir="$1" now_epoch="$2" lead_inbox="$3"
    local status_file="${dir}status.json"
    [[ -f "$status_file" ]] || return 0

    local name state updated
    name="$(basename "$dir")"
    state="$(json_field "$status_file" state)"
    updated="$(json_field "$status_file" updated)"

    if [[ "$state" == "working" && -n "$updated" ]]; then
        local age
        age="$(age_seconds "$updated" "$now_epoch")"
        if (( age > STALE_WORKING_S )); then
            warn_once "$name" "stale" \
                "$name has been 'working' with no status update for ${age}s (stalled?)."
        fi
    fi

    if [[ "$state" == "done" || "$state" == "blocked" ]]; then
        local report_ts
        report_ts="$(last_unprocessed_report_ts "$name" "$lead_inbox")"
        if [[ -n "$report_ts" ]]; then
            local age
            age="$(age_seconds "$report_ts" "$now_epoch")"
            if (( age > STALE_REPORT_S )); then
                warn_once "$name" "unread" \
                    "$name's $state report has sat unread in your inbox for ${age}s."
            fi
        fi
    fi
}

run_cycle() {
    [[ -d "$AGENTS_DIR" ]] || return 0
    local now_epoch lead_inbox dir
    now_epoch="$(date +%s)"
    lead_inbox="$AGENTS_DIR/lead/inbox.md"

    shopt -s nullglob
    for dir in "$AGENTS_DIR"/*/; do
        check_agent "$dir" "$now_epoch" "$lead_inbox"
    done
}

main() {
    if [[ "${1:-}" == "--once" ]]; then
        run_cycle
        return 0
    fi

    while true; do
        run_cycle
        sleep "$CYCLE_INTERVAL_S"
    done
}

main "$@"
