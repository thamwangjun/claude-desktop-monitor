# Plan 10: Notification policy (flags → notifications)

Part of the Claude Desktop Resource Monitor v2 build. Full requirements: [`../requirements/REQUIREMENTS-v2.md`](../requirements/REQUIREMENTS-v2.md). Execution order and rationale: [`index.md`](index.md).

## Context

Plan 09 delivers a ready-made `Notification`. This plan decides **which flag events become notifications, when, and with what text**, logs every outcome, and wires it into the poll loop for both TUI and headless modes.

Flag events come from `Analyzer._raise` / `_clear` / `_event` (`monitor.py`, "flag helpers") as `{id, state, metric, value, threshold, message}`. The §5 wording needs values those events do not carry (CPU baseline and window, swap used, parsed `.diag` fields, bytes written in 24 h). Decision: **enrich flag events with an optional `details` dict** rather than having the notification code look values up in `derived`. The notification text then depends only on the event, and `flag` log records gain the same context.

## Goal

Every notifiable `raised`/`event` flag produces exactly one notification (subject to a per-flag cooldown), worded per REQUIREMENTS-v2 §5, and every notification outcome is in the JSONL log.

## Prerequisites

Plan 09 complete.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| N-5 | Notify all flag types except `attr:vm?`. |
| N-6 | Only `raised` and `event` notify; `cleared` does not. |
| N-7 | One notification per flag; no merging, no role-vs-total suppression. |
| N-8 | Per-flag-ID cooldown `--notify-cooldown` (default 300 s), measured from the last sent notification; suppressed ones are logged; `b` does not reset cooldowns. |
| N-9 | Startup-listed `.diag` reports do not notify. |
| N-10, N-11, §5 | Title / subtitle / body line 1 (value vs threshold) / body line 2 (hint); role display names; TUI units. |
| N-12 | Tier per flag type. |
| N-18 | `--no-notify` disables notifications. |
| N-19 | Notifications in both TUI and headless modes. |
| L-7 | `notification` record: `id`, `tier`, `title`, `subtitle`, `body`, `status` = `sent` \| `suppressed_cooldown` \| `failed` (+ `error`). |
| L-8 | `session_start` config includes `notify` and `notify_cooldown`. |

## Design

### 1. Enriched flag events (Analyzer)

`_raise`, `_clear` and `_event` gain `details: dict | None = None`; when given, the event carries `"details": details`. Existing keys are unchanged, so v1 consumers and tests keep working (additive).

| Flag | `details` |
|---|---|
| `cpu:<suffix>` | `role`, `window_s`, `avg`, `baseline` (number or `None` while warming) |
| `mem:<suffix>` | `role`, `avg`, `baseline`, `growth_pct` |
| `wr:<suffix>` | `role`, `avg` |
| `swap:streak` | `streak`, `swap_used` (bytes) |
| `budget:2g` / `budget:8g` | `tier_label` (`2 GiB` / `8 GiB`), `pct`, `written_24h` (bytes), `budget` (bytes) |
| `bundle:growth` | `growth` (bytes) |
| `bundle:rate` | `rate` (bytes per 10 min) |
| `bundle:zst` | `present` (bool) |
| `partial:<path>` | `path`, `age_s` |
| `diag:<file>` | `file` (full path), `event`, `writes_mb`, `writes_over_s`, `limit_kbps`, `action` |
| `attr:vm?` | none (not notified) |

`_swap_streak_flag` needs the current swap-used value; `analyze()` already has `swap`, so pass it in.

### 2. Tiers and filter

```python
_NOTIFY_EXCLUDED = {"attr:vm?"}
_CRITICAL_PREFIXES = ("diag:", "budget:", "bundle:", "partial:", "monitor:")
def notification_tier(flag_id) -> str: "critical" if startswith(_CRITICAL_PREFIXES) else "warning"
```

Warning: `cpu:`, `mem:`, `wr:`, `swap:`. Unknown future prefixes default to warning.

### 3. Wording: `build_notification(event) -> Notification`

A pure function implementing the REQUIREMENTS-v2 §5 table exactly (title `Claude Monitor`). Role display: `total` → `Total Claude`, otherwise the role key as shown in the TUI (`renderer`, `utility:node`, `vm`, …). Bytes via `format_bytes`, CPU `%.0f%%`, ages and durations via the TUI's `_fmt_duration`. Paths through `shorten_middle` (Plan 09). If `details` is missing or incomplete, fall back to the event's v1 `message` as body line 1, so a notification is never dropped over a formatting gap.

### 4. Policy: `NotificationPolicy`

```python
class NotificationPolicy:
    def __init__(self, sender: NotificationSender, cooldown_s: float, clock=time.monotonic): ...
    def handle(self, event: dict) -> dict | None
        # returns a `notification` log record for suppressed_cooldown, else None
```

- Filter: `state in {"raised", "event"}` (N-6) and `id not in _NOTIFY_EXCLUDED` (N-5).
- Cooldown (N-8): `_last_sent[flag_id]`; if `now - last < cooldown_s`, return a `suppressed_cooldown` record and do not send. Otherwise build, `sender.send()`, set `_last_sent[flag_id] = now`. The timestamp is set at dispatch, not at delivery confirmation.
- `analyzer.reset()` (`b`) does not touch the policy (N-8).
- N-7: each event is handled independently, in the order the Analyzer emitted them.
- N-9: needs no code. Startup reports arrive under `diag["historic"]` and never become flag events; a test asserts this.

