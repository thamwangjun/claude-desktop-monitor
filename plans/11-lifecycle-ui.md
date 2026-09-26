# Plan 11: Monitor lifecycle and UI integration

Part of the Claude Desktop Resource Monitor v2 build. Full requirements: [`../requirements/REQUIREMENTS-v2.md`](../requirements/REQUIREMENTS-v2.md). Execution order and rationale: [`index.md`](index.md).

## Context

After Plan 10, flags notify, but two gaps remain for the overnight use case:

1. **Silent death.** If the monitor is killed or crashes, alerts simply stop, and nobody notices until morning. v1 handles SIGINT and SIGTERM identically (`_install_signal_handlers`: both set `stop_reason = "signal"`). SIGHUP (e.g. a closed terminal or tmux pane) is not handled at all, so the process dies with the default action: no log flush of the final state, no terminal restore.
2. **No visible state.** The TUI header does not show whether notifications are on, and a failing notifier is only visible in the log.

This plan touches process-level behaviour (signals, exception paths, shutdown order), which is why it is separate from Plan 10.

## Goal

An unexpected exit (unhandled exception, SIGTERM, SIGHUP) sends a critical notification before the process ends. A clean quit is silent. The TUI header shows `notify: on/off`, and the first notification failure is shown once in the TUI or on stderr.

## Prerequisites

Plans 09 and 10 complete.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| N-14 | Notify (critical, no cooldown) on unexpected exit: unhandled exception, SIGTERM, SIGHUP. Body: reason and log path, "alerts have stopped". |
| N-15 | Clean quit (`q`, Ctrl-C / SIGINT) and start do not notify. |
| N-16 | Add a SIGHUP handler performing the same clean shutdown as SIGTERM; SIGTERM is classed as unexpected. |
| N-17 | SIGKILL / power loss cannot be caught (README, Plan 12). |
| N-18 | `--no-notify` also suppresses the exit notification. |
| N-20 | TUI header shows `notify: on` / `notify: off`. |
| N-22 (display) | First notification failure: one warning in the TUI, or one stderr line when headless; later failures only logged. |

## Design

### Exit classification

`_RunState.stop_reason` becomes one of `quit` (`q`), `sigint`, `sigterm`, `sighup`. `_install_signal_handlers` installs one handler per signal that records its own reason, and adds `signal.SIGHUP`.

| Cause | `session_end.reason` | Notify? |
|---|---|---|
| `q` | `quit` | no |
| Ctrl-C / SIGINT | `signal` (+ `"signal": "SIGINT"`) | no |
| SIGTERM | `signal` (+ `"signal": "SIGTERM"`) | **yes** |
| SIGHUP | `signal` (+ `"signal": "SIGHUP"`) | **yes** |
| unhandled exception in `run()` | `error` (+ traceback, as in v1) | **yes** |

The v1 `reason` values stay unchanged; the specific signal is an added `signal` field (additive).

### Exit notification (N-14)

`build_exit_notification(cause: str, log_path: str) -> Notification`: `flag_id="monitor:exit"`, tier critical, subtitle `Monitor stopped unexpectedly`, body line 1 `<SIGTERM | SIGHUP | ExceptionType: message>`, line 2 `Alerts have stopped. Log: <shortened log path>`, per REQUIREMENTS-v2 §5.

Shutdown order in `run()`, on each unexpected path:

1. `renderer.stop()` (restore the terminal first, as in v1).
2. If notifications are enabled and pync loaded: `sender.fire_and_forget(exit_notification)` (Plan 09). This launches terminal-notifier from the main thread **without waiting**, bypassing both the cooldown policy and the worker queue (which may be busy or stuck). The child process completes even after the monitor exits.
3. Log a `notification` record with **`status: "dispatched"`**: the outcome is unknown by design. This is a fourth L-7 status, used only for `monitor:exit`.
4. The shared shutdown step from Plan 10 (every shutdown, clean or not): `sender.flush(timeout=2)` for **flag** notifications still queued, then drain and log their results. Then `session_end`, then close collectors, the sender and the log (v1 order otherwise).

The exit notification itself is launched before the flush, so it goes out immediately even if a supervisor follows SIGTERM with SIGKILL a few seconds later. The flush only adds a delay (at most 2 s) when flag notifications are still queued; normally the queue is empty and there is no delay.

