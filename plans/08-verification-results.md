# Plan 08: verification results

Machine: Apple M5 (Mac17,4), 10 CPUs, 32 GB RAM, macOS 26.7 (25G229). Claude Desktop 2.9939.2. Docker Desktop running concurrently for the whole session. Date: 2026-09-26.

| Check | Result |
|---|---|
| `uv run pytest` | **Pass** — 103 passed, 1 warning (an unrelated `DeprecationWarning` about `os.fork()` in a multi-threaded process from `tests/test_native.py`), 2.93s. |
| Fresh-clone install | **Pass** — see below. |
| V-1 | **Pass** — see below. |
| V-2 | **Pass** — see below. |
| V-3 | **Pass** — see below. |
| V-4 | **Pass** — see below. |
| V-5 | **Pass** — see below. |
| V-6 | **Pass** — see below. |

## Fresh-clone install

```
git clone /Users/thamw/development/local/claude-desktop-monitor /tmp/cdm-freshclone
cd /tmp/cdm-freshclone
mise install     # all tools already installed on this machine
uv sync          # created .venv, installed psutil 7.2.2, rich 15.0.0, watchdog 6.0.0, pytest 9.1.1 + deps
uv run monitor.py --no-tui --log /tmp/cdm-freshclone-run.jsonl   # ~30 s, then SIGTERM
```

Result: 13 lines, every line valid JSON. Record sequence: `session_start`, `diag_report` (the historic Claude report, see V-5), 10 × `sample`, `session_end`. stderr showed only the one-line "logging to ..." banner. **Pass.**

## V-1 — live TUI run with a real Cowork task, Docker's VM concurrently running

Ran `uv run monitor.py` (TUI, default log path `logs/monitor-20260926-003044.jsonl`) for 393 s (6.6 min) while Docker Desktop was running with its own VM (see V-6 for the PID/coalition detail). Pressed `m` at elapsed 13.5 s with label "before", started a Cowork task, pressed `m` again at elapsed 374.9 s with label "after" once it finished, then quit with `q`.

Between the two markers, `sessiondata.img` grew from 32,702,464 to 34,799,616 bytes (apparent) and the bundle total's allocated size grew by ~473 KiB — consistent with the per-session growth pattern in #37860. The VM row (PID 87782, `attribution: "coalition"`) was present and populated in every sample throughout. No flags were raised — expected, since a single short Cowork task is well under every alert's threshold (30% sustained CPU, 1 GB bundle growth, 100 MB/10 min bundle rate, 1 MB/s sustained writes). Session ended cleanly (`session_end`, `reason: "quit"`). **Pass.**

(An earlier attempt, `logs/monitor-20260926-001854.jsonl`, ran the TUI for 10.6 min without a Cowork task or markers — kept for reference but superseded by the run above, which exercises the marker and Cowork-task part of the procedure.)

## V-2 — `proc_pid_rusage` readable for the VM process without root

Ran `uv run monitor.py --no-tui --log logs/v1v2v5v6.jsonl` for ~11 s (no `sudo`). Last sample's VM-role process entry:

```json
{"pid": 87782, "role": "vm", "footprint": 1130154168, "disk_written": 138203136, "disk_read": 436613120, ...}
```

`footprint` and `disk_written` are non-null (and non-zero) throughout every sample in the run. **Pass.**

## V-3 — absent-path handling and pickup

```
rm -rf /tmp/cdm-nonexistent
uv run monitor.py --no-tui --support-dir /tmp/cdm-nonexistent --log logs/v3.jsonl
```

After 8 s, `bundle.present` was `false` and every entry in `paths` showed `present: false` (no path present, no errors, no exceptions in `logs/v3.err`).

While still running, created `/tmp/cdm-nonexistent/vm_bundles/claudevm.bundle/rootfs.img` (a 50 MB sparse-truncated file). Within the next samples (~10 s later, one `--full-rescan` cycle is not required since bundle files are `stat()`-checked every poll per D-2), `bundle.present` became `true`, `bundle.rows.rootfs.img.apparent` = 52428800, and `paths.vm_bundles.present` became `true`. No errors were logged or printed at any point. Process shut down cleanly on SIGTERM (`session_end` written). **Pass.**

## V-4 — extended headless run (≥ 1 h)

Started:

```
nohup uv run monitor.py --no-tui --log logs/v4.jsonl > /dev/null 2> logs/v4.err &
```

Two `SIGUSR1` markers sent: one at 12 s elapsed, one manually at 6777 s elapsed (the automated 30-min-mark watcher script had a bug — `ps -o etimes=` isn't reliable in this shell — and never fired; noticed when checking in, by which point the run had already been going 112 min, well past the 1 h requirement, so both markers and the stop were finished off manually instead). Ran for 6777.0 s (113 min) before `SIGTERM`.

Result: 2267 lines, every one valid JSON (0 parse failures). Record counts: 1 `session_start`, 1 `diag_report` (the historic Claude report, per V-5), 2260 `sample`, 2 `marker`, 2 `flag`, 1 `session_end`. Sample cadence: 2260 samples over 6777 s at the 3 s interval, every consecutive gap ≤ 3.01 s (well inside the ≤ 2× interval = 6 s bound). `logs/v4.err` contains only the startup banner and the two flag lines below — nothing else:

```
00:35:44 FLAG raised mem:gpu value=103354995.12 threshold=50.0 gpu memory 5m avg 98.57 MiB > baseline 65.58 MiB + 50%
00:38:23 FLAG cleared mem:gpu value=98892974.64 threshold=50.0 gpu memory 5m avg 94.31 MiB recovered toward baseline
```

This was a real, live-induced flag (not synthetic): the `gpu` role's memory grew past its 50%-over-baseline threshold and later recovered, both correctly raised and cleared. **Pass.**

## V-5 — startup `.diag` listing

At session start, `logs/v1v2v5v6.jsonl` contains exactly one `diag_report` record:

```json
{"type": "diag_report", "historic": true, "file": "/Library/Logs/DiagnosticReports/Claude_2026-09-25-213433_nu.diag", "coalition": {"name": "com.anthropic.claudefordesktop", "id": 11603}, ...}
```

`/Library/Logs/DiagnosticReports/com.apple.Virtualization.VirtualMachine_2026-09-25-210018_nu.diag` (Docker's coalition, `com.docker.docker`(11507)) produced no `diag_report` record — the only report seen was Claude's. Session started at 2026-09-26 00:2x +08, within the 24 h window of the 2026-09-25 21:34 report. **Pass.**

## V-6 — coalition attribution excludes Docker's VM

At the time of the checks above, two `com.apple.Virtualization.VirtualMachine` processes were running concurrently:

| PID | Coalition ID (`PROC_PIDCOALITIONINFO`) | Owner |
|---|---|---|
| 87782 | 11603 (`com.anthropic.claudefordesktop`) | Claude's Cowork VM |
| 2974 | 11507 (`com.docker.docker`) | Docker Desktop's VM |

The monitor's `processes` list for that sample contained exactly one `role: "vm"` entry, PID 87782 — Docker's PID 2974 does not appear anywhere in the sample. Cross-checked independently with `pgrep -f com.apple.Virtualization.VirtualMachine`, which listed both PIDs on the system while only one was tracked. **Pass.**
