# Plan 03: Process discovery, attribution and per-process metrics

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Execution order and rationale: [`index.md`](index.md).

## Context

Claude Desktop is an Electron app. It runs a main process plus helper processes: renderers, a GPU process, and utility services (network, Node, audio, video capture), plus a crash handler. The Cowork feature runs a Linux VM inside the macOS system process `com.apple.Virtualization.VirtualMachine`. That process has parent PID 1, so process parentage cannot identify it, and other apps (e.g. Docker Desktop) run processes with the same name.

macOS groups every process into a **resource coalition** and charges resources to it. Claude's coalition (`com.anthropic.claudefordesktop`) contains:
- the app's own processes;
- Claude's VM process;
- macOS system XPC services working for Claude: `MTLCompilerService`, `QuickLookUIService`, `com.apple.appkit.xpc.openAndSavePanelService`, `CSExattrCryptoService`.

Tracking the whole coalition makes the monitor's totals match what macOS itself charges Claude, including in its disk-write diagnostics.

Plan 02 provides, in `monitor.py` §2, `rusage(pid)` (footprint, resident, disk read/written bytes), `coalition_id(pid)` and `responsible_pid(pid)`. Each returns `None` on failure.

## Goal

A process collector (§3) that each poll yields per-process, per-role and total metrics for Claude, plus the optional Claude Code CLI group and optional `fs_usage` file-level write tracing.

## Prerequisites

Plans 01 and 02 complete.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| P-1 | Tracked set = every process in the Claude main process's resource coalition. Fallback if no coalition: executables under `Claude.app` + VM attributed per P-3/P-4. |
| P-2 | Excluded by default: Claude Code CLI, Zed's agent SDK, `Claude Usage.app`, anything merely matching "claude". `--include-cli` adds the Claude Code CLI as a separate labelled group. |
| P-3 | VM (`com.apple.Virtualization.VirtualMachine`) counts as Claude's when in Claude's coalition. |
| P-4 | Fallbacks: responsible PID is a Claude.app process; else name match with the row marked `?`. |
| P-5 | Roles: `main`, `renderer`, `gpu`, `utility:<sub-type>`, `crashpad`, `vm`, `xpc`, `other`. |
| P-6 | Re-discover every poll; appearing/disappearing processes never cause errors. |
| G-1 | Role rows aggregate their PIDs (sums). |
| G-3 | A Total Claude aggregate. |
| G-4 | Log records per-PID and per-role values. |
| C-1 | CPU as % of one core (can exceed 100%). |
| M-1 | Physical footprint (primary) and RSS per process and role. |
| I-1 | Per-process read/written bytes from `proc_pid_rusage`; rates from deltas. |
| I-4 | Unreadable counters → n/a, no error. |
| I-5 | `--trace-io`: `sudo fs_usage -w -f filesys` for tracked PIDs; top files by bytes written. Never without the flag. |

## Design

### Discovery (each poll)

1. **Find main.** Iterate `psutil.process_iter(["pid", "exe", "create_time"])`. Main = exe ending in `/Claude.app/Contents/MacOS/Claude`, where the app bundle is `/Applications/Claude.app` or `~/Applications/Claude.app`. No main → sample gets `claude.running = false`, and processes/roles/total are empty (other collectors keep running). More than one main (unusual) → use the oldest.
2. **Coalition.** `cid = coalition_id(main)`. Then, for every PID from `psutil.pids()`, look up its coalition. Cache the result keyed by `(pid, create_time)`: coalition membership is fixed for a process's lifetime, so after the first poll only new PIDs cost a syscall. Tracked = processes with that coalition ID.
3. **Fallback (cid is None).** Tracked = processes whose exe is inside the Claude.app bundle, plus `com.apple.Virtualization.VirtualMachine` processes with `responsible_pid()` equal to a Claude.app PID (`attribution: "responsible"`). If both lookups fail, name-matched VMs are included with `attribution: "name?"`.
4. **Attribution field** on every process: `"coalition" | "path" | "responsible" | "name?"`.

### Role classification (P-5)

Evaluated in order on exe path and `cmdline`:

| Condition | Role |
|---|---|
| exe == main exe | `main` |
| exe is `…/Virtualization.framework/…/com.apple.Virtualization.VirtualMachine` | `vm` |
| exe basename `chrome_crashpad_handler` | `crashpad` |
| exe inside Claude.app and `--type=renderer` | `renderer` |
| exe inside Claude.app and `--type=gpu-process` | `gpu` |
| exe inside Claude.app and `--type=utility` | `utility:<s>`, where `<s>` = the `--utility-sub-type` value up to its first `.` (`network.mojom.NetworkService` → `network`, `video_capture.mojom…` → `video_capture`); missing → `utility:unknown` |
| exe under `/System/` or `/usr/` | `xpc` |
| anything else | `other` |

The exe path comes from psutil `exe()`. If that raises `AccessDenied`, fall back to `cmdline()[0]`, then to `name()`.

### Per-process metrics

Keep a `psutil.Process` object cache keyed by `(pid, create_time)`, so CPU % deltas work and recycled PIDs are never confused.

| Field | Source | Notes |
|---|---|---|
| `cpu_pct` | `Process.cpu_percent(None)` | `None` on a process's first poll (psutil primes on the first call) |
| `footprint` | `rusage().footprint` | `None` if unreadable |
| `rss` | `rusage().resident`, else `memory_info().rss` | |
| `disk_read`, `disk_written` | `rusage()` cumulative counters | |
| `read_delta`, `write_delta` | counter − previous counter | See below |

