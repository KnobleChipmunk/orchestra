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
