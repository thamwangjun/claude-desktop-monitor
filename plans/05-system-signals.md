# Plan 05: System and external signals (swap, macOS diagnostic reports)

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Execution order and rationale: [`index.md`](index.md).

## Context

Two system-wide signals complete the picture of Claude Desktop's impact.

**Swap.** When memory pressure rises (the Cowork VM alone uses about 1–2 GB), macOS swaps. On macOS, `psutil.swap_memory().sin/.sout` are **not** swap: they equal `vm_stat`'s `Pageins`/`Pageouts`, which mostly reflect ordinary file reads and writes. This was verified on 2026-09-25 with psutil 7.2.2: 263 GB of "sin" versus 0 real swap-ins. True swap counters are `vm_stat`'s `Swapins`/`Swapouts`.

**macOS diagnostic reports.** macOS enforces per-app disk-write budgets over a rolling 86,400 s:
- **2 GiB** (24.86 KB/s)
- **8 GiB** (99.42 KB/s)

When an app exceeds one, macOS writes a report to `/Library/Logs/DiagnosticReports/`. That directory is readable without sudo on the reference machine: files are `root:_analyticsusers`, mode 660, and the user is in the group. Cowork's first-run provisioning on the reference machine produced such a report. Header excerpt:

```
Command:          Claude
Path:             /Applications/Claude.app/Contents/MacOS/Claude
Identifier:       com.anthropic.claudefordesktop
Version:          2.9939.2 (2.9939.2)
Resource Coalition: "com.anthropic.claudefordesktop"(11603)
PID:              87512

Event:            disk writes
Action taken:     none
Writes:           2147.48 MB of file backed memory dirtied over 1059 seconds (2028.35 KB per second average), exceeding limit of 24.86 KB per second over 86400 seconds
Writes limit:     2147.48 MB
Limit duration:   86400s
```

A report from Docker's VM in the same directory had `Resource Coalition: "com.docker.docker"(11507)`. It has the same process name as Claude's VM and must be ignored.

These reports are macOS's own verdict, the ground truth to compare the monitor's metrics against.

## Goal

A signals collector (§5) providing swap counters each poll, and live detection and parsing of Claude's macOS diagnostic reports.

## Prerequisites

Plan 01 complete. Independent of Plans 02–04, except that it optionally reads the current Claude coalition ID from the sample's `claude` section (Plan 03) to strengthen matching. It works without it.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| M-2 | Swap used/total (`sysctl vm.swapusage` or `psutil.swap_memory().used/.total`), and cumulative swap-ins and swap-outs from `vm_stat` `Swapins`/`Swapouts` × page size, **not** psutil `sin`/`sout`. |
| X-1 | Watch `/Library/Logs/DiagnosticReports/` via FSEvents for new `.diag` files. |
| X-2 | A report is Claude's when its `Command` is a tracked Claude binary **or** its `Resource Coalition` is `com.anthropic.claudefordesktop`. Other coalitions are ignored. |
| X-3 | Parse `Event`, `Writes`/`Writes limit`, `Duration`, `Action taken` and the date range. Raise a flag, show it, and log a `diag_report` record. |
| X-4 | At startup, list Claude reports from the preceding 24 h once, without flagging them. |
| X-5 | Unreadable directory → "n/a", continue. |
| L-6 | Log record type `diag_report`. |
| V-5 | Startup listing finds the existing Claude report and ignores the Docker one. |

## Design

### Swap (M-2)

- `psutil.swap_memory()` → `total`, `used`, `free`. These come from `sysctl vm.swapusage`, which is correct.
- `vm_stat` subprocess each poll (a few ms at a 3 s interval):
  - Parse the page size from the header `(page size of N bytes)`.
  - Parse the `Swapins:` and `Swapouts:` lines (values end with `.`).
  - Output `swapins_bytes` and `swapouts_bytes` = pages × page size.
  - Also record `pageins_bytes`/`pageouts_bytes` for context, clearly named so they aren't mistaken for swap.
- If `vm_stat` fails, those fields are `None` and the other fields are still reported.

Sample fragment:

```json
"swap": {"total": 2147483648, "used": 0, "free": 2147483648,
         "swapins_bytes": 0, "swapouts_bytes": 0,
         "pageins_bytes": 263051001856, "pageouts_bytes": 6248251392}
```

### Diagnostic reports (X-1 to X-5)

