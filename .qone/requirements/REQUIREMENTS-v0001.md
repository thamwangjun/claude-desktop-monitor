# REQUIREMENTS v0001 — Startup log cleanup
Status: Approved · Date: 2026-09-28

## Summary
At the start of every monitoring session, claude-desktop-monitor deletes its own session logs that are more than 3 days (72 h) old from its default log directory, so the directory stops growing without bound. Each deletion is recorded in the new session's log. Nothing else is touched.
Features: F1 Startup log cleanup

## F1 — Startup log cleanup

### Current behaviour
No log deletion exists anywhere in the tool.
- Default log directory `~/Library/Logs/claude-desktop-monitor/` is `DEFAULT_LOG_DIR` (`src/claude_desktop_monitor/monitor.py:47`).
- The default log path `monitor-<ts>.jsonl` (local time, `YYYYmmdd-HHMMSS`) is built in `parse_args` (`monitor.py:166-168`); `--log PATH` overrides it (`monitor.py:132`).
- `LogWriter` creates the parent directory and appends to the file (`monitor.py:1942-1946`).
- `run()` opens the log and then writes `session_start` (`monitor.py:2948-2985`).
- `main()` returns before any logging for `--notify-test` (`monitor.py:3083`) and for a failed `--trace-io` sudo preflight (`monitor.py:3085`).

### Requirements

#### REQ-0001-F1-01 Cleanup once per monitoring session
When a monitoring session starts (TUI or headless, with or without `--log`), the system shall run the log cleanup exactly once, before the first sample is collected.
**Acceptance criteria**
- AC1: A headless run with default logging and one eligible file older than 72 h → that file is gone before the first `sample` record is written to the new log.
- AC2: A TUI run with `--log /tmp/x/test.jsonl` → the cleanup still runs against the default log directory (one `log_cleanup` record in `/tmp/x/test.jsonl`).
- AC3: A session that runs for 5 days performs the cleanup once only; files that pass 72 h during the session are not deleted until the next session starts.

#### REQ-0001-F1-02 Cleanup skipped outside monitoring sessions
If the invocation is `--notify-test`, `--help`, has an argument error, or fails the `--trace-io` sudo preflight, then the system shall not run the log cleanup.
**Acceptance criteria**
- AC1: `claude-desktop-monitor --notify-test` with an eligible 4-day-old file in the default directory → the file still exists afterwards.
- AC2: `claude-desktop-monitor --help` and `claude-desktop-monitor --bogus` → no file in the default directory is deleted.
- AC3: `--trace-io` with the sudo preflight failing → no file in the default directory is deleted.

#### REQ-0001-F1-03 Fixed cleanup directory
The system shall scan only `~/Library/Logs/claude-desktop-monitor/` for cleanup, regardless of the `--log` value.
**Acceptance criteria**
- AC1: `--log /tmp/x/monitor-20260901-000000.jsonl` with a 10-day-old `/tmp/x/monitor-20260801-000000.jsonl` → the `/tmp/x` file still exists.
- AC2: A 10-day-old `./logs/monitor-20260801-000000.jsonl` (legacy location) → still exists after a session starts from that working directory.
- AC3: `~/Library/Logs/Claude/`, `~/Library/Application Support/Claude/` and `/Library/Logs/DiagnosticReports/` are never scanned and have nothing deleted from them.

#### REQ-0001-F1-04 Eligible file names
The system shall consider for deletion only top-level regular files in the cleanup directory whose names match `monitor-YYYYmmdd-HHMMSS.jsonl` (8 digits, `-`, 6 digits).
**Acceptance criteria**
- AC1: Files named `notes.txt`, `monitor-20260901.jsonl`, `monitor-20260901-000000.jsonl.bak` and `other-20260901-000000.jsonl`, each 10 days old → all still exist.
- AC2: A subdirectory `old/` holding a 10-day-old `monitor-20260801-000000.jsonl` → the subdirectory and its contents still exist.
- AC3: A symlink named `monitor-20260801-000000.jsonl` with a 10-day-old mtime → neither the symlink nor its target is deleted.
- AC4: A 10-day-old regular file `monitor-20260801-000000.jsonl` → deleted.

#### REQ-0001-F1-05 Age threshold
The system shall delete an eligible file when its age, the time at the cleanup minus the file's last-modified time (mtime), is strictly greater than 259,200 seconds (72 h).
**Acceptance criteria**
- AC1: mtime 72 h + 1 s ago → deleted.
- AC2: mtime exactly 72 h ago → kept.
- AC3: mtime 71 h 59 min ago → kept.
- AC4: A file whose filename timestamp is 10 days old but whose mtime is 1 minute ago (a long-running session still writing) → kept.
- AC5: mtime in the future → kept.
- AC6: The threshold is fixed. No CLI flag changes or disables it, and `--help` lists no new option.

#### REQ-0001-F1-06 Cleanup never blocks monitoring
If the cleanup directory is missing or unreadable, or a file cannot be deleted, then the system shall continue the cleanup with the remaining files and start monitoring normally.
**Acceptance criteria**
- AC1: The default directory does not exist and `--log` points elsewhere → the session starts, and `log_cleanup` has empty `deleted` and `failed`.
- AC2: An eligible file that vanishes between listing and deletion → skipped, and appears in neither `deleted` nor `failed`.
- AC3: An eligible file that cannot be deleted (permission error) → the session starts, the other eligible files are still deleted, and the file appears in `failed` with its error text.
- AC4: The directory exists but cannot be listed → the session starts, and `failed` has one entry for the directory with its error text.

