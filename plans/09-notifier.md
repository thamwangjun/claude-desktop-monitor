# Plan 09: Notifier (delivery layer)

Part of the Claude Desktop Resource Monitor v2 build. Full requirements: [`../requirements/REQUIREMENTS-v2.md`](../requirements/REQUIREMENTS-v2.md) (incremental on [`REQUIREMENTS-v1.md`](../requirements/REQUIREMENTS-v1.md)). Execution order and rationale: [`index.md`](index.md).

## Context

v2 sends macOS notifications when flags fire. This plan builds only the **mechanism**: given a fully formed notification (title, subtitle, body, tier), deliver it via pync without ever blocking the poll loop, and report the outcome back to the main thread. *Which* flags notify, and with what wording, is Plan 10.

REQUIREMENTS-v2 §3 records why pync was chosen: the project's mise Python is not an app bundle, so UNUserNotificationCenter-based libraries silently fail or crash. pync bundles `terminal-notifier.app` 2.0.0, which is a bundle, and it delivered notifications in testing. Only `contentImage` (a thumbnail) can show the Claude logo.

pync facts that shape the design (from `pync/TerminalNotifier.py`, 2.0.3):

- `import pync` creates a module-level `TerminalNotifier()` which runs `which terminal-notifier` and prefers a `terminal-notifier` on PATH (e.g. Homebrew) over the vendored copy. It **raises** if the binary is unusable. Decision: import it **eagerly at startup** when notifications are on, so a broken install is reported at once.
- `notify(message, **kwargs)` maps each kwarg to a `-<name> <value>` argument and launches the binary with `Popen`. Without `wait=True` it returns immediately and **never sees the exit status**; with `wait=True` it waits and raises on a non-zero exit.
- pync depends on `python-dateutil` (pulled in transitively).

## Goal

A `NotificationSender` that delivers notifications asynchronously with tier sounds and the Claude thumbnail, reports `sent`/`failed` results, and a `--notify-test` flag that proves delivery works on the user's Mac.

## Prerequisites

v1 complete (plans 01–08).

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| N-1 | Deliver via pync (bundled terminal-notifier); no fallback. |
| N-2 | Attach `assets/claude-icon.png` as `contentImage`; send without an image if the file is missing. |
| N-3 | Sending never blocks the poll loop. |
| N-4 | Escape text per terminal-notifier (leading `-` / `[`); truncate to fit a banner, shortening long paths in the middle. |
| N-13 | Critical tier → sound `Basso`; warning tier → sound `Glass`. |
| N-21 | `--notify-test`: send one warning and one critical sample, report success, exit; works regardless of `--no-notify`. |
| N-22 / N-23 (detection) | Detect failed sends (launch failure / non-zero exit) and report them; never stop monitoring. Logging is Plan 10, display is Plan 11. |
| E-3, E-7 | Add `pync` as a runtime dependency; commit `assets/claude-icon.png`. |

## Design

### Placement

A new `monitor.py` section, `# ── §7a Notifications ──`, between §7 (JSONL log writer) and §8 (renderers). Plan 10 adds its policy code to the same section.

### Data

```python
@dataclass(frozen=True)
class Notification:
    flag_id: str          # e.g. "cpu:renderer", "diag:<file>", "monitor:exit", "test:warning"
    tier: str             # "critical" | "warning"
    title: str            # always "Claude Monitor" except --notify-test
    subtitle: str
    body: str             # up to 2 lines joined with "\n"

@dataclass
class SendResult:
    notification: Notification
    status: str           # "sent" | "failed"
    error: str | None
    first_failure: bool   # True only for the first failure this session (N-22 warn-once)
```

`TIER_SOUNDS = {"critical": "Basso", "warning": "Glass"}`. `NOTIFY_ICON = Path(__file__).resolve().parent / "assets" / "claude-icon.png"`.

### Text helpers (N-4)