- **Directory.** `/Library/Logs/DiagnosticReports/`. If it isn't listable, report `diag.status = "unreadable"` and skip (X-5).
- **Watching.** A `watchdog` observer (non-recursive) on the directory. Created and moved-to events for `*.diag` files go into a pending queue. The poll thread processes the queue.
- **Parsing.**
  - Read up to the first 80 lines or 16 KB, stopping at the first blank line after the `Event:` block.
  - Parse `Key: value` pairs for `Date/Time`, `End time`, `Command`, `Path`, `Identifier`, `Version`, `Resource Coalition`, `PID`, `Event`, `Action taken`, `Writes`, `Writes limit`, `Limit duration`, `Duration`, `CPU`, `CPU limit`.
  - `Resource Coalition` is split into a name and an ID using `"<name>"(<id>)`.
  - Numbers inside `Writes` (MB written, seconds, average KB/s, limit KB/s) are extracted with a regex. The raw line is kept either way.
- **Incomplete files.** A report may be observed before it is fully written. If `Event:` isn't found yet, retry on the next two polls before recording it as `incomplete`.
- **Claude match (X-2).** Any one of:
  - coalition name == `com.anthropic.claudefordesktop`;
  - coalition ID == the current Claude coalition ID;
  - `Path` inside a `Claude.app` bundle.

  Everything else is ignored, including `com.docker.docker`.
- **Dedupe.** By file path; each report is processed once per session.
- **Startup (X-4).** Scan the directory for `*.diag` files with mtime in the last 24 h and parse them. Claude matches become `diag_report` records with `historic: true` and are listed in the TUI's events panel. They do not raise flags.
- **Output.** New (non-historic) Claude reports are returned in the sample as `diag.new: [...]`. Plan 06 turns each into a `flag` event with `state: "event"`, and Plan 01's log writer writes a `diag_report` record for each.

`diag_report` record:

```json
{"type": "diag_report", "ts": "...", "elapsed": 812.3, "historic": false,
 "file": "/Library/Logs/DiagnosticReports/Claude_2026-09-25-213433_nu.diag",
 "event": "disk writes", "action": "none", "command": "Claude", "pid": 87512,
 "coalition": {"name": "com.anthropic.claudefordesktop", "id": 11603},
 "start": "2026-09-25 21:16:53.979 +0800", "end": "2026-09-25 21:34:32.714 +0800",
 "writes_mb": 2147.48, "writes_over_s": 1059, "avg_kbps": 2028.35, "limit_kbps": 24.86, "limit_duration_s": 86400,
 "raw": {"Writes": "...", "Event": "..."}}
```

## Tasks

1. §5: `SwapSampler` (psutil and `vm_stat` parser).
2. `DiagReportWatcher`: observer, pending queue, header parser, retry for incomplete files, Claude matching, dedupe, startup scan.
3. Emit `diag_report` records (extend the log writer's dispatch) and the `diag.new` sample field.
4. `tests/test_signals.py`:
   - `vm_stat` parser on captured output (including the page-size header).
   - `.diag` parser on the two reference reports' headers, stored as fixtures under `tests/fixtures/`: header lines only, host name redacted.
   - Claude match accepts the Claude report and rejects the Docker one.
   - Incomplete-file retry.
   - Writes-line regex.

## Verification (done when)

- [ ] `swap.swapouts_bytes` matches `vm_stat` Swapouts × page size at the time of the sample. `pageouts_bytes` is recorded separately.
- [ ] V-5: startup on the reference machine (within 24 h of 2026-09-25 21:34) logs a historic `diag_report` for `Claude_2026-09-25-213433_nu.diag` and none for `com.apple.Virtualization.VirtualMachine_2026-09-25-210018_nu.diag` (Docker coalition). Outside that window, verify with the fixture test instead.
- [ ] A synthetic report copied into a temp directory, with the watch path overridden in the test, is detected and parsed while running.
- [ ] Making the directory unreadable (simulated in a test by pointing the watcher at a mode-000 temp dir) → `status: "unreadable"`, no exception.
- [ ] `uv run pytest` passes. Committed.

## Plan-level decisions (not specified in REQUIREMENTS.md)

- `pageins_bytes`/`pageouts_bytes` are logged for context, but never drive a flag.
- Incomplete reports are retried for two polls.
- Test fixtures store only header lines of the reference reports, with the host name redacted.
- Only `/Library/Logs/DiagnosticReports/` is watched; `~/Library/Logs/DiagnosticReports/` is not, because disk-write/CPU resource reports go to the system location.

## Out of scope

The swap-out streak flag (M-3) and turning reports into flags (Plan 06); display (Plan 07).