A second SIGTERM/SIGHUP during this shutdown is ignored (the handlers only set state), so shutdown cannot be cut short into a half-written log.

### SIGHUP details (N-16)

With the TUI, a hang-up means the terminal is gone: `renderer.stop()` must tolerate a dead TTY (catch `OSError` / `termios.error` when restoring attributes). Headless `nohup` runs ignore SIGHUP by design (nohup sets it to `SIG_IGN` before exec). The handler must **not** override an inherited `SIG_IGN`: check `signal.getsignal(SIGHUP)` before installing, so `nohup uv run monitor.py --no-tui` keeps running after logout.

### Renderer contract addition (N-22 display)

New method `on_notify_warning(message: str)` on both renderers:

- `HeadlessRenderer`: one stderr line, `HH:MM:SS NOTIFY-WARN <message>`.
- `TuiRenderer`: stores the message; the header shows it (see below) until the session ends.

`run()` calls it for the `SendResult` with `first_failure=True` (Plan 09 guarantees exactly one per session). Message: `notification failed (<error>); monitoring continues, see log`.

### TUI header (N-20)

The second header line gains `notify: on` / `notify: off` after `trace-io: …`. After the first failure it reads `notify: on (failing: see log)`, and the warning message appears as an extra header line in yellow for the rest of the session. If pync failed to load at startup, it reads `notify: off (unavailable: see log)`, with the same warning line. `TuiRenderer` receives the `notify_state` (`on` / `off` / `unavailable`) at construction.

## Tasks

1. `_RunState` reasons; per-signal handlers; SIGHUP with the `SIG_IGN` check; `session_end.signal` field.
2. `build_exit_notification`; the unexpected-exit paths in `run()` (signal, exception) with the shutdown order above; the second-signal guard.
3. Dead-TTY-tolerant `TuiRenderer.stop()`.
4. `on_notify_warning` on both renderers; call it from `run()` on `first_failure`.
5. TUI header `notify:` field and warning line.
6. Tests:
   - `tests/test_cli.py` / new `tests/test_lifecycle.py`: the handler sets the right reason per signal; SIGHUP left alone when inherited as `SIG_IGN`;
   - `run()` with fake collectors and a fake sender: SIGTERM → `fire_and_forget` called once, `notification(status=dispatched)` + `session_end(signal=SIGTERM)` logged, and `fire_and_forget` is called before `flush`; `q` and SIGINT → no exit notification; injected exception → exit notification with the exception type, and the exception still propagates; `--no-notify` → no exit notification;
   - `tests/test_tui.py`: the header contains `notify: on` / `notify: off`, and the failing variant after `on_notify_warning`;
   - headless `on_notify_warning` prints exactly one line.

## Verification (done when)

- [ ] `uv run pytest` passes.
- [ ] **V-10** (live, each step confirmed with the user):
  1. Headless `uv run monitor.py --no-tui &`, then `kill -TERM <pid>`: the `Monitor stopped unexpectedly: SIGTERM` banner (Basso, thumbnail) appears, and the log ends with `notification` (`dispatched`) then `session_end` (`signal: SIGTERM`).
  2. TUI in a tmux pane, then kill the pane: the SIGHUP banner appears, the log is complete, and the terminal is not garbled.
  3. TUI `q`: no banner. TUI Ctrl-C: no banner.
- [ ] **V-11 (header part)**: the TUI header shows `notify: on`; with `--no-notify` it shows `notify: off`.
- [ ] Forced failure (temporarily point the backend at a missing binary via a debug edit): one yellow header warning, then only log records on later failures.
- [ ] Committed, message prefixed `plan 11:`.

## Plan-level decisions (not specified in REQUIREMENTS-v2)

- The v1 `session_end.reason` values are kept; the specific signal goes in a new `signal` field.
- The exit notification bypasses the cooldown policy and the worker queue, and is not waited for (`dispatched` status). Reviewed 2026-09-26.
- A pync import failure at startup shows `notify: off (unavailable: see log)` in the header, plus the warning (the same `on_notify_warning` path).

The other decisions above were reviewed with the user on 2026-09-26 and kept.
- An inherited `SIG_IGN` for SIGHUP (nohup) is respected.
- The failure warning stays in the TUI header for the rest of the session.

## Out of scope

README and the full verification record (Plan 12).
