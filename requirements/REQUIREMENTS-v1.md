# Claude Desktop Resource Monitor — Requirements

Status: agreed requirements, pre-implementation (2026-09-25; revised after issue research R-1 to R-5 and the native API probe).

## 1. Purpose

A terminal UI (TUI) diagnostic tool that monitors Claude Desktop's resource usage on macOS, with emphasis on the failure modes reported for the **Cowork** feature, which runs a local Linux VM via Apple's Virtualization.framework.

The reported problems are **degradation over time**, not single-snapshot spikes:

- VM bundle disk footprint grows monotonically with no trim/GC.
- CPU climbs from a low idle baseline over minutes of idle time.
- Memory pressure leads to sustained swapping.
- Sustained disk writes trip macOS's own disk-write budgets (2 GiB and 8 GiB per rolling 24 h), producing `disk writes` diagnostic reports.

The tool is **observability only**: it flags anomalies and never acts on them.

## 2. Non-goals

- No reverse-engineering or modification of Claude Desktop or its VM.
- No auto-remediation (e.g. never deletes `vm_bundles/` or caches).
- Not a shipped product: minimal dependencies, no build step.
- No parsing of Claude's own log files (`~/Library/Logs/Claude/`): formats are undocumented and version-dependent. The README points to them instead.

## 3. Reference environment (observed facts the design relies on)

Observed on the development machine (Apple M5, 32 GB RAM, macOS 26.7, Claude Desktop 2.9939.2):

**Support directory**: `~/Library/Application Support/Claude/`

**VM bundle after a fresh Cowork first run** (`vm_bundles/claudevm.bundle/`, ~11 GB total):

| File | Apparent | Allocated | Notes |
|---|---|---|---|
| `rootfs.img` | 10 GiB | ~10 GiB | Fully allocated (not sparse) after decompression |
| `rootfs.img.zst` | ~1.17 GiB | ~1.17 GiB | Compressed download, retained |
| `sessiondata.img` | 4 MB → 29 MB within seconds | ~half of apparent | Sparse; actively growing |
| `initrd`, `initrd-micro`, `vmlinuz` (+ `.zst`) | ~130 MB combined | | |
| dotfiles `.*.origin`, `.cowork-adopted`; `gvisorMacAddress`, `machineIdentifier`, `vmIP` | tiny | | Metadata |

**Processes**:

- Main: `/Applications/Claude.app/Contents/MacOS/Claude`
- Helpers: `Claude Helper.app` / `Claude Helper (Renderer).app` with `--type=renderer`, `--type=gpu-process`, `--type=utility --utility-sub-type=<network.mojom.NetworkService | node.mojom.NodeService | audio.mojom.AudioService | video_capture.mojom.VideoCaptureService | …>`; `chrome_crashpad_handler`. Multiple instances of a role (e.g. two renderers, two NodeServices) are normal.
- VM: `com.apple.Virtualization.VirtualMachine` (XPC service inside `Virtualization.framework`), runs as the user, **PPID 1**: parentage does not identify the owning app.
- Other VM users may coexist: Docker Desktop was running its own `com.apple.Virtualization.VirtualMachine` instance at the same time.
- Unrelated processes match a naive `claude` name search: `Claude Usage.app`, Zed's `claude-agent-sdk`, the Claude Code CLI.
- macOS groups processes into **resource coalitions** and attributes resource usage per coalition: Claude = `com.anthropic.claudefordesktop`, Docker = `com.docker.docker` (as shown in diagnostic reports).

**macOS diagnostic reports** (`/Library/Logs/DiagnosticReports/*.diag`, readable without sudo):

- The Cowork first-run provisioning on the reference machine produced `Claude_2026-09-25-213433_nu.diag`: `Event: disk writes`, 2147.48 MB dirtied over 1059 s (2028 KB/s), exceeding the 24.86 KB/s-over-86400 s limit, `Action taken: none`, `Resource Coalition: "com.anthropic.claudefordesktop"`.
- Docker's VM produced `com.apple.Virtualization.VirtualMachine_2026-09-25-210018_nu.diag` with `Resource Coalition: "com.docker.docker"`, a same-named process owned by another app.
- macOS applies two write budgets per rolling 86400 s: **2 GiB (24.86 KB/s)** and **8 GiB (99.42 KB/s)**.

