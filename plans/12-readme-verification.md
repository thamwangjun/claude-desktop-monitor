# Plan 12: README and v2 verification

Part of the Claude Desktop Resource Monitor v2 build. Full requirements: [`../requirements/REQUIREMENTS-v2.md`](../requirements/REQUIREMENTS-v2.md). Execution order and rationale: [`index.md`](index.md).

## Context

Plans 09–11 implement notifications. This plan documents them for the user, whose main question will be "why am I not getting notifications?", and runs the complete v2 verification checklist against the real app, recording the results as Plan 08 did for v1.

## Goal

A README that explains notification behaviour and troubleshooting, and a recorded, passing verification pass over V-7 to V-12.

## Prerequisites

Plans 09–11 complete. Claude Desktop running with Cowork's VM (as for Plan 08).

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| §8.4 | README: notification behaviour (which flags, tiers and sounds, cooldown, `--no-notify`); first-run permission and Focus modes; `--notify-test`; why the main icon is terminal-notifier's; the SIGKILL / power-loss limit. |
| N-17 | Document that SIGKILL and power loss cannot notify. |
| N-24 | Document undetectable failures (permission off, Focus, Do Not Disturb) together with `--notify-test`. |
| V-7 to V-12 | Full v2 verification. |

## README additions

A new **Notifications** section after the alert rules:

1. **What notifies**: a table of flag type → tier → sound, taken from REQUIREMENTS-v2 N-5 and N-12. `attr:vm?` and cleared flags do not notify. Startup-listed `.diag` reports do not notify.
2. **Noise control**: one notification per flag; per-flag cooldown (`--notify-cooldown`, default 300 s); suppressed notifications are still in the log.
3. **Unexpected exit**: SIGTERM, SIGHUP and crashes notify. `q` and Ctrl-C do not. SIGKILL and power loss cannot. `nohup` runs ignore SIGHUP.
4. **Setup and troubleshooting**:
   - Run `uv run monitor.py --notify-test` once. The first notification makes **terminal-notifier** appear in System Settings → Notifications; allow it, and choose the Banners or Alerts style.
   - Focus modes and Do Not Disturb suppress banners silently; the monitor cannot detect this.
   - A Homebrew `terminal-notifier` on PATH is used in preference to pync's bundled copy.
   - The main icon is terminal-notifier's. The Claude logo appears as a thumbnail, because macOS only takes the main icon from the sending app bundle (a one-paragraph explanation, pointing to REQUIREMENTS-v2 §3).
5. **Log**: the `notification` record type and its statuses.
6. CLI table: add `--no-notify`, `--notify-cooldown`, `--notify-test`.

Also update the JSONL record-type list (`notification`, `flag.details`, `session_end.signal`) and the install section (pync is now a dependency; `uv sync` builds it from source).

## Verification checklist

Every live check is done **one at a time and confirmed with the user** before the next.

| ID | How |
|---|---|
| V-7 | `uv run pytest`: all v1 and v2 tests pass. |
| V-8 | `uv run monitor.py --notify-test`: warning (Glass) and critical (Basso) banners with the Claude thumbnail; exit 0. |
| V-9 | User-driven CPU spikes (setup as in Plan 10: measure idle, `--cpu-window 6 --cpu-threshold <idle + 5> --notify-cooldown 120`): a banner worded per §5; `flag.details` and `notification: sent` records; clear, then re-spike within 120 s → `suppressed_cooldown`, no banner; after 120 s → banner again. |
| V-10 | Headless `kill -TERM` → SIGTERM exit banner and a complete log. TUI in tmux, pane killed → SIGHUP banner. `q` and Ctrl-C → no banner. |
| V-11 | `--no-notify`: no banners, no `notification` records, header `notify: off`. |
| V-12 | Cadence (N-3): run headless for 10 min with `--cpu-window 6 --cpu-threshold <idle + 5> --notify-cooldown 0` while the user spikes and releases Claude's CPU repeatedly (each spike raises and notifies). Every `sample` record's `elapsed` step is within `interval ± 0.5 s`, and the log shows ≥ 10 `sent` records. Checked with a short `jq`/Python script over the log, which is recorded in the results. |

Results are recorded in `plans/12-verification-results.md` in the Plan 08 format (machine, versions, date, pass/fail per check, evidence excerpts).

## Tasks

1. README Notifications section and the other README updates listed above.
2. Run V-7 to V-12 with the user; write `plans/12-verification-results.md`.
3. Fix any defect found in the plan section that owns it (09, 10 or 11), noting it in the commit message.

## Verification (done when)

- [ ] README covers every item in §8.4, N-17 and N-24.
- [ ] V-7 to V-12 recorded and passing.
- [ ] Committed, message prefixed `plan 12:`.

## Plan-level decisions (not specified in REQUIREMENTS-v2)

- The V-12 tolerance is ±0.5 s per poll step, over a 10-minute run with at least 10 notifications sent.
- V-9 and V-12 induce flags with user-driven CPU spikes (short CPU window, threshold just above idle). Reviewed 2026-09-26.
- Results go in a separate file, as in Plan 08.

## Out of scope

New features. Anything found here that needs a requirement change goes back to REQUIREMENTS-v2 first.
