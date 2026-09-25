# Plan 07: TUI

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Execution order and rationale: [`index.md`](index.md).

## Context

By this point the monitor collects everything and computes all flags. Headless mode already logs samples to JSONL and prints flag lines to stderr.

This plan adds the interactive live view: a set of `rich` tables refreshed every poll, rows highlighted when flagged, and keyboard controls. It follows the Renderer contract from Plan 01 (`start`, `render`, `on_flag`, `poll_keys`, `stop`). All values come from the sample and `derived` data; the TUI computes nothing of its own.

The UI should be functional and minimal: correctness and readability over decoration.

## Goal

`uv run monitor.py` in a terminal shows a live dashboard of Claude's processes, memory, I/O, disk footprint, swap, write budget and flags, with `q`/`p`/`m`/`b` keys, and always restores the terminal.

## Prerequisites

Plans 01, 03, 04, 05 and 06 complete. The TUI reads their output.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| U-1 | Live-updating table rendered with `rich` (`Live`). |
| U-2 | Columns: name, current, Δ since last poll, Δ since start, peak; CPU rows also show the baseline. |
| U-3 | Highlight rows that cross a threshold. |
| U-4 | Header: session start, elapsed, log path, whether `--trace-io` is active. |
| G-2 | `p` toggles per-PID rows under each role. |
| I-6 | Directory growth visible as a secondary I/O signal. |
| K-1 | `q` (and Ctrl-C) quit cleanly: flush log, restore terminal. |
| K-2 | `p` toggles the per-PID view. |
| K-3 | `m` writes a timestamped marker with an optional label. |
| K-4 | `b` resets CPU/memory baselines and peaks. |
| (§4.9) | Key input via stdlib `termios`/`tty` cbreak mode. |

## Design

### Layout

Panels, stacked vertically. When the terminal is ≥ 160 columns wide, they are arranged in two columns (left: processes; right: disk and system).

1. **Header** (U-4):
   - Session start time, elapsed, poll interval, log path.
   - Claude status: running/not running, app version, main PID, coalition ID.
   - `trace-io: on/off`.
   - Key help: `q quit · p PIDs · m marker · b reset`.
2. **CPU** (% of one core). Rows: each role + Total. Columns: `Role | n | Current | Δpoll | Δstart | Peak | Avg(60s) | Baseline | vs base`.
3. **Memory** (physical footprint; RSS as a second value). Rows: roles + Total. Columns: `Role | Footprint | Δpoll | Δstart | Peak | RSS | Avg(5m) | vs base`.
4. **Disk I/O**. Rows: roles + Total. Columns: `Role | Write/s | Avg(60s) | Read/s | Written this session | Peak write/s`. Footer line: rolling 24 h written (`X GiB of 2 GiB (p%) · of 8 GiB (q%)`).
5. **Disk footprint** (I-6). Rows:
   - bundle rows `rootfs.img`, `sessiondata.img`, `other`, `bundle total`;
   - tracked directories (`vm_bundles`, `vm_warm`, `cache`, `code_cache`, `claude_code_vm`, `agent_sessions`, `indexeddb`, `gpucache`, `support_total`).

   Columns: `Path | Allocated | Apparent | Δpoll | Δstart | Peak`. Absent paths are shown dim as `absent`.
6. **System**: swap used/total, swap-ins, swap-outs (cumulative, plus Δpoll), current streak count.
7. **Flags and events**:
   - Active state flags with their message and the time raised.
   - Event flags from the last 10 minutes.
   - Historic `.diag` reports from startup, marked `(before session)`.
   - With `--trace-io`: the top 10 written files over 60 s.
   - With `--include-cli`: a separate small `Claude Code CLI` table (CPU, footprint, write/s); it is never included in Total.

**Per-PID view (`p`, G-2).** Under each role row, indented rows show every PID: `pid`, exe basename (always for `other`/`xpc`), attribution (`?` shown for `name?`), and that table's metrics.

### Formatting