**Claude VM logs**: `~/Library/Logs/Claude/` contains `cowork_vm_swift.log`, `cowork_vm_node.log`, `coworkd.log`, `vzgvisor.log`, `main.log`.

**Native API probe results** (ctypes, no root, 2026-09-25):

- `proc_pidinfo(pid, PROC_PIDCOALITIONINFO=20, …)` returns `coalition_id[0]` (resource coalition). Claude main, helpers and Claude's VM process all report **11603**, the same ID as the `.diag` report's `"com.anthropic.claudefordesktop"(11603)`. Docker's VM reports **11507** (`com.docker.docker`).
- `responsibility_get_pid_responsible_for_pid` (exported by libSystem) returns the Claude main PID for Claude's VM, and a Docker PID for Docker's VM.
- `proc_pid_rusage(pid, RUSAGE_INFO_V2, …)` succeeds for Claude's VM process: footprint ~1 GB, write counter readable. The Claude main process had already written ~11.9 GB (the provisioning burst).
- Claude's coalition also contains **macOS system XPC services working for Claude**: `MTLCompilerService` (several), `com.apple.appkit.xpc.openAndSavePanelService`, `QuickLookUIService`, `CSExattrCryptoService`. macOS charges their resource use to Claude.

**Python/psutil facts**:

- `psutil.Process.io_counters()` is **not available on macOS**.
- Homebrew/system Python is PEP 668 "externally managed".
- `psutil.swap_memory().sin/.sout` on macOS equal `vm_stat` `Pageins`/`Pageouts` × page size (file-backed paging), **not** swap. True swap counters are `vm_stat` `Swapins`/`Swapouts`.

## 4. Functional requirements

### 4.1 Process discovery and attribution

