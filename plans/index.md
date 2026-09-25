# Implementation plans: index

How and why the Claude Desktop Resource Monitor is built in eight plans.

## The project in brief

`monitor.py` is a macOS terminal tool that watches Claude Desktop's resource usage over time, with emphasis on the **Cowork** feature's local Linux VM (Apple Virtualization.framework). Public issue reports describe **gradual degradation**:
- the VM disk image growing without bound;
- CPU creeping up while idle;
- memory growth and swapping;
- sustained disk writes that trip macOS's own disk-write budgets (2 GiB and 8 GiB per rolling 24 h).

The tool samples processes, disk footprint, swap and macOS diagnostic reports every few seconds. It logs everything to JSONL, flags anomalies, and shows a live dashboard. It is observability only: it never modifies or deletes anything.

- Single Python script, run with `uv run monitor.py`.
- Tool versions pinned by mise: Python 3.14.7, uv 0.12.18.
- Runtime dependencies: `psutil`, `rich`, `watchdog`.

The authoritative specification is [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Every plan restates the requirements it covers, so each can be read on its own.

## The plans

| # | Plan | Delivers | Checkpoint (done when) |
|---|---|---|---|
| 01 | [Scaffold and core loop](01-scaffold.md) | mise/uv project, `monitor.py` skeleton with section layout and data contract, all CLI flags, poll loop, JSONL log, headless output, signal handling | Headless run writes a well-formed session log |
| 02 | [macOS native layer](02-native-layer.md) | ctypes calls: `proc_pid_rusage` (disk bytes, footprint), resource coalition ID, responsible PID | Tests confirm values for Claude's VM and exclusion of Docker's VM |
| 03 | [Processes](03-processes.md) | Coalition-based discovery, role classification, VM attribution, per-process/role/total CPU, memory and I/O; `--include-cli`; `--trace-io` | Log shows correct roles; Claude's VM tracked, Docker's not |
| 04 | [Disk footprint](04-disk-footprint.md) | VM bundle file sizes each poll (apparent and allocated), directory sizes via FSEvents with incremental re-walks, absent paths, `--support-dir` | Sizes match `du`; absent → present handled |
| 05 | [System signals](05-system-signals.md) | True swap counters (`vm_stat`), live watch and parse of macOS `.diag` reports | Startup finds the Claude report and ignores Docker's |
| 06 | [Analysis and alerts](06-analysis-alerts.md) | Deltas, peaks, rolling windows, baselines, 24 h write budget, every alert rule, flag state machine | Unit tests per rule; live induced flags |
| 07 | [TUI](07-tui.md) | `rich` live dashboard, highlighting, keys `q`/`p`/`m`/`b`, terminal safety | Interactive checklist passes |
| 08 | [README and verification](08-readme-verification.md) | README with metric → issue map (9 citations) and interpretation guide; full verification V-1 to V-6 | Results recorded and passing |

## How the plans are executed

### Order and dependencies

```
01 Scaffold ──┬──> 02 Native ──> 03 Processes ──┐
              ├──> 04 Disk ─────────────────────┼──> 06 Analysis ──> 07 TUI ──> 08 README & verification
              └──> 05 Signals ──────────────────┘
```

- **01 first.** Everything plugs into its section layout, component contracts and sample data contract.
- **02 → 03.** Process attribution and I/O depend on the native calls.
- **02, 04 and 05 are independent of each other** after 01, and may be done in any order or in parallel (e.g. separate worktrees), because each edits its own `monitor.py` section.
- **06 needs the output shapes of 03–05.** Its tests use synthetic samples, so it can start once those shapes are fixed, but live verification needs the collectors.
- **07 needs 06.** It displays `derived` values and flags.
- **08 last.** It documents and verifies the finished tool.

Default sequential order: **01 → 02 → 03 → 04 → 05 → 06 → 07 → 08**.

### Per-plan workflow

For each plan:
1. Read the plan in full; it lists its prerequisites, requirements, design, tasks and checks.
2. Implement only within the plan's scope and its `monitor.py` section(s). A defect found in an earlier plan's section is fixed there and noted in the commit message.
3. Run the plan's **Verification (done when)** checklist, including `uv run pytest` for the whole suite (no regressions).
4. Commit, one commit per plan (more if natural), message prefixed `plan NN:`.
5. **Checkpoint with the user**: report what was done, the verification results, and any deviation from the plan before starting the next plan.

A plan is not complete until every checkbox in its verification list is ticked. If a check cannot pass as specified, stop and raise it with the user rather than weakening the check.

### Shared conventions (defined in Plan 01, used everywhere)

- **Section layout of `monitor.py`**: §1 constants/CLI, §2 native, §3 processes, §4 disk, §5 signals, §6 analysis, §7 log writer, §8 renderers, §9 main loop.
- **Units**: bytes, bytes/s, % of one core, seconds. Binary units (KiB/MiB/GiB) for display.
- **Contracts**: collectors `collect(now) -> dict`; analyzer `analyze(sample) -> (derived, flag_events)`; renderer `start/render/on_flag/poll_keys/stop`.
- **Log**: JSONL with record types `session_start`, `sample`, `flag`, `marker`, `diag_report`, `session_end`; flushed per record.
- **Errors**: collectors never raise on vanished processes, missing paths or unreadable data; they report `None`/`absent`/`n/a`.
- **Tests**: `pytest` (dev-only dependency), in `tests/`. The application stays one file.

## Why the work is split this way

1. **Riskiest first.** The only parts resting on undocumented behaviour are the private macOS calls: resource coalition, responsible PID, and `proc_pid_rusage` on the VM process. They are isolated in Plan 02, right after the scaffold, so a failure would be found before anything is built on them.

   A probe on 2026-09-25 already confirmed all three work without root on the reference machine. It also showed that Claude's coalition includes macOS XPC helper services. That led to the coalition-wide tracked set (REQUIREMENTS P-1) and the `xpc` role.
2. **Data collection separate from judgment.** Collectors (03–05) only measure; Plan 06 alone decides what is anomalous. Alert rules can then be unit-tested with synthetic samples: a 1 GB bundle growth, a 24 h write budget or a 5-minute memory trend cannot be produced on demand.
3. **Function before presentation.** Headless mode and the JSONL log carry all the data from Plan 01 onwards. The TUI (07) comes last, on a settled data model, so it is built once rather than reworked as each metric is added.
4. **Independent collectors.** Processes, disk and signals touch different system facilities and different script sections, so they can be built and verified without waiting on each other.
5. **Every plan ends runnable.** Each plan finishes with an observable check against the real Claude Desktop, so problems surface at the plan that caused them.

The alternative, vertical slices (one metric end-to-end, including TUI, per plan), was considered and rejected: it shows a dashboard sooner, but reworks the TUI and alert structure with each slice and mixes test concerns.

## Requirement traceability

| Requirement IDs | Plan |
|---|---|
| E-1 – E-6, T-1, L-1 – L-4, H-1, H-2, §6 (flag definitions) | 01 |
| I-1 (native call), M-1 (footprint source), P-3/P-4 (lookups), V-2 (early) | 02 |
| P-1 – P-6, G-1, G-3, G-4, C-1, M-1, I-1, I-4, I-5 | 03 |
| D-1 – D-9 (D-3/D-8 detection), T-2, T-3, L-5, V-3 (early) | 04 |
| M-2, X-1 – X-5, L-6, V-5 (early) | 05 |
| C-2 – C-5, M-3, M-4, I-2, I-3, D-3/D-8/D-7 (derivation and flags), §5 | 06 |
| U-1 – U-4, G-2, I-6, K-1 – K-4 | 07 |
| §8 deliverables, §11 citations, V-1 – V-6 | 08 |

I-5 (`--trace-io`) was not named in the original eight-plan outline; it is assigned to Plan 03 (process I/O).

## Plan-level decisions for review

These choices are made in the plans but are **not** specified in REQUIREMENTS.md. Review them before or during execution. Each plan lists its own.

| Plan | Decision |
|---|---|
| 01 | pytest as a dev-only dependency; tests in `tests/` |
| 01 | `session_end` record type; SIGUSR1 writes a marker (headless marking); missed ticks skipped |
| 01 | Size flags accept decimal (KB/MB/GB) and binary (KiB/MiB/GiB) suffixes |
| 02 | `rusage_info_v2` struct version; jetsam coalition ignored |
| 03 | Claude.app accepted in `/Applications` or `~/Applications` |
| 03 | Pre-existing processes contribute only post-start writes; new processes contribute all writes |
| 03 | `--include-cli` group excluded from Total Claude; Zed's `claude-agent-sdk` excluded by path |
| 03 | `fs_usage` restart debounce 30 s; top 10 files over 60 s |
| 04 | Scan units = children of `support/` and of `vm_bundles/`; hard links de-duplicated, APFS clones not |
| 04 | Extra log fields `scan_ms`, `files`, `last_scan_elapsed`; `.partial` search covers all of `vm_bundles/` |
| 05 | `pageins`/`pageouts` logged for context only; incomplete `.diag` retried for 2 polls; only `/Library/Logs/DiagnosticReports/` watched |
| 06 | 10% clear hysteresis; swap streak clears on the first flat poll |
| 06 | Baselines update only when a window is full; 32 MiB floor for memory growth |
| 06 | Event flags visible for 10 min; bundle rules use the `claudevm.bundle` total; start value 0 if absent at start; `reset()` scope |
| 07 | One panel per metric family; two columns at ≥ 160 cols; red/yellow/dim only; polling pauses during `m`; keys disabled without a TTY stdin |
| 08 | V-4 "extended" = ≥ 1 h; results in `08-verification-results.md`; `jq`/pandas snippets; #51913 cited only after approval |

## Requirements changes made while planning (2026-09-25)

Planning probes changed REQUIREMENTS.md in two places. Both are already applied there:

- **P-1, P-3, P-5:** the tracked set is the whole Claude resource coalition, including macOS XPC helpers as role `xpc`. This came from the native-API probe and was approved by the user.
- **M-2:** swap-ins and swap-outs now come from `vm_stat` `Swapins`/`Swapouts`, because psutil's `sin`/`sout` on macOS are file page-ins and page-outs, not swap. Without this change, the M-3 swap-streak flag would fire on ordinary file I/O.

## Status

| Plan | Status |
|---|---|
| 01 | Done (5681d43) — added `conftest.py` at repo root (not listed in Plan 01's file table) so `uv run pytest` can `import monitor` without packaging the script; no other deviations |
| 02 | Done (84efa5c) — no deviations; live tests confirmed Claude's Cowork VM (coalition 11603) matched and Docker's VM (coalition 11507) excluded on this run |
| 03 | Done (4e0d673) — deviation: the CLI-group name match (`is_cli_process`) accepts both `claude` and `claude.exe` basenames, because this machine's mise/npm global install shims the Claude Code CLI as `claude.exe`; the plan assumed a bare `claude` basename. `--trace-io` line parsing (`parse_fs_usage_line`) splits fs_usage columns on runs of 2+ spaces rather than plain whitespace, since paths under the support dir contain single embedded spaces (e.g. "Application Support"). Not yet live-verified: `--trace-io` (needs user sudo) and quit/relaunch mid-run (needs user, per Hard rules never to quit Claude Desktop). |
| 04 | Done (pending commit) — no deviations. Fixed a real bug found while testing: `DiskCollector` now resolves `--support-dir` (`Path.resolve()`) before comparing it against watchdog event paths, because macOS resolves symlinked prefixes (e.g. `/tmp` → `/private/tmp`) in FSEvents paths but not in the raw CLI argument; without this every event was silently dropped (`relative_to` raised and was swallowed). Not yet live-verified: "during a Cowork task, only affected units show fresh `last_scan_elapsed`" (needs a live Cowork task, per §9). |
| 05 | Not started |
| 06 | Not started |
| 07 | Not started |
| 08 | Not started |