- Bytes in binary units (`KiB`/`MiB`/`GiB`, 1 decimal). Rates add `/s`. CPU as `12.3%`.
- Deltas are signed (`+1.2 MiB`, `−0.4%`); zero deltas shown as `·`.
- `None` → `n/a` (unreadable, I-4); warming baselines → `…`.

### Highlighting (U-3)

- A row whose series has an **active state flag** is shown in **red**.
- A VM row with `?` attribution is shown in **yellow**.
- Absent paths are dim.
- No other styling.

### Rendering mechanics

- `rich.live.Live(auto_refresh=False, screen=True, transient=False)`. `render()` builds a fresh renderable and calls `live.update(..., refresh=True)` once per poll and after each key.
- `vertical_overflow="crop"`. If the terminal is too short, the bottom panels are cropped, and the header shows `(terminal too small: N rows hidden)`.

### Keys (K-1 to K-4)

- On `start()`: save `termios` attributes and switch stdin to cbreak (`tty.setcbreak`).
- `poll_keys(timeout)`: `select.select([stdin], [], [], timeout)`, then read the available bytes. The main loop uses this as its inter-poll wait, so keys respond immediately.
  - `q` → stop request (same path as SIGINT).
  - `p` → toggle the PID view and re-render.
  - `b` → `analyzer.reset()`, which also logs a marker, and re-render.
  - `m`:
    1. Stop `Live` and restore normal terminal mode.
    2. Prompt `Marker label (Enter for none): ` with `input()`.
    3. Write a `marker` record.
    4. Return to cbreak mode and restart `Live`.

    Polling is paused while the prompt is open; the gap is visible in the log's `elapsed`.
- On `stop()`, always restore `termios` attributes and stop `Live`, from `finally` and an `atexit` handler, so the terminal is sane after exceptions and SIGTERM.

### Fallback

If stdin is not a TTY (e.g. piped input) while stdout is a TTY, run the TUI without key handling and state `keys disabled` in the header.

## Tasks

1. §8: `TuiRenderer` implementing the renderer contract; panels 1–7; formatting helpers.
2. Key handling (cbreak, select-based wait, `m` prompt flow) and terminal restoration.
3. Main-loop integration: choose the TUI when stdout is a TTY and `--no-tui` is absent; route `b` to `analyzer.reset()`.
4. `tests/test_tui.py`:
   - Formatting helpers (bytes, signed deltas, `n/a`, warming).
   - Render a synthetic sample with `rich.console.Console(record=True, width=200)` and assert on key strings: role names, `absent`, red style on a flagged row, the per-PID rows after toggling.

## Verification (done when)

- [ ] Interactive run with Claude Desktop and Cowork's VM up:
  - all panels populate within two polls;
  - VM row present;
  - Docker's VM absent;
  - bundle rows show ~10 GiB `rootfs.img`.
- [ ] `p` shows per-PID rows, including `xpc` services with their exe names; pressing again hides them.
- [ ] `m`, type "test", Enter: the log contains `{"type": "marker", "label": "test"}` and the dashboard resumes.
- [ ] `b`: baselines show `…`, then repopulate after the window fills; a marker is logged.
- [ ] With `--cpu-threshold 1`, flagged CPU rows turn red within ~60 s and appear in the flags panel.
- [ ] `q` exits and the terminal echoes input normally (`stty -a` shows `icanon echo`). Same after `kill -TERM`, and after an injected exception (a temporary debug raise).
- [ ] An 80×24 terminal shows the header and the first panels without garbling, with a cropped note.
- [ ] `uv run pytest` passes. Committed.

## Plan-level decisions (not specified in REQUIREMENTS.md)

- One panel per metric family (CPU, memory, I/O, footprint, system), so U-2's column set fits.
- Two-column layout at ≥ 160 columns.
- Red = active flag, yellow = uncertain VM attribution, dim = absent; no near-threshold colouring.
- Polling pauses during the `m` prompt.
- Keys are disabled when stdin is not a TTY.

## Out of scope

Metric collection and alert logic (earlier plans); README (Plan 08).
