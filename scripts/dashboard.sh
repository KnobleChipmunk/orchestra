#!/usr/bin/env bash
set -euo pipefail

# One-screen overview of every agent on the Orchestra blackboard.
# Reads <name>/status.json + <name>/agent.log under $ORCHESTRA_AGENTS_DIR
# (default resolved in scripts/config.sh) and prints one row per agent:
# name, state, age of last update, last log line.

source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
AGENTS_DIR="$ORCHESTRA_AGENTS_DIR"

# Extract a top-level string field from a status.json without depending on jq.
json_field() {
    local file="$1" key="$2"
    grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$file" 2>/dev/null \
        | head -1 \
        | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/'
}

human_age() {
    local seconds="$1"
    if (( seconds < 60 )); then
        printf '%ds' "$seconds"
    elif (( seconds < 3600 )); then
        printf '%dm' $(( seconds / 60 ))
    elif (( seconds < 86400 )); then
        printf '%dh' $(( seconds / 3600 ))
    else
        printf '%dd' $(( seconds / 86400 ))
    fi
}

width="$(tput cols 2>/dev/null || true)"
[[ "$width" =~ ^[0-9]+$ ]] || width=120

printf '%-14s %-10s %-6s  %s\n' "NAME" "STATE" "AGE" "LAST LOG LINE"

if [[ -d "$AGENTS_DIR" ]]; then
    shopt -s nullglob
    now_epoch="$(date +%s)"
    for dir in "$AGENTS_DIR"/*/; do
        status_file="${dir}status.json"
        [[ -f "$status_file" ]] || continue

        name="$(basename "$dir")"

        state="$(json_field "$status_file" state)"
        [[ -n "$state" ]] || state="unknown"

        updated="$(json_field "$status_file" updated)"
        if [[ -n "$updated" ]]; then
            updated_epoch="$(date -d "$updated" +%s 2>/dev/null || echo "$now_epoch")"
        else
            updated_epoch="$now_epoch"
        fi
        diff=$(( now_epoch - updated_epoch ))
        if (( diff < 0 )); then
            diff=0
        fi
        age="$(human_age "$diff")"

        last_line=""
        log_file="${dir}agent.log"
        if [[ -f "$log_file" ]]; then
            last_line="$(sed -E \
                -e 's/\x1b\[[0-9;:<=>?$!]*[a-zA-Z@]//g' \
                -e 's/\x1b\][^\x07]*(\x07|\x1b\\)//g' \
                -e 's/\x1b[()][0-9A-Za-z]//g' \
                -e 's/[\x0d\x0e\x0f]//g' \
                "$log_file" \
                | grep -v '^[[:space:]]*$' | tail -1 || true)"
        fi

        row="$(printf '%-14s %-10s %-6s  %s' "$name" "$state" "$age" "$last_line")"
        printf '%s\n' "${row:0:width}"
    done
fi
