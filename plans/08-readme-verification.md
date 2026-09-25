# Plan 08: README and end-to-end verification

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Execution order and rationale: [`index.md`](index.md).

## Context

The monitor exists to help someone decide whether Claude Desktop, and specifically the Cowork feature's local VM, is degrading their Mac over time. The README must connect each on-screen number to the failure modes reported publicly, and explain how to read a trend as a signal.

This plan also runs the full verification checklist from REQUIREMENTS §9 against the real app, and records the results.

## Goal

A complete `README.md` and a recorded, passing verification pass over V-1 to V-6.

## Prerequisites

Plans 01–07 complete. Claude Desktop with Cowork's VM downloaded (the reference machine has it). Docker Desktop available, to exercise the attribution check.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| §8.3 | README: install and run (mise + uv); what each metric maps to in the known issue reports, with links; how to interpret sustained growth, CPU climb and swap-out streaks; alert rules and CLI flags; where to look next when a flag fires. |
| §11 | Cite the 9 approved issues, with open/closed state. |
| V-1 | Verified live against a real Cowork VM (bundle present, VM running, Docker's VM concurrently running). |
| V-2 | `proc_pid_rusage` readable for the VM process without root. |
| V-3 | Absent-path case via `--support-dir /tmp/nonexistent`, then directories created while running are picked up. |
| V-4 | An extended headless run produces a well-formed JSONL log (every line parses; markers present). |
| V-5 | Startup listing finds the existing Claude `.diag` report and ignores the Docker-coalition one. |
| V-6 | Coalition attribution selects Claude's VM and excludes Docker's concurrently running VM. |

## README outline

1. **What this is**: one paragraph; observability only, never modifies or deletes anything.
2. **Requirements**: macOS, Claude Desktop, mise. Tested on Apple M5, macOS 26.7, Claude Desktop 2.9939.2.
3. **Install and run**:
   ```
   mise install          # Python 3.14.7, uv 0.12.18
   uv sync
   uv run monitor.py     # or: mise run monitor
   uv run monitor.py --no-tui    # headless, e.g. under nohup or tmux for overnight runs
   ```
4. **The screen, panel by panel**: what each column means. CPU is % of one core. Footprint vs RSS. Apparent vs allocated size. `n/a`, `…`, `?`, `absent`.
5. **Metric → known issue map**: one table, each row = metric, what normal looks like, what the reported failure looks like, and the citation. Uses exactly the approved list:

   | Issue | State | Metric |
   |---|---|---|
   | [#22543](https://github.com/anthropics/claude-code/issues/22543): Cowork 10GB VM bundle degrades performance | Open | bundle size, CPU climb (idle 24% → 55%), swap-ins, Cache/Code Cache |
   | [#65577](https://github.com/anthropics/claude-code/issues/65577): rootfs.img grows unboundedly, never reclaimed | Open | bundle growth, `sessiondata.img` |
   | [#37860](https://github.com/anthropics/claude-code/issues/37860): VM root partition 86% full on fresh image | Closed | `sessiondata.img` growth |
   | [#89869](https://github.com/anthropics/claude-code/issues/89869): provisioning writes 10.7 GB in 13 min | Open | write rate, 2 GiB/8 GiB budgets, `rootfs.img.zst` kept, non-sparse rootfs, `*.partial` |
   | [#55242](https://github.com/anthropics/claude-code/issues/55242): disk-write watchdog during long sessions | Closed | rolling 24 h budget |
   | [#43390](https://github.com/anthropics/claude-code/issues/43390): 8–13 GB written in under an hour | Closed | write rate, `vm_bundles/warm/`, directory tracking |
   | [#26194](https://github.com/anthropics/claude-code/issues/26194): VM process spins CPU indefinitely | Closed | `vm` CPU (~316%) |
   | [#87794](https://github.com/anthropics/claude-code/issues/87794): VM fails to boot, 120% CPU nonstop | Closed | `vm` CPU at idle |
   | [#30972](https://github.com/anthropics/claude-code/issues/30972): VM loads at app launch without opening Cowork | Closed | `vm` footprint (~1.87 GB) |

6. **Reading the signals**: interpretation guidance.
   - **Sustained growth**: the bundle's allocated size rising across sessions and never falling points to the no-trim pattern (#65577).
   - **`sessiondata.img` rising per session**: accumulating per-session data (#37860).
   - **CPU climb**: the baseline stays flat while the 60 s average creeps up at idle (#22543). The `vm` role pinned above 100% at idle points to a stuck boot or a spin loop (#26194, #87794).
   - **Swap-out streak**: real memory pressure, not the file paging psutil misreports.
   - **Write budget**: approaching 80% of 2 GiB predicts a macOS disk-writes report. Compare with the `.diag` reports listed in the flags panel.
   - **Memory growth over baseline**: the suspected leak pattern.
   - **One spike versus a trend**: why the log matters, and how to use markers (`m`) around Cowork tasks.
7. **Alert rules and CLI flags**: copied from REQUIREMENTS §5 and §6, with defaults.
8. **When a flag fires, look here next**:
   - `/Library/Logs/DiagnosticReports/Claude_*.diag`: the `Event`, `Writes`, `Action taken` lines.
   - `~/Library/Logs/Claude/`: `cowork_vm_swift.log`, `cowork_vm_node.log`, `coworkd.log`, `vzgvisor.log`, `main.log`.
     - `guest_vsock_connect started` with no matching `completed` means the VM is stalled at boot (#87794).
     - A download message repeating every few seconds means a re-download loop.
   - `--trace-io` (sudo) to see which files are being written.
9. **Reviewing a session log**: the JSONL schema summary (record types, units) and short `jq` and pandas examples:
   - flags timeline: `jq -c 'select(.type=="flag")'`;
   - bundle allocated size over time;
   - total CPU over time: `pandas.read_json(path, lines=True)`.
10. **Known limitations**:
    - Private macOS APIs (coalition, responsible PID) may change.
    - Writes before the session started are invisible to the 24 h budget.
    - APFS clones are counted per file.
    - `fs_usage` parsing is best-effort.
    - psutil's `sin`/`sout` are not swap on macOS (why `vm_stat` is used).
11. **Non-goals**: no remediation, no VM modification.

**#51913 citation gate.** REQUIREMENTS §8 mentions #51913 (a re-download loop) in the "look here next" guidance. #51913 is **not** in the approved citation list (§11). Before the README cites it, ask the user to approve it. Until then, describe the pattern without a link.

## Verification procedure

Record results in `plans/08-verification-results.md`: date, machine, Claude version, command, observed outcome, pass/fail, and log file names.

| Check | Procedure | Pass criterion |
|---|---|---|
| V-1 | Start Docker Desktop (its VM running) and Claude Desktop with Cowork. Run the TUI for 10 min and run one Cowork task, marked with `m` before and after | VM row present with `coalition` attribution; bundle rows populated; flags behave per Plan 06 |
| V-2 | Inspect the log: the `vm` process has non-null `footprint` and `disk_written` | Non-null throughout |
| V-3 | `uv run monitor.py --no-tui --support-dir /tmp/cdm-nonexistent`, then create `vm_bundles/claudevm.bundle/` with a sparse file while running | No errors; paths go from absent to present within the next samples |
| V-4 | `nohup uv run monitor.py --no-tui > /dev/null 2> run.err &` for **≥ 1 hour**; send `kill -USR1 <pid>` at least twice (headless marker, Plan 01) | Every line parses; ≥ 2 `marker` records; `session_start`, samples at the expected cadence (gaps ≤ 2× interval), `session_end` after `kill -TERM`; `run.err` contains only flag lines |
| V-5 | Start within 24 h of a Claude `.diag` report (or use the fixture test from Plan 05 if none is recent) | Historic `diag_report` for the Claude report; none for the Docker-coalition report |
| V-6 | During V-1, list `com.apple.Virtualization.VirtualMachine` PIDs (`pgrep -f com.apple.Virtualization.VirtualMachine`) and compare with the log | Exactly Claude's VM PID tracked; Docker's absent |

Also run `uv run pytest` (whole suite) and a fresh-clone install (`git clone … && mise install && uv sync && uv run monitor.py --no-tui` for 30 s).

## Tasks

1. Write `README.md` per the outline. Check every CLI flag and default against `monitor.py --help`.
2. Ask the user about the #51913 citation.
3. Run V-1 to V-6, the fresh-clone check and pytest; record them in `plans/08-verification-results.md`.
4. Fix any defects found (in the relevant plan's section) and re-run the affected checks.
5. Commit README, results and fixes.

## Verification (done when)

- [ ] README covers all 11 outline sections; the flags table matches `--help` exactly.
- [ ] All 9 approved citations present, with state; no unapproved links.
- [ ] `plans/08-verification-results.md` records a pass for V-1 to V-6, the fresh-clone check and pytest.
- [ ] Committed.

## Plan-level decisions (not specified in REQUIREMENTS.md)

- An "extended period" for V-4 = at least 1 hour.
- Verification results are recorded in `plans/08-verification-results.md`.
- `jq`/pandas review snippets are included in the README.
- #51913 is cited only after user approval.

## Out of scope

New features. Anything found missing becomes a follow-up item for the user, not scope creep.