- `escape_notifier_text(s)`: prefix `\` when the string starts with `-` or `[` (terminal-notifier treats those as options / special input).
- `shorten_middle(s, max_len)`: `/Library/Logs/Diag…/Claude_2026-09-25-213433_nu.diag` style; used by Plan 10 for paths.
- `truncate(s, max_len)`: end ellipsis. Limits: subtitle ≤ **40** chars, each body line ≤ **60** chars, body ≤ 2 lines. All REQUIREMENTS-v2 §5 texts fit these limits except long file paths, which `shorten_middle` handles.

### Sender (N-1, N-3, N-22 detection)

```python
class NotificationSender:
    def __init__(self, backend: Callable[[Notification], None] | None = None, max_queue: int = 64): ...
    def send(self, n: Notification) -> None       # non-blocking enqueue
    def drain_results(self) -> list[SendResult]   # called by the main thread each poll
    def flush(self, timeout: float) -> None       # wait up to timeout for the queue to empty (shutdown, --notify-test)
    def fire_and_forget(self, n: Notification) -> None  # main thread, no wait, no result (exit notification, Plan 11)
    def close(self) -> None
```

- One **daemon worker thread** consumes a `queue.Queue(maxsize=64)`. The poll loop only calls `send()` (`put_nowait`) and `drain_results()` (reads a results queue), so a slow or hung notifier can only stall the worker, never sampling (N-3).
- Queue full → the notification is not sent; a `failed` result with `error="queue full"` is produced immediately.
- The default **backend** (`_pync_backend`) runs in the worker thread:
  1. Uses the `pync` module loaded at startup (see "Loading pync").
  2. `pync.Notifier.notify(escaped_body, title=…, subtitle=…, sound=TIER_SOUNDS[tier], contentImage=str(NOTIFY_ICON) if it exists, wait=True)`.
  3. Any exception → `failed` with `error=f"{type(e).__name__}: {e}"`.
- `first_failure` is set on the first failed result of the session only; the sender tracks this so Plans 10 and 11 need no extra state.
- `fire_and_forget(n)` calls `pync.Notifier.notify(…, wait=False)` directly on the calling thread: it launches terminal-notifier and returns without waiting, bypassing the queue (which may be busy or stuck). The child process completes even if the monitor exits. Used only for the unexpected-exit notification (Plan 11).

### Loading pync

`load_pync() -> tuple[module | None, str | None]` does `import pync` inside `try` and returns `(module, None)` or `(None, "<ExceptionType>: <message>")`. It is called **once at startup, only when notifications are enabled** (Plan 10 wires it into `run()`; `--notify-test` calls it too). On failure, notifications are **off for the session**, with one warning and a log record (Plans 10 and 11). This is the one case where notifications are disabled automatically: nothing could be sent. It clarifies N-23, which is about individual send failures. With `--no-notify`, pync is never imported.
- Tests inject a fake backend (records calls, can raise or sleep).

### Thumbnail asset (N-2, E-7)

Generated once and committed:

```
sips -s format png -Z 256 /Applications/Claude.app/Contents/Resources/electron.icns --out assets/claude-icon.png
```

At runtime only `NOTIFY_ICON.exists()` is checked; no dependency on Claude.app or `sips`.

### `--notify-test` (N-21)

- New flag in `parse_args`. Handled in `main()` **before** `_sudo_preflight()` and `run()`; it ignores `--no-notify` and every monitoring flag.
- Calls `load_pync()`; on failure prints the error and exits 1.
- Sends two samples through a real `NotificationSender`, then `flush(timeout=10)`:
  - warning: subtitle `Test notification — warning tier`, body `Sound: Glass` / `If you can see this, notifications work.`
  - critical: subtitle `Test notification — critical tier`, body `Sound: Basso` / `If you can see this, notifications work.`
- Prints one line per result to stderr, plus a reminder that a successful send cannot prove the banner was shown (System Settings → Notifications → terminal-notifier; Focus modes). Exit code 0 if both `sent`, else 1.

### Dependency (E-3)

`uv add pync` → `pyproject.toml` dependencies become `psutil`, `rich`, `watchdog`, `pync`; `uv.lock` updated. pync 2.0.3 ships as an sdist only; `uv sync` builds it (verified working on Python 3.14.7).

## Tasks

1. `uv add pync`; generate and commit `assets/claude-icon.png`.
2. §7a: `Notification`, `SendResult`, `TIER_SOUNDS`, `NOTIFY_ICON`, text helpers.
3. `NotificationSender` with worker thread, result queue, `flush`, `close`, `first_failure`; default pync backend.
4. `--notify-test` in `parse_args` and `main()`.
5. `tests/test_notify.py` (fake backend only; no real notifications):
   - escaping (`-x`, `[x`, normal text) and truncation/middle-shortening;
   - `load_pync()` success and failure (monkeypatched import);
   - `fire_and_forget()` calls the backend with `wait=False` and returns immediately;
   - `send()` returns immediately while the backend sleeps (e.g. backend sleeps 1 s, `send()` < 50 ms);
   - backend success → `sent`; backend raises → `failed` with error text; `first_failure` only on the first failure;
   - queue full → immediate `failed` (`queue full`);
   - tier → sound mapping; `contentImage` omitted when the icon path does not exist;
   - `--notify-test` parse and exit code with a fake backend (success → 0, one failure → 1).

## Verification (done when)

- [ ] `uv run pytest` passes (all v1 tests plus `tests/test_notify.py`).
- [ ] `uv sync` from a clean `.venv` installs pync without errors.
- [ ] **V-8** (live, confirmed with the user one notification at a time): `uv run monitor.py --notify-test` shows the warning banner (Glass) and the critical banner (Basso), each with the Claude thumbnail; exit code 0.
- [ ] Committed, message prefixed `plan 09:`.

## Plan-level decisions (not specified in REQUIREMENTS-v2)

- One daemon worker thread and a bounded queue (64); a full queue produces a `failed` result rather than blocking.
- pync's `notify(..., wait=True)` inside the worker gives exit-status detection; no timeout on a single send (a hung send stalls only the worker and the notifications queued behind it, never sampling). Reviewed 2026-09-26: kept.
- Eager `import pync` at startup when notifications are on; import failure → notifications off for the session, with a warning. Reviewed 2026-09-26.
- Text limits: subtitle 40 chars, body lines 60 chars. Reviewed 2026-09-26; two §5 hints were reworded to fit.
- Thumbnail at `assets/claude-icon.png`. Reviewed 2026-09-26.
- `--notify-test` sample wording as above; exit 0 only if both sends succeed. Reviewed 2026-09-26.
- A PATH `terminal-notifier` (e.g. Homebrew) takes precedence over pync's vendored copy; this is pync's behaviour and is left as is.

## Out of scope

Which flags notify, cooldown, wording, `--no-notify`, logging (Plan 10); exit notifications, SIGHUP, TUI/stderr warning display (Plan 11); README (Plan 12).