| ID | Requirement |
|---|---|
| P-1 | Tracked set = **every process in Claude's resource coalition** (the coalition of the Claude main process `/Applications/Claude.app/Contents/MacOS/Claude`). This includes app helpers, Claude's VM process and macOS system XPC services working for Claude, matching how macOS charges resources to Claude. Fallback when the coalition cannot be determined: every process whose executable lives under `/Applications/Claude.app/` plus Claude's VM process(es) per P-3/P-4. |
| P-2 | Excluded by default: Claude Code CLI, Zed agent SDK, `Claude Usage.app`, and anything else merely matching "claude". The Claude Code CLI can be included as a separate, clearly labelled group via `--include-cli`. |
| P-3 | VM attribution (explicit check for `com.apple.Virtualization.VirtualMachine` processes; also used in the P-1 fallback), primary: a `com.apple.Virtualization.VirtualMachine` process counts as Claude's if it is in the **same resource coalition** as the Claude main process (coalition ID via `ctypes` `proc_pidinfo(PROC_PIDCOALITIONINFO)`). This matches how macOS itself attributes resources. VMs owned by other apps (Docker, UTM, OrbStack, …) are excluded. |
| P-4 | VM attribution, fallbacks: if the coalition lookup fails, use the macOS **responsible PID** (`ctypes`, e.g. `responsibility_get_pid_responsible_for_pid`) and require it to be a Claude.app process. If that also fails, fall back to name matching and mark the VM row with `?` (uncertain attribution). |
| P-5 | Role classification from the command line: `main`, `renderer`, `gpu`, `utility:<sub-type short name>` (e.g. `utility:network`, `utility:node`), `crashpad`, `vm`, `xpc` (macOS system services under `/System/` in Claude's coalition, other than the VM), `other`. |
| P-6 | Process set is re-discovered every poll; processes appearing/disappearing (renderer restarts, VM start/stop) must be handled without errors. |

### 4.2 Grouping

| ID | Requirement |
|---|---|
| G-1 | Default view: **one row per role**, with metrics aggregated (summed) across that role's PIDs. Deltas and peaks are continuous across PID churn. |
| G-2 | A key (`p`) toggles an expanded view showing individual PIDs under each role. |
| G-3 | A **Total Claude** row aggregates all tracked processes. |
| G-4 | The log records both per-PID and per-role values regardless of view. |

### 4.3 Disk footprint

| ID | Requirement |
|---|---|
| D-1 | Report **apparent size** (`st_size`) and **allocated size** (`st_blocks × 512`) for every tracked path. Alerts and growth rates use **allocated** size. |
| D-2 | VM bundle rows: `rootfs.img`, `sessiondata.img`, **other** (all remaining bundle files combined), and **bundle total**. These are measured via `stat()` on every poll (cheap). |
| D-3 | Flag when `rootfs.img.zst` disappears or reappears (download cache removed/re-downloaded). |
| D-4 | Tracked directories (recursive, allocated + apparent): `vm_bundles/`, `Cache/`, `Code Cache/`, `vm_bundles/warm/`, `claude-code-vm/`, `local-agent-mode-sessions/`, `IndexedDB/`, `GPUCache/`, and the **whole support directory total**. |
| D-5 | Directory sizes are maintained via **FSEvents** (`watchdog` library): events mark subtrees dirty and only dirty subtrees are re-walked. A full rescan runs every 5 minutes (`--full-rescan`, default `300` s) as a safety net against missed/coalesced events. |
| D-6 | Missing paths (e.g. `vm_bundles/` absent because Cowork was never used, or the support dir wiped while the app runs) are shown as "absent", never raise, and are picked up automatically when they appear. |
| D-7 | For each path show: current, Δ since last sample, Δ since session start, peak this session. |
| D-8 | Flag any `*.partial` file inside `vm_bundles/` older than **5 minutes** (stalled/abandoned download or re-download loop). Such files count toward the bundle total and the "other" row. |
| D-9 | All support-dir paths (§4.3) are resolved relative to `--support-dir`. |

### 4.4 CPU

| ID | Requirement |
|---|---|
| C-1 | CPU is measured per process as **% of one core** (the same convention as `ps`/`top`; can exceed 100%). |
| C-2 | Per role and for Total Claude, maintain a **60 s rolling average** (`--cpu-window`, default `60`). |
| C-3 | Flag a role/total when its rolling average exceeds **30%** (`--cpu-threshold`, default `30`). |
| C-4 | **Idle baseline** = the **rolling minimum** of the 60 s average observed this session, per role and total. Display the current value's difference from the baseline so an idle climb is visible. |
| C-5 | The `b` key resets baselines (CPU and memory) and peaks (see K-4). |

### 4.5 Memory and swap

| ID | Requirement |
|---|---|
| M-1 | Per-process and per-role memory, with Δ and peak: **physical footprint** (`ri_phys_footprint` from `proc_pid_rusage`, the "Memory" figure Activity Monitor shows) as the primary value, plus **RSS**. RSS is used if footprint cannot be read. |
| M-4 | Memory growth alert: per role and Total Claude, flag when the **5-minute average footprint** is more than **50%** above that role's **rolling-minimum baseline** of the same 5-min average (`--mem-growth`, default `50`). This catches the slow-leak pattern (#22543) and ignores the VM's large but stable allocation (~1.9 GB, #26194/#30972). `b` resets this baseline too. |
| M-2 | System-wide swap: **swap used/total** (`sysctl vm.swapusage`, equivalently `psutil.swap_memory().used/.total`), and cumulative **swap-ins** and **swap-outs** from `vm_stat` `Swapins`/`Swapouts` (pages × page size). **Not** `psutil.swap_memory().sin/.sout`: on macOS these are `vm_stat` `Pageins`/`Pageouts` (file-backed paging), verified 2026-09-25 with psutil 7.2.2. |
| M-3 | Flag when **swap-outs increase on 3 consecutive polls**. Swap used and swap-ins are displayed and logged but do not trigger the flag. |

### 4.6 Disk I/O

| ID | Requirement |
|---|---|
| I-1 | Default mode (no sudo): per-process cumulative bytes read/written via `proc_pid_rusage` (`ctypes`, `rusage_info_v2+` fields `ri_diskio_bytesread` / `ri_diskio_byteswritten`). Rates are derived from counter deltas. |
| I-2 | Per role and Total Claude: flag when the **60 s rolling average write rate exceeds 1 MB/s** (`--write-threshold`, default 1 MB/s). |
| I-3 | Track Total Claude bytes written over a **rolling 24 h window** (effectively capped at session length for shorter sessions), mirroring macOS's accounting. Show it as a percentage of both macOS budgets: **2 GiB** (24.86 KB/s × 86400 s) and **8 GiB** (99.42 KB/s × 86400 s). Flag each tier at **80%** (`--budget-warn`, default `80`). Per-minute buckets are sufficient for the window. |
| I-4 | Processes whose counters cannot be read (permission) show "n/a" rather than erroring. |
| I-5 | Optional elevated mode `--trace-io`: runs `sudo fs_usage -w -f filesys` against the tracked PIDs to show *which files* are being written (top files by bytes). Never invoked without this flag. |
| I-6 | Directory growth (4.3) remains visible as a secondary I/O signal. |

### 4.7 Polling

| ID | Requirement |
|---|---|
| T-1 | Process/metric poll interval `--interval`, default **3 s**, allowed range 2–5 s. |
| T-2 | Directory sizes update from FSEvents between polls; bundle file `stat()`s run each poll. |
| T-3 | The tool must stay lightweight: no full directory walks on the fast poll path. |

### 4.8 TUI

| ID | Requirement |
|---|---|
| U-1 | Live-updating table rendered with `rich` (`Live`). |
| U-2 | Columns: metric/process name, current value, Δ since last poll, Δ since start, peak this session; CPU rows also show baseline. |
| U-3 | Rows crossing a threshold are highlighted (see §5). |
| U-4 | Header shows session start, elapsed time, log path, and whether `--trace-io` is active. |

### 4.9 Keyboard controls

| ID | Key | Action |
|---|---|---|
| K-1 | `q` | Quit cleanly (flush log, restore terminal). Ctrl-C does the same. |
| K-2 | `p` | Toggle per-PID expanded view. |
| K-3 | `m` | Write a timestamped **marker** record to the log, with an optional short label (e.g. "started Cowork task"). |
| K-4 | `b` | Reset CPU and memory baselines and session peaks. |

Key input uses stdlib `termios`/`tty` cbreak mode; no extra dependency.

### 4.10 Headless mode

| ID | Requirement |
|---|---|
| H-1 | `--no-tui`: no table; samples go to the log, and each flag event is printed as one line to stderr. Intended for `nohup`/`tmux` overnight runs. |
| H-2 | Headless behaviour is also used automatically when stdout is not a TTY. |

### 4.11 Logging

| ID | Requirement |
|---|---|
| L-1 | Format: **JSONL**, one file per session, default `./logs/monitor-YYYYmmdd-HHMMSS.jsonl`; override with `--log PATH`. |
| L-2 | Record types (field `type`): `session_start` (config, versions, host info), `sample` (timestamp, per-PID, per-role, totals, paths, bundle files, swap, I/O), `flag` (raised/cleared, metric, value, threshold), `marker` (label). |
| L-3 | Every record carries an ISO-8601 timestamp with timezone and a monotonic elapsed-seconds field. |
| L-4 | Log is flushed after each record so a crash/kill loses at most the current sample. |
| L-5 | Logs every bundle file individually (not just the rows shown in the TUI). |
| L-6 | Record type `diag_report` for each macOS diagnostic report detected (§4.12). |

### 4.12 macOS diagnostic reports

| ID | Requirement |
|---|---|
| X-1 | Watch `/Library/Logs/DiagnosticReports/` via FSEvents for new `.diag` files. |
| X-2 | A report is Claude's when its `Command` is a tracked Claude binary **or** its `Resource Coalition` is `com.anthropic.claudefordesktop`. Reports from other coalitions (e.g. `com.docker.docker`) are ignored. |
| X-3 | Parse `Event` (e.g. `disk writes`, `cpu resource`), `Writes`/`Writes limit`, `Duration`, `Action taken`, and the `Date/Time` range. Raise a flag, show it in the TUI, and log a `diag_report` record. |
| X-4 | At startup, list Claude reports from the preceding 24 h once (context for the rolling budgets). They are not re-flagged. |
| X-5 | If the directory is unreadable, show "n/a" and continue. |

## 5. Alert rules (summary)

| Signal | Rule | Default | Flag |
|---|---|---|---|
| CPU | per role / total 60 s avg > threshold | 30% of one core | `--cpu-threshold`, `--cpu-window` |
| VM bundle growth | allocated bundle size > start size + N | 1 GB | `--bundle-growth` |
| VM bundle rate | allocated growth over 10 min > N | 100 MB / 10 min | `--bundle-rate` |
| Bundle cache | `rootfs.img.zst` disappears or reappears | — | — |
| Memory growth | per role / total 5-min avg footprint > rolling-min baseline + N% | 50% | `--mem-growth` |
| Swap | swap-outs increase on 3 consecutive polls | 3 polls | — |
| Disk writes | per role / total 60 s avg write rate > N | 1 MB/s | `--write-threshold` |
| Write budget | Total Claude rolling-24 h writes ≥ N% of 2 GiB tier; separately of 8 GiB tier | 80% | `--budget-warn` |
| Stale download | `*.partial` in `vm_bundles/` older than 5 min | 5 min | — |
| macOS diagnostic | new Claude `.diag` report (e.g. `disk writes`) | — | — |
| VM attribution | coalition and responsible-PID lookups both failed | — | row marked `?` |

The spec's original absolute ">5 GB VM bundle" alert is **dropped**: a fresh bundle is already ~11 GB, so an absolute threshold is meaningless. Absolute size remains displayed.

## 6. CLI summary

| Flag | Default | Purpose |
|---|---|---|
| `--interval SEC` | 3 | Poll interval (2–5) |
| `--full-rescan SEC` | 300 | Periodic full directory rescan |
| `--cpu-threshold PCT` | 30 | CPU alert threshold (% of one core) |
| `--cpu-window SEC` | 60 | CPU rolling-average window |
| `--write-threshold BYTES/S` | 1 MB/s | Disk write alert threshold |
| `--budget-warn PCT` | 80 | Warn at this % of each macOS 24 h write budget |
| `--mem-growth PCT` | 50 | Memory footprint growth-over-baseline alert |
| `--support-dir PATH` | `~/Library/Application Support/Claude` | Override the support directory (testing, replaying a copied bundle). Process discovery is unaffected |
| `--bundle-growth BYTES` | 1 GB | Bundle growth-since-start alert |
| `--bundle-rate BYTES` | 100 MB | Bundle growth per 10 min alert |
| `--log PATH` | `./logs/monitor-<ts>.jsonl` | Log file |
| `--no-tui` | off | Headless logging mode |
| `--include-cli` | off | Also track Claude Code CLI as a separate group |
| `--trace-io` | off | Elevated `sudo fs_usage` file-level write tracing |

## 7. Tooling and packaging

| ID | Requirement |
|---|---|
| E-1 | **mise** manages tool versions via a project `mise.toml`: Python **3.14.7**, uv **0.12.18** (latest available as of 2026-09-25). |
| E-2 | **uv** project: `pyproject.toml` + committed `uv.lock`; environment created with `uv sync`. |
| E-3 | Runtime dependencies: `psutil`, `rich`, `watchdog`. Everything else from the standard library (`ctypes`, `termios`, `json`, …). |
| E-4 | Application code is a **single script**, `monitor.py`; no build step. |
| E-5 | Run command: `uv run monitor.py [flags]` (a mise task alias, e.g. `mise run monitor`, may be provided). |
| E-6 | No `sudo` in default mode. |

## 8. Deliverables

1. `monitor.py`: the monitor.
2. `mise.toml`, `pyproject.toml`, `uv.lock`.
3. `README.md` containing:
   - how to install and run (mise + uv);
   - what each tracked metric maps to in the known issue reports, with links;
   - how to interpret sustained growth / CPU climb / swap-out streaks as signals;
   - the alert rules and CLI flags;
   - where to look next when a flag fires: the Claude VM logs in `~/Library/Logs/Claude/` (e.g. `guest_vsock_connect started` with no matching `completed` means the VM is stalled at boot, per #87794; a repeating download message means a re-download loop, per #51913) and the `.diag` reports in `/Library/Logs/DiagnosticReports/`.
4. This `requirements/REQUIREMENTS-v1.md`.

## 9. Verification

| ID | Requirement |
|---|---|
| V-1 | Verified live against a real Cowork VM on the reference machine (bundle present, VM running, Docker's VM concurrently running to exercise P-3 exclusion). |
| V-2 | Confirm `proc_pid_rusage` is readable for the VM process without root; if not, I-4 applies and the README documents it. |
| V-3 | Confirm the absent-path case (D-6) with `--support-dir /tmp/nonexistent`, then create directories under it while running and confirm they are picked up. |
| V-4 | Headless run for an extended period produces a well-formed JSONL log (every line parses; markers present). |
| V-5 | Startup listing (X-4) finds the existing `Claude_2026-09-25-213433_nu.diag` and ignores the Docker-coalition VM report. |
| V-6 | Coalition attribution (P-3) selects Claude's VM process and excludes Docker's concurrently running VM. |

## 10. Open items

| # | Item | Status |
|---|---|---|
| O-1 | **README citations**: approved list in §11. | Resolved 2026-09-25 |
| O-2 | **`--support-dir PATH` flag**: adopted (D-9, §6). | Resolved 2026-09-25 |
| O-3 | **Memory alert**: growth-based on physical footprint (M-1, M-4). | Resolved 2026-09-25 |

## 11. Approved README citations

All links verified 2026-09-25. The README must cite these (with open/closed state) against the metrics they motivate.

| Issue | State | Motivates | Key evidence |
|---|---|---|---|
| [anthropics/claude-code#22543](https://github.com/anthropics/claude-code/issues/22543): Cowork creates 10GB VM bundle that severely degrades performance | Open (high-priority) | Bundle size, CPU climb, swap, `Cache/`/`Code Cache/` | Idle CPU 24% → 55% over minutes (renderer 24%, main 21%, GPU 7%); swap-ins 20K → 24K+; bundle regenerates after deletion |
| [#65577](https://github.com/anthropics/claude-code/issues/65577): rootfs.img grows unboundedly, never reclaimed | Open | Bundle growth, `sessiondata.img` | No fstrim/discard pass-through; ~10 GB written in a day; `sessiondata.img` 1.3 GB; bundle ~13 GB |
| [#37860](https://github.com/anthropics/claude-code/issues/37860): Cowork VM root partition 86% full on fresh image | Closed (dup/stale) | `sessiondata.img` growth | Per-session data (e.g. plugin copies) accumulates, never auto-cleaned |
| [#89869](https://github.com/anthropics/claude-code/issues/89869): VM provisioning writes 10.7 GB in 13 min, trips disk-write limit | Open | Write rate, `rootfs.img.zst` retention, non-sparse rootfs | Two limits: 24.86 KB/s (2 GiB/day) and 99.42 KB/s (8 GiB/day); `.zst` retained; stray `*.partial` |
| [#55242](https://github.com/anthropics/claude-code/issues/55242): exceeds disk-write watchdog during long sessions | Closed (dup) | Cumulative writes vs 24 h budget | 3 incidents in 8 days; accumulation over 13–23 h sessions |
| [#43390](https://github.com/anthropics/claude-code/issues/43390): writes 8–13 GB in under an hour | Closed (invalid: wrong repo) | Write rate, directory tracking | 2–5 MB/s sustained; `vm_bundles/warm/` 941 MB; Spotlight/apfsd knock-on CPU |
| [#26194](https://github.com/anthropics/claude-code/issues/26194): VM process spins CPU indefinitely (M5) | Closed | VM-process CPU | `com.apple.Virtualization.VirtualMachine` ~316% CPU, 1.9 GB RAM, `hv_trap` loop |
| [#87794](https://github.com/anthropics/claude-code/issues/87794): VM fails to boot, 120% CPU nonstop | Closed | VM CPU at idle | 120% within minutes of launch, without using Cowork |
| [#30972](https://github.com/anthropics/claude-code/issues/30972): VM loads at app launch without opening Cowork | Closed (not planned) | VM RSS | ~1.87 GB RAM with Cowork never opened |
