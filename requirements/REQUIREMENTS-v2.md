# Claude Desktop Resource Monitor — Requirements v2

Status: agreed requirements, pre-implementation (2026-09-26). Grey areas 1–12 and open items O-1 to O-4 are resolved.

v2 is an **incremental** change on top of [`REQUIREMENTS-v1.md`](REQUIREMENTS-v1.md), which remains authoritative for everything not mentioned here. v1 is fully implemented (plans 01–08).

## 1. Purpose

v1 flags anomalies in the TUI, on stderr (headless) and in the JSONL log. That requires someone to be watching the terminal or reading the log afterwards. v2 adds **macOS notifications** when flags fire, so degradation is noticed while it happens, which matters most during unattended overnight headless runs.

The tool remains observability only (v1 §1, §2).

## 2. Non-goals

- No click actions on notifications (clicking just dismisses).
- No per-flag-type notification configuration: the notified set (§4.2) is fixed.
- No notification when a flag clears.
- No custom main notification icon (not possible without a signed app bundle, see §3).
- No build step (v1 E-4 still holds); no Swift helper app.

## 3. Reference environment (observed facts the design relies on)

Tested 2026-09-26 on the reference machine (macOS 26.7) with the project's mise Python 3.14.7 (python-build-standalone). The interpreter is **ad-hoc, linker-signed, with no app bundle** (`codesign`: `Identifier=-`, `Signature=adhoc`, `Info.plist=not bound`).

macOS notification APIs (`UNUserNotificationCenter`, and the deprecated `NSUserNotificationCenter`) only deliver notifications for an **app bundle**. Consequences for Python libraries:

| Library | Latest (PyPI) | Mechanism | Result with the project interpreter |
|---|---|---|---|
| desktop-notifier 6.2.0 | 2025-08-08 | UNUserNotificationCenter via rubicon-objc | Silently falls back to `DummyNotificationCenter` ("can only be used from an app bundle"); nothing shown |
| macos-notifications 0.2.1 | 2024-10-12 | NSUserNotification via pyobjc | Crashes: `defaultUserNotificationCenter()` returns `None` |
| notify-py 0.3.43 | 2024-06-05 | bundled `Notificator.app` | Shown (0.24 s; 2.65 s first call) |
| **pync 2.0.3** | 2018-05-04 | bundled `terminal-notifier.app` 2.0.0, launched as a subprocess | **Shown**; call returns immediately |
| osascript `display notification` | built into macOS | Script Editor | Shown (~0.08 s); no icon option |

Icon customisation tests (pync / terminal-notifier):

| Test | Result |
|---|---|
| `appIcon` (replace app icon, private API) | Ignored: terminal-notifier icon shown |
| `contentImage` (attached image) | **Works**: Claude logo shown as thumbnail next to the terminal-notifier app icon |
| `sender=com.anthropic.claudefordesktop` (pose as Claude) | Notification not shown |
| Custom `osacompile` applet with Claude's icon, ad-hoc re-signed | Notification not shown; app never registered in System Settings → Notifications |

Other facts:

- Claude's app icon: `/Applications/Claude.app/Contents/Resources/electron.icns` (`CFBundleIconFile`); `sips -s format png` converts it to PNG.
- macOS reports no error when notifications are disabled for the sender or suppressed by a Focus mode; only a failed launch or a non-zero exit status is detectable.
- terminal-notifier treats a message starting with `-` or `[` specially; such text must be escaped.
- pync 2.0.3 installs from sdist (no wheel) and works on Python 3.14.7.

## 4. Functional requirements

### 4.1 Delivery

| ID | Requirement |
|---|---|
| N-1 | Notifications are sent via **pync** (bundled terminal-notifier). No fallback mechanism. |
| N-2 | Every notification attaches **Claude's app icon as `contentImage`** (thumbnail), from a **committed PNG** `assets/claude-icon.png` (256 px, generated once from `/Applications/Claude.app/Contents/Resources/electron.icns` with `sips`). No runtime dependency on Claude.app. If the file is missing, the notification is sent without an image. |
| N-3 | Sending **never blocks the poll loop**. A slow or hung notifier must not delay sampling. |
| N-4 | Title, subtitle and body text are escaped per terminal-notifier's rules (leading `-` / `[`), and truncated to fit a banner: subtitle ≤ 40 chars, body ≤ 2 lines of ≤ 60 chars; long paths shortened in the middle. |