#### REQ-0001-F1-07 `log_cleanup` record
When the cleanup has run, the system shall write one `log_cleanup` record to the session log immediately after `session_start`, with fields `dir` (the cleanup directory path), `max_age_s` (259200), `deleted` (the list of deleted file names) and `failed` (a list of `{file, error}` objects).
**Acceptance criteria**
- AC1: Nothing eligible → the second record is `{"type": "log_cleanup", "dir": "<path>", "max_age_s": 259200, "deleted": [], "failed": []}`, plus the `ts` and `elapsed` fields every record carries.
- AC2: Two files deleted → `deleted` contains exactly those two file names (names only, not full paths).
- AC3: The record appears exactly once per session and is always the record directly after `session_start`.
- AC4: The `session_start` record and every other existing record type are unchanged.

#### REQ-0001-F1-08 No on-screen output
The system shall not display any message about the cleanup in the TUI, on stdout or on stderr.
**Acceptance criteria**
- AC1: A headless run in which 3 files are deleted and 1 fails → stderr and stdout contain no cleanup text.
- AC2: A TUI run in which files are deleted → no banner, header text or warning mentions the cleanup, and no macOS notification is sent for it.

#### REQ-0001-F1-09 README documentation
The README shall describe the startup cleanup next to the default log location and list `log_cleanup` among the log record types.
**Acceptance criteria**
- AC1: The README states that logs in `~/Library/Logs/claude-desktop-monitor/` more than 3 days old are deleted at startup, and that `--log` locations elsewhere and legacy `./logs/` are never touched.
- AC2: The README's log-record description includes `log_cleanup` and its fields.
- AC3: The README still cites only the 9 approved issues (v1 §11).

### Carried forward
- L-1 (v1, amended v3): one JSONL file per session named `monitor-YYYYmmdd-HHMMSS.jsonl` in `~/Library/Logs/claude-desktop-monitor/`, with `--log PATH` as the override. This naming is what REQ-0001-F1-04 matches, and the directory is what REQ-0001-F1-03 scans. Legacy `./logs/` is still not moved, and now also not cleaned.
- L-2 (v1) and L-8 (v3): the set of log record types gains `log_cleanup` (additive); existing records, including `session_start`, are unchanged.
- v1 §2 "no auto-remediation" and the v1 hard rules (never modify `~/Library/Logs/Claude/`, `~/Library/Application Support/Claude/` or `/Library/Logs/DiagnosticReports/`): still hold. The cleanup only deletes the monitor's own logs.
- V-13 (v3), V-20 (v3.1): tests never write to, or delete from, the real `~/Library/Logs/claude-desktop-monitor/`.
- W-1 (v3), v1 §11: the README update keeps the 9-citation rule.

## Out of scope
- F1: Periodic or continuous cleanup while a session is running.
- F1: Size-based caps, file-count caps, compression or archiving of logs.
- F1: A CLI flag or config setting to change or disable the 72 h threshold.
- F1: Cleaning the directory of a `--log` path, or the legacy `./logs/` directory.
- F1: Deleting anything other than top-level `monitor-YYYYmmdd-HHMMSS.jsonl` regular files (subdirectories, symlinks, other names).
- F1: On-screen or notification reporting of the cleanup.
- F1: Any change to Claude's own directories (still read-only).

## Decisions
| # | Feature | Grey area | Resolution |
|---|---------|-----------|------------|
| 1 | All | Feature list | Single feature F1 Startup log cleanup (delete old monitor logs once at startup); periodic cleanup, size caps and compression out of scope |
| 2 | F1 | Which directory is cleaned, and the safety guard for protected dirs | Only `~/Library/Logs/claude-desktop-monitor/`, regardless of `--log`; legacy `./logs/` and any `--log` parent are never scanned |
| 3 | F1 | Which files are eligible | Only top-level regular files named `monitor-YYYYmmdd-HHMMSS.jsonl`; subdirectories, symlinks and other files never touched |
| 4 | F1 | What "age" means: mtime vs filename timestamp | Age = now minus file mtime; a still-running session writing every poll is never deleted |
| 5 | F1 | Threshold boundary: rolling 72 h vs calendar days; `>` vs `≥` | Rolling: delete when age is strictly more than 72 h (259,200 s); exactly 72 h kept |
| 6 | F1 | Fixed 3 days vs configurable/disableable | Fixed 72 h, no new CLI flag, cannot be disabled |
| 7 | F1 | Which start paths run it, and when | Once per monitoring session (TUI or headless, with or without `--log`), before the first sample; not for `--notify-test`, `--help`, argument errors, or failed `--trace-io` sudo preflight |
| 8 | F1 | Failure handling | Monitoring always starts. Missing dir = nothing to do; vanished file = skipped silently; undeletable file or unreadable dir = skipped, path + error recorded; no on-screen warning |
| 9 | F1 | Observability of deletions | New `log_cleanup` record right after `session_start` in every monitoring session (even when nothing deleted): `dir`, `max_age_s` 259200, `deleted` [file names], `failed` [{file, error}]; nothing on screen |
| 10 | Cross-feature | README documentation | Yes: short note next to the default log location (3-day startup deletion, `--log` elsewhere and `./logs/` untouched) and `log_cleanup` in the log record description; 9-issue rule unchanged |