### 5. Main loop wiring (N-18, N-19, L-7, L-8)

In `run()`:

- Create `sender` + `policy` only when notifications are enabled (`not args.no_notify`); otherwise `policy = None` and nothing is logged.
- When enabled, call `load_pync()` (Plan 09) once before monitoring starts. On failure: no sender and no policy for the session, and one `notification` record `{"id": "notify:init", "status": "failed", "error": …}` is written. `run()` keeps a `notify_state` of `on` / `off` / `unavailable` for Plan 11's header and warning.
- In the existing `for event in flag_events` loop, after `log.write(flag)` and `renderer.on_flag`, call `policy.handle(event)` and log any returned record.
- Each poll, after the flag loop: `for r in sender.drain_results(): log.write(notification_record(r))`. `sent` and `failed` records therefore come from the worker's results and are always written by the main thread (the LogWriter is not thread-safe).
- At shutdown (`finally`): `sender.flush(timeout=2)`, drain and log the remaining results, then `sender.close()`. This applies to **every** shutdown (clean quit, signal or exception) and only bounds how long already-queued flag notifications get to go out and confirm; normally the queue is empty. The unexpected-exit notification itself is fire-and-forget and is launched before this flush (Plan 11). Reviewed 2026-09-26.
- `session_start.config` already contains `vars(args)` (`no_notify`, `notify_cooldown`); add an explicit `"notify": not args.no_notify` field (L-8).

`notification` record:

```json
{"type": "notification", "id": "cpu:renderer", "tier": "warning", "title": "Claude Monitor",
 "subtitle": "High CPU — renderer", "body": "62% of one core (60 s avg) > 30%\nBaseline 8%. Idle climb? See TUI role rows",
 "status": "sent", "ts": "…", "elapsed": 123.4}
```

`failed` adds `"error"`. Both renderers work unchanged, since they are not involved in this plan (N-19).

### 6. CLI

- `--no-notify` (`store_true`).
- `--notify-cooldown SEC` (float, `≥ 0`, default 300); `0` disables damping (used in verification).

## Tasks

1. Analyzer: optional `details` on the three flag helpers; fill per the table in all flag helpers and `_diag_events`.
2. §7a: `_NOTIFY_EXCLUDED`, `notification_tier`, `build_notification`, `NotificationPolicy`, `notification_record`.
3. CLI flags; `run()` wiring (create, handle, drain per poll, flush at shutdown); `session_start.notify`.
4. Tests:
   - `tests/test_analysis.py`: `details` present with the expected keys for each flag type; v1 keys unchanged.
   - `tests/test_notify.py` (policy, fake sender, fake clock):
     - `cleared` not sent; `attr:vm?` not sent; every other type sent once;
     - tiers per type;
     - two different flags in one poll → two sends (N-7), including `cpu:renderer` + `cpu:total`;
     - cooldown: a re-raise at t+100 s produces `suppressed_cooldown` with no send, a re-raise at t+301 s is sent; `diag:` ids are unaffected by other diag ids; `--notify-cooldown 0` sends every time;
     - `analyzer.reset()` does not reset the cooldown;
     - `build_notification` golden strings for every §5 row, plus the fallback when `details` is missing;
     - historic diag reports (X-4 fixture) → no events → no sends;
     - `--no-notify` → no sender, no `notification` records.
   - `tests/test_cli.py`: new flag defaults and validation.

## Verification (done when)

- [ ] **V-7**: `uv run pytest` passes (v1 + v2 tests).
- [ ] **V-9** (live, confirmed with the user one step at a time): `uv run monitor.py --cpu-threshold 1 --notify-cooldown 120` with Claude running:
  1. A `High CPU — …` banner appears (Glass, thumbnail) within ~60 s, worded per §5.
  2. The log has matching `flag` (with `details`) and `notification` (`sent`) records.
  3. After a clear and re-raise within 120 s, no banner appears, and a `suppressed_cooldown` record is logged.
- [ ] **V-11** (live, confirmed with the user): the same run with `--no-notify` shows no banners and writes no `notification` records.
- [ ] Committed, message prefixed `plan 10:`.

## Plan-level decisions (not specified in REQUIREMENTS-v2)

- `details` payloads per the table above; the `flag` log record includes them.
- Cooldown timestamps are taken at dispatch, not at delivery confirmation.
- `sent` / `failed` records are logged when the worker reports them, which can be a poll later than the flag.
- Fall back to the v1 `message` when `details` is incomplete.
- Unknown flag prefixes default to the warning tier.
- `--notify-cooldown 0` means no cooldown.
- Pync import failure at startup → `notify:init` failed record; notifications unavailable for the session.

All of the above were reviewed with the user on 2026-09-26 and kept as drafted.

## Out of scope

Exit notifications, SIGHUP, TUI header indicator, on-screen failure warning (Plan 11); README (Plan 12).