### 4.2 What notifies

| ID | Requirement |
|---|---|
| N-5 | Notified flag types: `cpu:*`, `mem:*`, `wr:*`, `budget:*`, `bundle:growth`, `bundle:rate`, `bundle:zst`, `partial:*`, `swap:streak`, `diag:*`. **`attr:vm?` does not notify** (tool-internal attribution confidence; stays in the TUI and log). |
| N-6 | Only the **`raised`** and **`event`** states notify. **`cleared`** does not. |
| N-7 | **One notification per flag**: flags raised in the same poll are not merged, and per-role flags are not suppressed when the matching `total` flag also fires. |
| N-8 | **Per-flag cooldown**: after a notification is sent for a flag ID, further `raised`/`event` transitions of the **same flag ID** within `--notify-cooldown` (default **300 s**) are not notified. They are still flagged in the TUI/stderr and logged as `suppressed_cooldown` (L-7). The cooldown is measured from when the last notification for that ID was **dispatched** (handed to the sender), not from delivery confirmation. `diag:<file>` IDs are unique per report, so they are effectively never suppressed. The `b` key (v1 K-4) does **not** reset cooldowns. |
| N-9 | Startup-listed diagnostic reports (v1 X-4) **do not notify**. |

### 4.3 Content

| ID | Requirement |
|---|---|
| N-10 | **Title**: `Claude Monitor`. **Subtitle**: names the signal (and role, where applicable). **Body line 1**: current value vs threshold. **Body line 2**: a short "look next" hint. Wording per flag type is in §5. |
| N-11 | Role names are shown as in the TUI; the `total` suffix is shown as `Total Claude`. Values use the same units and formatting as the TUI (`format_bytes`, % of one core). |

### 4.4 Severity and sound

| ID | Requirement |
|---|---|
| N-12 | Two tiers. **Critical**: `diag:*`, `budget:*`, `bundle:growth`, `bundle:rate`, `bundle:zst`, `partial:*`, and the unexpected-exit notification (N-14). **Warning**: `cpu:*`, `mem:*`, `wr:*`, `swap:streak`. |
| N-13 | Critical plays sound **`Basso`**; warning plays sound **`Glass`**. |

### 4.5 Monitor lifecycle

| ID | Requirement |
|---|---|
| N-14 | Notify (critical tier) when the monitor **exits unexpectedly**: an unhandled exception, or **SIGTERM** / **SIGHUP** (e.g. `kill`, a closed tmux pane or terminal). The body gives the reason and the log path, and warns that alerts have stopped. Not subject to the cooldown. Launched without waiting for confirmation, before any other shutdown step, so it goes out immediately. Shutdown then waits up to 2 s for flag notifications still queued; normally there are none. |
| N-15 | A **clean quit** (`q`, Ctrl-C / SIGINT) and monitor **start** do not notify. |
| N-16 | SIGHUP gets a handler (v1 installs SIGINT and SIGTERM only) so that it performs the same clean shutdown as SIGTERM (flush log, restore terminal) in addition to N-14. SIGTERM continues to shut down cleanly but is now classed as unexpected. |
| N-17 | SIGKILL and power loss cannot be caught; the README documents this limit. |

### 4.6 Enablement and modes

| ID | Requirement |
|---|---|
| N-18 | Notifications are **on by default**; `--no-notify` disables all of them (including N-14). |
| N-19 | Notifications are sent in **both** TUI and headless modes (v1 H-1, H-2). |
| N-20 | The TUI header (v1 U-4) shows `notify: on` / `notify: off`. |
| N-21 | `--notify-test`: send one sample **warning** and one sample **critical** notification (with the thumbnail and tier sounds), report whether the sends succeeded, and exit without monitoring. It works regardless of `--no-notify`. Intended for checking macOS notification permissions. |

### 4.7 Failures

