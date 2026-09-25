# Plan 06: Analysis and alerts

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Execution order and rationale: [`index.md`](index.md).

## Context

The failure modes this monitor targets are **degradation over time**, not single spikes:
- CPU creeping up from an idle baseline;
- memory footprint growing (a suspected leak);
- the VM bundle growing because the guest never trims its disk;
- sustained disk writes exhausting macOS's rolling 24 h write budgets (2 GiB and 8 GiB);
- swap-outs starting and not stopping.

Collectors (Plans 03–05) produce raw per-poll samples. This plan turns the sample stream into deltas, peaks, rolling averages and baselines, and raises or clears flags. It works purely on sample dictionaries, so every rule can be unit-tested with synthetic data. That matters because real-world triggers take minutes to days to reproduce.

### Inputs used (from the sample; see Plan 01's data contract)

| Sample field | From | Used for |
|---|---|---|
| `roles.<r>.cpu_pct`, `total.cpu_pct` | Plan 03 | CPU window and baseline |
| `roles.<r>.footprint`, `total.footprint`, `rss` | Plan 03 | memory growth |
| `roles.<r>.write_delta`/`write_rate`, `total.*` | Plan 03 | write-rate flag, 24 h budget |
| `processes[].attribution` | Plan 03 | `?` attribution flag |
| `paths.<n>.allocated/apparent` | Plan 04 | Δ/peak |
| `bundle.rows.*`, `bundle.zst_present`, `bundle.partials` | Plan 04 | bundle growth/rate, zst change, stale partials |
| `swap.*` | Plan 05 | swap-out streak |
| `diag.new` | Plan 05 | diagnostic-report events |

## Goal

An `Analyzer` (§6) implementing `analyze(sample) -> (derived, flag_events)` that covers every alert rule in REQUIREMENTS §5, plus the reset hook used by the `b` key.

## Prerequisites

Plan 01 complete. The collector output shapes from Plans 03–05 must be implemented or at least fixed (tests use synthetic samples, so this plan can proceed in parallel once the shapes are frozen).

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| D-7 | Per path: current, Δ since last sample, Δ since start, peak. |
| C-2 | Per role and Total: 60 s rolling average CPU (`--cpu-window`). |
| C-3 | Flag when the rolling average > 30% of one core (`--cpu-threshold`). |
| C-4 | Idle baseline = rolling minimum of the 60 s average; show current − baseline. |
| C-5/K-4 | Reset baselines (CPU and memory) and peaks on request. |
| M-3 | Flag when swap-outs increase on 3 consecutive polls. |
| M-4 | Flag when a role's or the total's 5-min average footprint > its rolling-min baseline (of the same 5-min average) + 50% (`--mem-growth`). |
| I-2 | Flag when a role's or the total's 60 s average write rate > 1 MB/s (`--write-threshold`). |
| I-3 | Rolling 24 h Total Claude bytes written as % of the 2 GiB and 8 GiB budgets; flag each tier at 80% (`--budget-warn`). |
| D-3 | Flag when `rootfs.img.zst` disappears or reappears. |
| D-8 | Flag `*.partial` in `vm_bundles/` older than 5 min. |
| X-3 | New Claude diagnostic report → flag. |
| §5 | Bundle growth: allocated bundle total > start + 1 GB (`--bundle-growth`). Bundle rate: allocated growth over 10 min > 100 MB (`--bundle-rate`). VM attribution `?` → flag. |

## Design

### Metric series

Every tracked scalar is a **series** keyed by a stable ID:

| Series ID | Value |
|---|---|
| `cpu:<role>`, `cpu:total` | `cpu_pct` |
| `mem:<role>`, `mem:total` | `footprint` (falls back to `rss` when footprint is `None`) |
| `rss:<role>`, `rss:total` | `rss` |
| `wr:<role>`, `wr:total` | `write_rate` |
| `rd:<role>`, `rd:total` | `read_rate` |
| `path:<name>:alloc`, `path:<name>:app` | directory allocated/apparent |
| `bundle:<row>:alloc`, `bundle:<row>:app` | bundle rows (`rootfs.img`, `sessiondata.img`, `other`, `total`) |
| `swap:used`, `swap:swapins`, `swap:swapouts` | swap values |

For each series, `derived[id]` contains:
- `cur`
- `d_poll` (vs previous sample; `None` on first sighting)
- `d_start` (vs first sighting this session; series that appear late start from their first value)
- `peak` (max since start or the last reset)
- where applicable, `avg`, `baseline` and `vs_baseline`

A series whose value is absent (e.g. the role disappeared, the path is absent) gets `cur: None` and keeps its history; it resumes when the value returns.

### Rolling windows

`TimeWindow(seconds)`: a deque of `(t, value)`, where `t` is the sample's monotonic `elapsed`. Entries older than the window are evicted.
- `mean()` = arithmetic mean of the values in the window.
- `full` = the oldest entry is at least `seconds − interval` old.

Samples arrive at a fixed interval, so an arithmetic mean is adequate.

| Window | Length | Used by |
|---|---|---|
| CPU | `--cpu-window` (60 s) | C-2, C-3, C-4 |
| Write rate | 60 s | I-2 |
| Memory | 300 s | M-4 |
| Bundle rate | 600 s of `bundle:total:alloc` | §5 bundle rate |

### Baselines (C-4, M-4)

- Baseline = minimum of the window mean, **updated only while the window is full**. This prevents the first, partial sample (e.g. CPU `None`/0) from pinning the baseline at zero.
- `vs_baseline = avg − baseline` for CPU (percentage points), and `avg / baseline − 1` for memory (fraction).
- **Memory floor**: the M-4 rule only applies when the baseline ≥ 32 MiB. This avoids meaningless percentages on tiny helpers.