**Delta rules:**
- First observation of a PID:
  - If it was created **after** session start: delta = full counter (its writes all happened during the session).
  - Otherwise: delta = 0, since its counter holds pre-session history. Example: Claude main had ~11.9 GB written before the probe.
- Counter decreased (should not happen): delta = 0.
- Process gone between listing and reading: skipped silently, including `psutil.NoSuchProcess`, `AccessDenied` and `ZombieProcess` (P-6).

### Aggregation (G-1, G-3)

Per role and for `total`:
- `count`
- `cpu_pct`: sum, ignoring `None`
- `footprint`, `rss`: sums
- `read_delta`, `write_delta`: sums
- `read_rate`, `write_rate`: `*_delta / dt`, where `dt` is the actual monotonic time since the previous poll

Plan 06 computes rolling averages and flags from these.

### Claude Code CLI group (P-2, `--include-cli`)

- Only when the flag is set.
- Members: processes **not** in Claude's coalition whose exe basename is `claude`, excluding any path containing `claude-agent-sdk` (Zed's embedded agent). Other name matches such as `Claude Usage.app` are never included.
- Reported under `cli` with its own per-process list and aggregate, role `cli`.
- **Not** added to `total`, so Total Claude always means Claude Desktop.

### `--trace-io` (I-5)

- At startup, before any TUI, run `sudo -v`. The user types their password in the normal terminal. If it fails, exit with a clear message. Default mode never calls sudo (E-6).
- Spawn `sudo -n fs_usage -w -f filesys <pid> <pid> …` for the tracked PIDs, stdout piped and read by a background thread.
- Restart the subprocess when the tracked PID set changes, at most once per 30 s.
- Parse lines defensively; the `fs_usage` output format is undocumented:
  - Write-family calls: `write`, `pwrite`, `writev`, `pwritev`, `WrData*`.
  - Extract the byte count from `B=0x<hex>` and take the file path as the path column.
  - Count lines that don't parse as `unparsed`.
- Keep a 60 s sliding window of `(time, path, bytes)`. Each poll the sample gets `trace_io = {"active": true, "top": [{"path", "bytes"} × 10], "unparsed": n}`.
- On exit, terminate the subprocess (SIGTERM, then SIGKILL after 2 s).

### Sample fragment

```json
"claude": {"running": true, "main_pid": 87512, "coalition_id": 11603, "app_path": "/Applications/Claude.app"},
"processes": [{"pid": 87782, "role": "vm", "exe": "...", "attribution": "coalition",
               "cpu_pct": 12.5, "footprint": 1129316352, "rss": 1227760000,
               "disk_read": 0, "disk_written": 137363456, "read_delta": 0, "write_delta": 4096}],
"roles": {"vm": {"count": 1, "cpu_pct": 12.5, "footprint": ..., "rss": ..., "read_delta": ..., "write_delta": ..., "read_rate": ..., "write_rate": ...}, ...},
"total": { same fields },
"cli": {"processes": [...], "aggregate": {...}},   // only with --include-cli
"trace_io": {...}                                  // only with --trace-io
```

## Tasks

1. §3: `ProcessCollector` with the discovery, coalition cache, classification, metrics, deltas and aggregation above.
2. Fallback path (no coalition) with attribution labels.
3. `--include-cli` group.
4. `--trace-io`: `sudo -v` preflight in main (before the renderer starts), `FsUsageTracer` thread, parser, restart and debounce, cleanup.
5. `tests/test_processes.py`:
   - Role classification on recorded cmdlines (from the reference machine).
   - `--utility-sub-type` parsing.
   - Delta rules: new-after-start, pre-existing, counter reset.
   - Aggregation sums.
   - CLI-group exclusion of `claude-agent-sdk` paths.
   - `fs_usage` line parser on sample lines, including unparsable ones.

## Verification (done when)

- [ ] With Claude Desktop (Cowork VM up) and Docker Desktop running, a 30 s headless log shows:
  - roles `main`, `renderer`, `gpu`, `utility:network`, `utility:node`, `crashpad`, `vm`, `xpc`;
  - the VM with `attribution: "coalition"`;
  - Docker's VM PID absent (V-6);
  - `total` equal to the sum of the roles.
- [ ] VM footprint within ~5% of Activity Monitor's "Memory" column for that process.
- [ ] Quit Claude Desktop mid-run: `claude.running` becomes false with no exception. Relaunch: processes rediscovered.
- [ ] `--include-cli` shows the Claude Code CLI under `cli`, not Zed's agent, and `total` is unchanged.
- [ ] `--trace-io` prompts for sudo before starting and lists top written files (e.g. under `vm_bundles/`) during Cowork activity. Without the flag, no sudo prompt ever appears.
- [ ] `uv run pytest` passes. Committed.

## Plan-level decisions (not specified in REQUIREMENTS.md)

- Claude.app is accepted in `/Applications` or `~/Applications`.
- Processes that exist at session start only contribute writes made after the start; processes created later contribute all their writes.
- The CLI group is excluded from Total Claude.
- `fs_usage` restarts are debounced to once per 30 s; top-10 list over a 60 s window.
- Processes classified `other` still count in totals; the TUI (Plan 07) shows their exe basename.

## Out of scope

Rolling averages, baselines, thresholds and flags (Plan 06); display (Plan 07).