| ID | Requirement |
|---|---|
| N-22 | A failed send (notifier cannot be launched, or exits non-zero) is logged (L-7) and produces **one warning per session**: in the TUI (e.g. in the header or flag area) or, headless, one stderr line. Later failures are logged only. |
| N-23 | Failures never stop monitoring and never disable notifications; each subsequent flag still attempts a send. Exception: if pync cannot be loaded at startup, notifications are unavailable for the session (nothing could be sent); this is warned once and logged (`notify:init`), and the TUI header shows `notify: off (unavailable)`. |
| N-24 | The undetectable cases (notifications disabled for terminal-notifier in System Settings, Focus modes, Do Not Disturb) are documented in the README together with `--notify-test`. |

### 4.8 Logging

| ID | Requirement |
|---|---|
| L-7 | New record type **`notification`** (v1 L-2 extended): flag `id`, `tier`, `title`, `subtitle`, `body`, and `status` = `sent` \| `suppressed_cooldown` \| `failed` (with `error` when failed) \| `dispatched` (unexpected-exit notification only: launched without waiting, outcome unknown). The unexpected-exit notification uses a reserved id (e.g. `monitor:exit`). Carries the v1 L-3 timestamp fields. |
| L-8 | The `session_start` record's config includes `notify` (on/off) and `notify_cooldown`. |

## 5. Notification wording (per flag type)

Values in `<>` are filled in at send time. The hint wording was accepted in O-1 (§12), and two hints were later shortened to fit the N-4 limits.

| Flag | Tier | Subtitle | Body line 1 | Body line 2 (hint) |
|---|---|---|---|---|
| `cpu:<role>` | warning | `High CPU — <role>` | `<avg>% of one core (<window> s avg) > <threshold>%` | `Baseline <baseline>%.` For `vm`: `See ~/Library/Logs/Claude/cowork_vm_swift.log`; otherwise `Idle climb? See TUI role rows` |
| `mem:<role>` | warning | `Memory growth — <role>` | `5-min avg <avg> > baseline <baseline> + <pct>%` | `Slow-leak pattern; press b in TUI to re-baseline` |
| `wr:<role>` | warning | `High disk writes — <role>` | `<avg>/s (60 s avg) > <threshold>/s` | `Counts toward macOS 24 h budget; --trace-io shows files` |
| `swap:streak` | warning | `Sustained swapping` | `Swap-outs up on 3 consecutive polls` | `Swap used <used>; check memory rows` |
| `budget:2g` / `budget:8g` | critical | `Write budget <pct>% — <2 GiB\|8 GiB> tier` | `Total Claude wrote <bytes> in 24 h` | `macOS files a disk-writes .diag report at 100%` |
| `bundle:growth` | critical | `VM bundle growth` | `+<growth> since start > <threshold>` | `rootfs.img is never trimmed; check sessiondata.img` |
| `bundle:rate` | critical | `VM bundle growing fast` | `+<rate> in 10 min > <threshold>` | `Check ~/Library/Logs/Claude/ for downloads` |
| `bundle:zst` | critical | `VM download cache <removed\|reappeared>` | `rootfs.img.zst <disappeared\|reappeared>` | `Reappeared = re-download; see ~/Library/Logs/Claude/` |
| `partial:<path>` | critical | `Stalled VM download` | `<file> is <age> old (> 5 min)` | `Possible re-download loop; see ~/Library/Logs/Claude/` |
| `diag:<file>` | critical | `macOS diagnostic: <event>` | disk writes: `<writes> in <duration> (limit <limit>)`; other events: `<event>, action: <action taken>` | `See /Library/Logs/DiagnosticReports/<file>` |
| `monitor:exit` | critical | `Monitor stopped unexpectedly` | `<reason: exception type / SIGTERM / SIGHUP>` | `Alerts have stopped. Log: <log path>` |

## 6. CLI changes (additions to v1 §6)

| Flag | Default | Purpose |
|---|---|---|
| `--no-notify` | off (notifications on) | Disable macOS notifications |
| `--notify-cooldown SEC` | 300 | Per-flag notification cooldown (N-8) |
| `--notify-test` | off | Send sample warning and critical notifications, then exit (N-21) |

## 7. Tooling and packaging changes

| ID | Requirement |
|---|---|
| E-3 (amended) | Runtime dependencies: `psutil`, `rich`, `watchdog`, **`pync`**. |
| E-7 | No other new dependencies. The thumbnail is a committed asset, `assets/claude-icon.png` (N-2). |

## 8. Deliverables

