# Phase B Pilot Results — worker→lead wake-link (2026-07-03)

**Verdict: PASS — 12/12 matrix messages processed, plus the hook-driven
worker `DONE:` notify. Gate open for Phase D.**

Setup: LEAD = Opus 4.8 interactive session (ACK-counting brief), worker =
Sonnet 5 with `notify_target=lead`. Delivery via `messaging.deliver`
(inbox append + "Check your inbox." nudge).

## Matrix outcomes (attempt 3, final code)

| Case | Msgs | Outcome |
|---|---|---|
| Lead idle | M1–M4 | 4/4 processed. ~half needed the self-heal path (previous nudge wedged; next delivery resubmitted it). |
| Lead mid-turn (75s Bash) | M5–M9 | 5/5 processed after turn end (queued or healed by successive deliveries). |
| Human draft in prompt | M10–M12 | 3/3 inbox-delivered with keystrokes correctly skipped; processed on the post-draft pickup nudge. |
| Hook notify (worker DONE) | 1 | Delivered and ACKed — the production path works end-to-end. |

## Failure signatures found and fixed (attempts 1–2)

1. **Scrollback-as-draft false positive.** The TUI renders past submissions
   with the same `❯` prefix as the live input box; the guard matched
   history and skipped every nudge forever. Fix: only the LAST `❯` line
   (the input box, NBSP-padded) counts.
2. **Channel-poisoning deadlock.** A nudge whose Enter was lost sat in the
   input box and was then treated as a human draft — one lost Enter blocked
   all future nudges. Fix: a stuck nudge is recognized and resubmitted, not
   skipped.
3. **Stale-box Enter ignored.** A bare Enter does NOT submit an input box
   whose text was typed earlier (observed: 7+ consecutive Enters ignored);
   Enter only registers after fresh keystrokes. Fix: `_force_submit` types
   a space first; `deliver` verifies the box drained and retries once.

## Reliability model (accepted)

Per-nudge delivery is lossy (mid-turn arrivals can wedge), but the channel
is **self-healing**: every delivery repairs a predecessor's stuck nudge, and
the message content is always safe in the inbox. Defense in depth:
delivery-time verify+retry → next-delivery self-heal → (Phase D4) watchdog
sweep for quiet periods. No message was lost in any attempt — only nudges,
never content.

## Notes for LEAD.md operation

- The lead must treat "Check your inbox." as "read EVERYTHING new", not
  "read one message" — batching under mid-turn queuing is normal.
- A final turn-end inbox check (already in LEAD.md) covers draft-guarded
  deliveries.

---

# Phase C Gate Results — uncoached lead run (2026-07-03)

**Verdict: PASS.** A fresh Opus lead, briefed only by docs/LEAD.md plus a
two-subtask goal, decomposed, spawned two Sonnet workers into separate
worktrees, was woken by their hook-driven DONE nudges, reviewed both diffs,
ran the test suite itself, and emitted a genuine line-initial DONE — no
coaching of the lead's decisions at any point.

## Findings fixed mid-gate (both merged with regression tests)

1. **MCP permission wedge** (PR #5): acceptEdits + Read/Write/Edit/Bash
   allowlist does not cover MCP tools; the lead froze on a dialog at its
   first orchestrator call. spawn_agent now pre-approves
   `mcp__claude-orchestrator`.
2. **Quoted-sentinel false done** (PR #6): the lead quoting the protocol
   ("print 'DONE: <summary>'") in its planning prose was detected as a real
   DONE 15s into the run. Sentinels now count only at line start.

## Operational finding (not a bug): parked human drafts stall the loop

A human-typed draft sat in the lead's input box; the draft guard correctly
skipped every nudge to protect it, so both worker DONEs waited silently in
the inbox until a human submitted/cleared the draft. Working as designed,
but it makes the D4 watchdog (staleness sweep) the priority Phase D package,
and argues for the lead's turn-end inbox check being non-negotiable.

## Interventions tally (gate acceptance context)

Two mid-run code fixes (above) plus one delivery assist caused by the parked
draft. Zero coaching of decomposition, spawning, monitoring, or review.
