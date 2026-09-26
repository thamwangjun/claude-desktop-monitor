# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One module, `src/claude_desktop_monitor/monitor.py`: a macOS-only terminal tool that watches Claude Desktop's resource usage over time (especially the Cowork Linux VM), logs JSONL, raises flags against fixed rules, sends macOS notifications, and shows a `rich` TUI or runs headless. **Observability only.**

`PLAN_EXEC_INSTRUCTIONS.md` is the full working guide (verified technical facts, hard rules, git rules, user-only checks). Read it before non-trivial changes. Docs by authority: `requirements/REQUIREMENTS-v1.md` → `REQUIREMENTS-v2.md` → `REQUIREMENTS-v3.md` → `REQUIREMENTS-v3.1.md` (each later version wins where it amends an earlier one) → `plans/index.md` (status table, conventions) → `plans/NN-*.md`. If a plan conflicts with the requirements, stop and ask the user. v1 (plans 01–08), v2 (09–12) and v3 (13–17, Homebrew distribution) are done: 0.3.0 is published with an arm64 macOS 26 bottle on the tap `thamwangjun/homebrew-tap` (cloned at `../homebrew-tap`). v3.1 (18–19) is in progress: 18 (CPU threshold default 30 → 90, version 0.3.1, tag `v0.3.1`) and 19 (formula bump, bottle, publish) are not started. The default log location is `~/Library/Logs/claude-desktop-monitor/` (its own directory, separate from and unrelated to the read-only `~/Library/Logs/Claude/` below), `session_start.versions.monitor` reports the installed package version, and the package version is 0.3.1.

## Commands

```
mise install                          # Python 3.14.7, uv 0.12.19, Rust 1.98.1 (mise.toml)
uv sync                               # .venv from uv.lock; never pip install
uv run claude-desktop-monitor                     # TUI
uv run claude-desktop-monitor --no-tui            # headless (also automatic when stdout isn't a TTY)
uv run claude-desktop-monitor --notify-test       # sends 2 REAL banners; only when the user agrees
python -m claude_desktop_monitor --no-tui         # equivalent, without the console script
uv run pytest                         # full suite (always run the whole suite before committing)
uv run pytest tests/test_analysis.py::test_name   # single test
```

Tests import `from claude_desktop_monitor import monitor` (the package installs editable via `uv sync`, so no `sys.path` hack or `conftest.py` is needed). `tests/test_native.py` makes real ctypes calls against live processes.

## Architecture

One module, `src/claude_desktop_monitor/monitor.py`, importable without side effects, split into banner-marked sections `# ── §N name ──`:

| § | Section |
|---|---|
| 1 | Constants, unit helpers, CLI (`build_parser`) |
| 2 | Native macOS calls via ctypes on libSystem (`rusage`, `coalition_id`, `responsible_pid`) |
| 3 | `ProcessCollector` (+ optional `FsUsageTracer` for `--trace-io`) |
| 4 | `DiskCollector` (VM bundle + support dirs; watchdog FSEvents, incremental re-walks) |
| 5 | `SignalsCollector` (`SwapSampler` from `vm_stat`, `DiagReportWatcher` for `.diag` files) |
| 6 | `Analyzer` (rolling windows, baselines, 24 h write budget, flag state machine) |
| 7 | `LogWriter` (JSONL) |
| 7a | Notifications: `NotificationSender` (one worker thread), `NotificationPolicy` (tiers, cooldown), wording |
| 8 | `HeadlessRenderer`, `TuiRenderer` |
| 9 | `run()` main loop, signal handling, `main()` |

Data flow per poll in `run()`: each collector's `collect(now)` merges a fragment into one `sample` dict → `analyzer.analyze(sample)` returns `(derived, flag_events)` → log `diag_report`/`sample`/`flag` records → `NotificationPolicy.handle(event)` → drain sender results into `notification` records → `renderer.render(...)` → `poll_keys` (`q`/`p`/`m`/`b`). Shutdown in `finally`: restore terminal, fire-and-forget exit notification on unexpected exit (SIGTERM/SIGHUP/exception, not `q`/SIGINT), bounded flush, `session_end`.

Component contracts (keep them stable):
- Collector: `collect(now) -> dict`, `close()`. Never raise for vanished processes, missing paths or unreadable data; report `None`/`absent`.
- Analyzer: `analyze(sample) -> (derived, flag_events)`, `reset()`, `active_flags`.
- Renderer: `start()`, `render(sample, derived, active_flags)`, `on_flag(event)`, `poll_keys(timeout)`, `on_notify_warning(msg)`, `stop()`.
- `LogWriter` is only ever called from the main thread.
- Sample keys (`claude`, `processes`, `roles`, `total`, `cli`, `trace_io`, `paths`, `bundle`, `swap`, `diag`, `derived`, `active_flags`) and log record types are a contract; don't rename them.
- Units: bytes, bytes/s, CPU % of one core (can exceed 100%), seconds; display in KiB/MiB/GiB.

Non-obvious facts (details in `PLAN_EXEC_INSTRUCTIONS.md` §6):
- Claude's processes are identified by **resource coalition ID** (private `PROC_PIDCOALITIONINFO`), falling back to responsible PID, then name match (shown as `?`). Docker's `com.apple.Virtualization.VirtualMachine` must never be counted as Claude's; Claude Code CLI / Zed agent / `Claude Usage.app` aren't Claude Desktop.
- `psutil.swap_memory().sin/.sout` are file paging on macOS, not swap; real swap is `vm_stat` `Swapins`/`Swapouts`. `psutil` `io_counters()` doesn't exist on macOS; disk I/O comes from `proc_pid_rusage`.
- Notifications go through pync → `terminal-notifier` (Homebrew arm64 preferred via PATH), because the unbundled interpreter can't use UNUserNotificationCenter. Don't re-evaluate other notification libraries. Fire-and-forget launches `terminal-notifier` directly with `start_new_session=True` so a SIGHUP doesn't kill it.
- FSEvents paths are resolved (`/tmp` → `/private/tmp`), so `--support-dir` is `resolve()`d before comparison.

## Hard rules

- Never modify, delete or move anything under `~/Library/Application Support/Claude/`, `~/Library/Logs/Claude/` or `/Library/Logs/DiagnosticReports/`, in code or while testing. Tests use `tmp_path`; manual runs use `--support-dir /tmp/...`. `~/Library/Logs/claude-desktop-monitor/` is the monitor's own log directory (default `--log` destination), not covered by this rule; tests still never write there — pass `--log` under `tmp_path`.
- Never run `sudo` (only `--trace-io` uses it, and the user runs that themselves). Never kill/restart Claude Desktop, its VM or Docker; ask the user.
- Unit tests use a fake notification sender/backend, never real banners. Never change macOS notification/Focus settings.
- README may cite only the 9 issues approved in `REQUIREMENTS-v1.md` §11.
- No new flags, dependencies or files beyond what's asked. Runtime deps are `psutil`, `rich`, `watchdog`, `pync`.
- Work on `main`, never push unless asked. Plan commits are prefixed `plan NN: …`; `plans/index.md` status gets the short hash. Style: plain Python 3.14, type hints where they help, comments only for non-obvious *why*.