### 24 h write budget (I-3)

- Per-minute buckets (a deque of up to 1440) of the sum of `total.write_delta`. Window total = sum of the buckets from the last 86,400 s (capped at session length).
- `derived.write_budget = {"window_s": min(elapsed, 86400), "bytes": B, "pct_2g": B / 2^31 × 100, "pct_8g": B / 2^33 × 100}`.
- **Documented limitation:** writes made before the session started are invisible, because Plan 03 only counts post-start writes for pre-existing processes. The percentage can therefore understate macOS's own accounting. The startup `.diag` listing (Plan 05) gives that context.

### Flags

Two kinds:
- **State flags** have `raised`/`cleared` transitions. Each raise and each clear emits one `flag` event.
- **Event flags** are one-shot (`state: "event"`), shown in the TUI's recent-events list for 10 minutes and always logged.

To prevent a value hovering at the threshold from flapping, a state flag **clears** only when its value falls below 90% of the threshold (10% hysteresis). Exception: the swap streak.

| Flag ID | Kind | Raise when | Clear when |
|---|---|---|---|
| `cpu:<role>`, `cpu:total` | state | CPU window full and mean > `cpu_threshold` | mean < 0.9 × threshold |
| `mem:<role>`, `mem:total` | state | memory window full, baseline ≥ 32 MiB, `avg/baseline − 1 > mem_growth/100` | ratio < 0.9 × threshold |
| `wr:<role>`, `wr:total` | state | write window mean > `write_threshold` | mean < 0.9 × threshold |
| `budget:2g`, `budget:8g` | state | pct ≥ `budget_warn` | pct < 0.9 × budget_warn |
| `bundle:growth` | state | `bundle:total:alloc` − start value > `bundle_growth` | below 0.9 × threshold |
| `bundle:rate` | state | alloc now − alloc at the oldest point in the 600 s window > `bundle_rate` | below 0.9 × threshold |
| `bundle:zst` | event | `zst_present` changes (either direction) | — |
| `partial:<relpath>` | state | a partial file with `age_s > 300` exists | the file is gone |
| `swap:streak` | state | `swapouts_bytes` increased on each of the last 3 polls | a poll with no increase |
| `attr:vm?` | state | any process with `attribution == "name?"` | none remaining |
| `diag:<filename>` | event | an entry in `diag.new` | — |

Each event: `{"id", "state", "metric", "value", "threshold", "message"}`, where the message is human-readable, e.g. `"renderer CPU 60s avg 41.2% > 30%"` or `"Claude disk writes report: 2147 MB over 1059 s, limit 24.86 KB/s, action none"`.

**Bundle start value:** if the bundle is absent at start and appears later (e.g. first Cowork provisioning during the session), the start value is 0. That provisioning then raises `bundle:growth`, which is intended: it is an ~11 GB write burst.

`active_flags` in the sample = the sorted list of raised state-flag IDs.

### Reset (`b` key, C-5/K-4)

`Analyzer.reset()` clears all baselines and peaks, and sets `baseline` to "warming" until each window is full again. It does **not** clear active flags, windows, `d_start` references or the write-budget buckets. It emits a `marker` record `{"label": "reset baselines/peaks"}`.

## Tasks

1. §6: `TimeWindow`, `Series` (cur/d_poll/d_start/peak/avg/baseline), `Analyzer` wiring every series from the sample.
2. Rules and the flag state machine with hysteresis; event flags; `active_flags`.
3. Write-budget buckets and `derived.write_budget`.
4. `reset()`.
5. Headless renderer prints each flag event (Plan 01 format).
6. `tests/test_analysis.py` with synthetic sample generators:
   - Window `full` semantics; baseline not set until the window is full.
   - CPU raise/clear with hysteresis; no flap at 29–31%.
   - Memory growth with the 32 MiB floor.
   - Write-rate flag.
   - Budget percentages across minute buckets and past 24 h (simulated `elapsed`).
   - Bundle growth from an absent start; bundle rate over 600 s.
   - zst toggle event; partial ageing.
   - Swap streak: 3 increases raise, a flat poll clears.
   - Role appearing late (`d_start` from first sighting).
   - `reset()` semantics.

## Verification (done when)

- [ ] `uv run pytest tests/test_analysis.py -v` passes, with at least one test per flag in the table above.
- [ ] Live headless run, 10 min: `derived` present in every sample; no flags while Claude is idle below thresholds.
- [ ] Live induced checks:
  - `--cpu-threshold 1` raises `cpu:*` flags within ~60 s and they clear after restoring.
  - `--write-threshold 1KB` raises `wr:total` during Cowork activity.
  - `--bundle-rate 1MB` raises `bundle:rate` while `sessiondata.img` grows.

  Each prints a stderr line and writes a `flag` record.
- [ ] Committed.

## Plan-level decisions (not specified in REQUIREMENTS.md)

- 10% clear hysteresis on state flags; the swap streak clears on the first flat poll.
- Baselines update only when a window is full.
- A 32 MiB baseline floor for the memory growth rule.
- Event flags stay visible for 10 min in the TUI.
- Bundle rules use the `claudevm.bundle` total (per-poll `stat`), not the `vm_bundles/` directory size.
- Bundle start value is 0 when the bundle is absent at start.
- `reset()` does not reset `d_start` references, active flags or budget buckets.

## Out of scope

Display and highlighting (Plan 07). Collectors (Plans 03–05).
