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

---

# Phase D Results — self-bootstrapping build (2026-07-03)

**Verdict: COMPLETE. All five packages built by the system itself — LEAD
decomposed, spawned Sonnet workers, reviewed, and merged every PR (#10–#12,
#14, #15). Suite grew 59 → 101 tests, all green, ruff+mypy clean.**

| Package | Worker | PR | Interventions |
|---|---|---|---|
| D1 dashboard | d1 | #10 | 0 |
| D2 respawn + stop-guard | d2 | #11 | 0 |
| D3 inbox hygiene | d3 | #12 | 0 |
| D4 watchdog + self-nudge rule | d4 | #14 | 1 (lost DONE nudge re-sent by CC; watchdog did not exist yet) |
| D5 docs refresh | d5 | #15 | 0 — the lost nudge was caught by the D4 watchdog in production |

Average interventions 0.2/package (acceptance bar: ≤1). The lead also
self-initiated correct behaviors nobody asked for: fresh worktrees from main
per worker, PR-based merges for the toy branches, post-merge worker cleanup.

## New failure class found: agent self-nudges

All "mystery drafts" this session were the LEAD typing next-step intents
into its own pane, where the lost-Enter behavior parked them and the draft
guard then (correctly) suppressed all incoming nudges — deadlock. Fixes:
LEAD.md now bans self-nudging (merged in #14), and the watchdog's
unprocessed-report sweep breaks any residual stall.

## Also fixed mid-phase (infrastructure, not lead conduct)

- Stale-DONE re-detection: a sentinel-less turn re-detected the previous
  turn's DONE from history; Stop-state now scopes to the turn's final
  assistant message (PR #13).

## Validation sweep

Dashboard renders all agents one-screen; stop_agent refuses a dirty
worktree with a clear message; inbox read_new/mark_processed round-trips;
watchdog live-validated in production (woke the lead 5.5 min after d5's
lost nudge); docs spot-check accurate. respawn_agent is unit-tested (d2,
TDD) — live kill/resume drill deferred to Phase E operations. Every worker's
end state all phase was a sentinel; zero mystery idles.

---

# Phase E Results — first production sprint (2026-07-03/04, budget-buddy)

**Verdict: SUCCESS.** A ten-issue tiered backlog on a real repo (personal
financial data) was executed end-to-end by one Opus lead (914k context at
retirement) driving ~14 Sonnet workers: 13 PRs merged, 8 issues closed, 2
bugs found-and-fixed beyond the request, 2 discovered issues filed, 3
emergent improvements shipped. Every stop condition held: design decisions
escalated (5 questions before starting), real-DB writes dry-run + backed up
+ individually approved, credentials never touched, dependency order kept.
Residual open items were time-gated or awaiting-requester, not failed work.

## Failure classes found in production (all fixed, with regression tests)

1. **Human Enter loss is systematic.** Every one of the requester's typed
   pane replies (8+) parked unsubmitted — the TUI ignores a bare Enter after
   any pause. Fixes: `scripts/say.py` (verified-submit reply channel) and
   the **unstick loop** (`scripts/unstick.py` in tmux window `unstick`):
   force-submits a draft unchanged for 10+ minutes; in-progress typing
   restarts the clock. First live catch within 15 minutes of deployment; a
   parked draft had previously stalled the board for 3 hours overnight.
2. **Agent self-nudges deadlock the channel** (typing intent into one's own
   pane parks it, and the draft guard then suppresses all nudges). Banned in
   LEAD.md; unstick is the backstop.
3. **Silent completion without notify_target** — one worker (i7) was
   spawned without it and finished invisibly. Rule: notify_target is
   mandatory on every spawn (make it default-on in spawn_agent — filed as
   follow-up).
4. **Instruction eclipse under conversation load** — a standing task
   (issue closures) slipped for three turns because each turn answered only
   the newest inbox thread. Mitigation: single-purpose messages; durable
   fix: the lead keeps a standing TODO file on the blackboard (LEAD.md).
5. **Supervisor false escalations (CC's own, twice).** CC flagged the lead
   for "unauthorized" merges the requester had authorized directly in the
   pane, and "corrected" it that the `ant` CLI doesn't exist (it does —
   official Anthropic CLI). Rule: before correcting an agent, read the full
   instruction channel (its transcript's user messages) and verify facts
   against authoritative reference. The agent that read the source may be
   right.

## What the architecture proved

- Opus-lead / Sonnet-worker split: ~3M tokens total (estimate) for 13
  reviewed PRs; lead never wrote feature code, caught real issues in review
  (spawned i23b as a follow-up rather than rubber-stamping).
- The delivery stack is now defense-in-depth and every layer was validated
  by a real incident: verified submit → next-delivery self-heal → watchdog
  re-nudge → unstick stale-draft submit.
- Requester-in-the-loop worked as designed: 5 pre-start questions, per-write
  approvals, live policy renegotiation (merge authority) — all honored.

## Retirement

Lead retired at 914k/1M context after the sprint wrapped. Durable state
(blackboard, LEAD.md, GitHub issues/PRs, this doc) carries everything; the
next assignment starts with a fresh lead.