1. `monitor.py`: notification support.
2. `pyproject.toml`, `uv.lock`: `pync` added.
3. `assets/claude-icon.png`: notification thumbnail (N-2).
4. `README.md` additions:
   - notification behaviour: which flags notify, tiers and sounds, cooldown, `--no-notify`;
   - first-run permission: terminal-notifier appears in System Settings → Notifications; Focus modes suppress banners silently; use `--notify-test` to check;
   - why the main icon is terminal-notifier's (app-bundle requirement, §3) and the Claude logo is a thumbnail;
   - the SIGKILL / power-loss limit of N-14.
5. This `requirements/REQUIREMENTS-v2.md`.

## 9. Verification

Unit tests use a **mocked sender** (no real notifications). Live checks are done **one at a time, each confirmed with the user** before the next.

| ID | Requirement |
|---|---|
| V-7 | Unit tests: flag → notification mapping (N-5, `attr:vm?` excluded), state filter (N-6), one-per-flag (N-7), cooldown incl. `suppressed_cooldown` logging (N-8), startup diag reports not notified (N-9), tier/sound (N-12, N-13), wording and escaping/truncation (N-4, §5), failure warn-once (N-22, N-23), `notification` records (L-7), `--no-notify`. |
| V-8 | `uv run monitor.py --notify-test` shows a warning (Glass) and a critical (Basso) notification, each with the Claude thumbnail. |
| V-9 | Live induced flag via user-driven CPU spikes (`--cpu-window 6`, threshold just above Claude's idle CPU): the notification appears with correct content; a clear then re-raise within the cooldown is not notified and is logged as `suppressed_cooldown`. |
| V-10 | `kill -TERM <pid>` on a headless run: the unexpected-exit notification appears and the log is flushed. Clean `q` / Ctrl-C: no notification. |
| V-11 | `--no-notify` run with an induced flag: no notification; the TUI header shows `notify: off`. |
| V-12 | The sampling cadence is unaffected while notifications are sent (N-3): during a 10-min run with repeated user-driven CPU spikes and `--notify-cooldown 0`, poll timestamps in the log stay within the configured interval. |

## 10. Decision log (grey areas)

| # | Grey area | Decision |
|---|---|---|
| 1 | Mechanism | pync with Claude `contentImage` thumbnail (see §3 for alternatives tested) |
| 2 | Which flags | All except `attr:vm?` |
| 3 | Transitions | `raised` + `event`; no `cleared` |
| 4 | Default | On by default, `--no-notify`; no per-type config |
| 5a | Bursts | One notification per flag, no merging |
| 5b | Flapping | Per-flag cooldown, 5 min |
| 6 | Content | Values + short hint |
| 6b | Click action | None |
| 7 | Severity | Two tiers: critical (Basso), warning (Glass) |
| 8 | Modes | TUI + headless; TUI header indicator |
| 9a | Startup `.diag` reports | Not notified (X-4 unchanged) |
| 9b | Lifecycle | Unexpected exit only |
| 10 | Failures | Non-blocking, warn once, keep trying; exception: pync import failure at startup → notifications unavailable for the session |
| 11 | Logging | `notification` record with `sent` / `suppressed_cooldown` / `failed` / `dispatched` (exit notification only) |
| 12 | Verification | Mocked-sender unit tests, `--notify-test`, live checks |

## 11. Relation to v1 requirements

| v1 item | Change |
|---|---|
| L-2 record types | + `notification` (L-7) |
| U-4 TUI header | + `notify: on/off` (N-20) |
| H-1 headless | + one-time notification-failure warning on stderr (N-22) |
| Signal handling (K-1) | SIGHUP handled; SIGTERM/SIGHUP classed as unexpected exit (N-14, N-16) |
| E-3 dependencies | + `pync` |
| §6 CLI | + `--no-notify`, `--notify-cooldown`, `--notify-test` |

## 12. Open items

| # | Item | Status |
|---|---|---|
| O-1 | Hint wording per flag type | Resolved 2026-09-26: accepted as drafted in §5 |
| O-2 | Thumbnail source | Resolved 2026-09-26: committed PNG `assets/claude-icon.png` (N-2) |
| O-3 | `--notify-cooldown` configurability | Resolved 2026-09-26: CLI flag, default 300 s (§6) |
| O-4 | Cooldown vs `b` key | Resolved 2026-09-26: `b` does not reset notification cooldowns (N-8) |
