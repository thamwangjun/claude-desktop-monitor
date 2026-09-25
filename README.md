# Claude Desktop Resource Monitor

`monitor.py` is a macOS terminal tool that watches **Claude Desktop**'s resource usage over time, with emphasis on the **Cowork** feature's local Linux VM (Apple's Virtualization.framework). Public issue reports describe *gradual degradation*, not single-snapshot spikes: the VM's disk image growing without bound, CPU creeping up while idle, memory growth and swapping, and sustained disk writes that trip macOS's own disk-write budgets (2 GiB and 8 GiB per rolling 24 h).

The tool samples processes, disk footprint, swap and macOS diagnostic reports every few seconds, logs everything to JSONL, flags anomalies against fixed rules, and shows a live `rich` dashboard (or runs headless for overnight logging).

**This is observability only.** It never modifies, deletes or moves anything under Claude's support directory, its logs, or `/Library/Logs/DiagnosticReports/`. It never kills or restarts Claude Desktop, its VM, or Docker. It never reverse-engineers or modifies Claude Desktop or its VM.

## Requirements

- macOS (uses `ctypes` calls into `libSystem.B.dylib` and private-but-stable Apple APIs).
- [Claude Desktop](https://claude.ai/download) installed, in `/Applications` or `~/Applications`.
- [mise](https://mise.jdx.dev/) for tool version management.

Tested on Apple M5 (Mac17,4), macOS 26.7 (25G229), Claude Desktop 2.9939.2.

## Install and run

```
mise install                 # Python 3.14.7, uv 0.12.18 (pinned in mise.toml)
uv sync                      # create/refresh .venv from uv.lock
uv run monitor.py            # live TUI
uv run monitor.py --no-tui   # headless, e.g. under nohup or tmux for overnight runs
# or: mise run monitor       # alias for `uv run monitor.py`
```

Runtime dependencies are `psutil`, `rich` and `watchdog`; everything else is the standard library. `psutil.Process.io_counters()` isn't available on macOS, so process I/O comes from a `ctypes` call instead (`proc_pid_rusage`) — see [Known limitations](#known-limitations).

No `sudo` is used unless you pass `--trace-io` (see below).

## The screen, panel by panel

The header shows session start, elapsed time, poll interval, the log path, Claude's running state, app version, main PID, resource coalition ID, whether `--trace-io` is on, and the key bindings.

Below it, one panel per metric family (two columns side by side at terminal widths ≥ 160, stacked otherwise):

- **CPU (% of one core)** — one row per role plus `total` (Total Claude, §4.2). Columns: process count, current, Δ since last poll, Δ since session start, peak this session, 60 s rolling average, idle baseline (the rolling minimum of that average), and current vs. baseline. CPU is measured the same way `ps`/`top` do — a process can exceed 100% by using more than one core.
- **Memory** — per role and total: physical **footprint** (`proc_pid_rusage`'s `ri_phys_footprint`, the figure Activity Monitor calls "Memory"), Δ poll, Δ start, peak, RSS, 5-minute rolling average, and average vs. its own rolling-minimum baseline. Footprint is the primary value; RSS is used only where footprint can't be read.
- **Disk I/O** — per role and total: current write rate, 60 s average write rate, current read rate, bytes written this session (cumulative), and peak write rate. A footer line shows Total Claude's rolling-24h write total against both macOS write budgets (2 GiB and 8 GiB).
- **Disk footprint** — VM bundle rows (`rootfs.img`, `sessiondata.img`, `other`, bundle total) and tracked support-directory paths (`vm_bundles`, `vm_bundles/warm`, `Cache`, `Code Cache`, `claude-code-vm`, `local-agent-mode-sessions`, `IndexedDB`, `GPUCache`, support-directory total). Each row shows **allocated** size (`st_blocks × 512`, what actually occupies disk — the figure alerts and growth rates use) and **apparent** size (`st_size`), plus Δ poll, Δ start and peak.
- **System** — swap used/total, cumulative swap-ins and swap-outs (true `vm_stat` `Swapins`/`Swapouts`, *not* the file-paging counters `psutil` mislabels `sin`/`sout` on macOS), and the current swap-out streak length.
- **Flags and events** — active flags with how long each has been raised, a 10-minute history of flag transitions, historic `.diag` reports found at startup (within the preceding 24 h), and — with `--trace-io` — the top files written in the last 60 s.
- **Claude Code CLI** (only with `--include-cli`) — process count, aggregate CPU, footprint and write rate for the CLI group, shown separately from Total Claude.

`p` toggles per-PID sub-rows under each role (CPU, memory and I/O panels).

Special values: **`n/a`** = the metric couldn't be read (e.g. a permission error, or `proc_pid_rusage` failed for that process). **`…`** = a rolling baseline is still warming up (not enough samples yet). **`?`** = VM attribution fell back to name matching because both the coalition lookup and the responsible-PID lookup failed — treat the row with caution. **`absent`** = the path doesn't exist yet (e.g. Cowork has never run); it starts showing real values automatically once the path appears, with no need to restart the monitor.

## Metric → known issue map

| Issue | State | Metric |
|---|---|---|
| [#22543](https://github.com/anthropics/claude-code/issues/22543): Cowork creates 10GB VM bundle that severely degrades performance | Open | bundle size, CPU climb (idle 24% → 55%), swap-ins, `Cache/`/`Code Cache/` |
| [#65577](https://github.com/anthropics/claude-code/issues/65577): rootfs.img grows unboundedly, never reclaimed | Open | bundle growth, `sessiondata.img` |
| [#37860](https://github.com/anthropics/claude-code/issues/37860): Cowork VM root partition 86% full on fresh image | Closed | `sessiondata.img` growth |
| [#89869](https://github.com/anthropics/claude-code/issues/89869): VM provisioning writes 10.7 GB in 13 min | Open | write rate, 2 GiB/8 GiB budgets, `rootfs.img.zst` kept, non-sparse `rootfs.img`, `*.partial` |
| [#55242](https://github.com/anthropics/claude-code/issues/55242): exceeds disk-write watchdog during long sessions | Closed | rolling 24 h write budget |
| [#43390](https://github.com/anthropics/claude-code/issues/43390): writes 8–13 GB in under an hour | Closed | write rate, `vm_bundles/warm/`, directory tracking |
| [#26194](https://github.com/anthropics/claude-code/issues/26194): VM process spins CPU indefinitely | Closed | `vm` role CPU (~316%) |
| [#87794](https://github.com/anthropics/claude-code/issues/87794): VM fails to boot, 120% CPU nonstop | Closed | `vm` role CPU at idle |
| [#30972](https://github.com/anthropics/claude-code/issues/30972): VM loads at app launch without opening Cowork | Closed | `vm` role footprint (~1.87 GB) |

## Reading the signals

- **Sustained bundle growth**: allocated bundle size rising across sessions and never falling back down points to the no-trim/no-GC pattern (#65577).
- **`sessiondata.img` rising per session**: accumulating per-session data that's never cleaned up (#37860).
- **CPU climb**: the idle baseline stays flat while the rolling average creeps upward over minutes (#22543). The `vm` role pinned above 100% while idle points to a stuck boot or a spin loop, not normal usage (#26194, #87794).
- **Swap-out streak**: real memory pressure. This is deliberately *not* the same signal as `psutil`'s `sin`/`sout`, which on macOS are ordinary file paging and fire constantly regardless of memory pressure.
- **Write budget**: approaching 80% of the 2 GiB (or 8 GiB) tier predicts a macOS `disk writes` diagnostic report. Cross-check against the `.diag` reports shown in the Flags panel.
- **Memory growth over baseline**: a role's or total's 5-minute average footprint climbing well above its own session-minimum baseline is the suspected slow-leak pattern — distinct from the VM's large-but-flat footprint (~1.9 GB), which sits well below any growth threshold once it settles.
- **One spike vs. a trend**: a single high reading is often nothing (a build, a large read). What matters is whether it recurs or a rolling average keeps climbing — which is why everything is logged, not just displayed. Press `m` to write a labelled marker before and after doing something in Cowork (e.g. starting a task), so the log makes clear which activity produced which growth.

## Alert rules and CLI flags

| Signal | Rule | Default | Flag |
|---|---|---|---|
| CPU | per role / total 60 s avg > threshold | 30% of one core | `--cpu-threshold`, `--cpu-window` |
| VM bundle growth | allocated bundle size > start size + N | 1 GB | `--bundle-growth` |
| VM bundle rate | allocated growth over 10 min > N | 100 MB / 10 min | `--bundle-rate` |
| Bundle cache | `rootfs.img.zst` disappears or reappears | — | — |
| Memory growth | per role / total 5-min avg footprint > rolling-min baseline + N% | 50% | `--mem-growth` |
| Swap | swap-outs increase on 3 consecutive polls | 3 polls | — |
| Disk writes | per role / total 60 s avg write rate > N | 1 MB/s | `--write-threshold` |
| Write budget | Total Claude rolling-24h writes ≥ N% of 2 GiB tier; separately of 8 GiB tier | 80% | `--budget-warn` |
| Stale download | `*.partial` in `vm_bundles/` older than 5 min | 5 min | — |
| macOS diagnostic | new Claude `.diag` report (e.g. `disk writes`) | — | — |
| VM attribution | coalition and responsible-PID lookups both failed | — | row marked `?` |

An absolute ">5 GB VM bundle" alert was considered and dropped: a fresh Cowork bundle is already ~11 GB, so an absolute threshold is meaningless. Absolute size is still shown.

CLI flags (`uv run monitor.py --help`):

| Flag | Default | Purpose |
|---|---|---|
| `--interval SEC` | 3 | Poll interval (2–5) |
| `--full-rescan SEC` | 300 | Periodic full directory rescan interval |
| `--cpu-threshold PCT` | 30 | CPU alert threshold, % of one core |
| `--cpu-window SEC` | 60 | CPU rolling-average window |
| `--write-threshold BYTES/S` | 1MB | Disk write alert threshold, bytes/s |
| `--budget-warn PCT` | 80 | Warn at this % of each macOS 24h write budget |
| `--mem-growth PCT` | 50 | Memory footprint growth-over-baseline alert, % |
| `--bundle-growth BYTES` | 1GB | Bundle growth-since-start alert, bytes |
| `--bundle-rate BYTES` | 100MB | Bundle growth per 10 min alert, bytes |
| `--log LOG` | `./logs/monitor-<timestamp>.jsonl` | Log file path |
| `--no-tui` | off | Headless logging mode, no live table |
| `--include-cli` | off | Also track the Claude Code CLI as a separate group |
| `--trace-io` | off | Elevated `sudo fs_usage` file-level write tracing |
| `--support-dir SUPPORT_DIR` | `~/Library/Application Support/Claude` | Override the Claude support directory (testing) |

Byte-valued flags accept decimal (`KB`/`MB`/`GB`) or binary (`KiB`/`MiB`/`GiB`) suffixes, e.g. `--write-threshold 512KiB`.

Headless mode (`--no-tui`, or automatically when stdout isn't a TTY) prints one line per flag transition to stderr and writes nothing else to the terminal — meant for `nohup`/`tmux`.

## When a flag fires, look here next

- **`/Library/Logs/DiagnosticReports/Claude_*.diag`**: read the `Event`, `Writes`/`Writes limit`, `Duration` and `Action taken` lines. The monitor already surfaces these in the Flags panel and as `diag_report` log records, but the raw file has the full detail.
- **`~/Library/Logs/Claude/`** (`cowork_vm_swift.log`, `cowork_vm_node.log`, `coworkd.log`, `vzgvisor.log`, `main.log`) — not parsed by the monitor (their format is undocumented and version-dependent), but worth grepping by hand:
  - `guest_vsock_connect started` with no matching `completed` line means the VM is stalled at boot ([#87794](https://github.com/anthropics/claude-code/issues/87794)).
  - A download message repeating every few seconds means a re-download loop (reported separately as #51913, a re-download loop distinct from the stalled-download `*.partial` flag above; not one of the citations verified for this README, so no link here).
- **`--trace-io`** (needs `sudo`): shows which files are actually being written. Run it yourself so you can type your password: `sudo -v` first, then `uv run monitor.py --trace-io`.

## Reviewing a session log

Each line in the JSONL log is one record with a `type` field: `session_start` (config, dependency and host versions), `sample` (one poll: `claude`, `processes`, `roles`, `total`, optionally `cli` and `trace_io`, `paths`, `bundle`, `swap`, `diag`, `derived`, `active_flags`), `flag` (`raised`/`cleared`/`event`, with `id`, `value`, `threshold`, `message`), `marker` (a `label`), `diag_report` (one parsed `.diag` file), and `session_end` (`reason`: `quit`, `error`, or the SIGTERM handler). Every record has `ts` (ISO-8601 with timezone) and `elapsed` (monotonic seconds since session start).

Flags timeline:

```
jq -c 'select(.type=="flag")' logs/monitor-*.jsonl
```

Bundle allocated size over time:

```
jq -c 'select(.type=="sample") | {ts, elapsed, bytes: .bundle.rows.total.allocated}' logs/monitor-*.jsonl
```

Total Claude CPU over time, with pandas:

```python
import pandas as pd
df = pd.read_json("logs/monitor-20260925-213000.jsonl", lines=True)
samples = df[df["type"] == "sample"]
cpu = samples["total"].apply(lambda t: t.get("cpu_pct") if isinstance(t, dict) else None)
print(pd.concat([samples["elapsed"], cpu.rename("cpu_pct")], axis=1))
```

## Known limitations

- Process attribution relies on two private-but-documented-in-xnu-headers macOS APIs (`PROC_PIDCOALITIONINFO`, `responsibility_get_pid_responsible_for_pid`). Apple could change or remove them in a future macOS release; the tool falls back to name matching (marked `?`) if either fails.
- The rolling 24 h write budget only counts writes seen since the monitor started; writes from before that are invisible to it, so the percentage shown can undercount what macOS itself is tracking.
- APFS clones are counted per file (each clone's allocated size is added separately), so directory totals can overstate real disk usage compared to `du` on a volume with heavy cloning.
- `--trace-io`'s `fs_usage` output format is undocumented and varies across macOS versions; its parsing is best-effort and may miss or misattribute some writes.
- `psutil.swap_memory().sin`/`.sout` are **not** swap on macOS — they're `vm_stat` `Pageins`/`Pageouts` (file-backed paging), which is why real swap comes from `vm_stat`'s `Swapins`/`Swapouts` instead.

## Non-goals

This tool never suggests or performs remediation (it doesn't delete caches, trim the VM bundle, or restart anything), and it never modifies or reverse-engineers Claude Desktop or its VM. It is diagnostic only: what to look at next is up to you.
