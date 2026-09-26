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

The authoritative specification is [`../requirements/REQUIREMENTS-v1.md`](../requirements/REQUIREMENTS-v1.md). Every plan restates the requirements it covers, so each can be read on its own.

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

These choices are made in the plans but are **not** specified in requirements/REQUIREMENTS-v1.md. Review them before or during execution. Each plan lists its own.

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

Planning probes changed requirements/REQUIREMENTS-v1.md in two places. Both are already applied there:

- **P-1, P-3, P-5:** the tracked set is the whole Claude resource coalition, including macOS XPC helpers as role `xpc`. This came from the native-API probe and was approved by the user.
- **M-2:** swap-ins and swap-outs now come from `vm_stat` `Swapins`/`Swapouts`, because psutil's `sin`/`sout` on macOS are file page-ins and page-outs, not swap. Without this change, the M-3 swap-streak flag would fire on ordinary file I/O.

## Status

| Plan | Status |
|---|---|
| 01 | Done (5681d43) — added `conftest.py` at repo root (not listed in Plan 01's file table) so `uv run pytest` can `import monitor` without packaging the script; no other deviations |
| 02 | Done (84efa5c) — no deviations; live tests confirmed Claude's Cowork VM (coalition 11603) matched and Docker's VM (coalition 11507) excluded on this run |
| 03 | Done (4e0d673) — deviation: the CLI-group name match (`is_cli_process`) accepts both `claude` and `claude.exe` basenames, because this machine's mise/npm global install shims the Claude Code CLI as `claude.exe`; the plan assumed a bare `claude` basename. `--trace-io` line parsing (`parse_fs_usage_line`) splits fs_usage columns on runs of 2+ spaces rather than plain whitespace, since paths under the support dir contain single embedded spaces (e.g. "Application Support"). Not yet live-verified: `--trace-io` (needs user sudo) and quit/relaunch mid-run (needs user, per Hard rules never to quit Claude Desktop). |
| 04 | Done (29aadb6) — no deviations. Fixed a real bug found while testing: `DiskCollector` now resolves `--support-dir` (`Path.resolve()`) before comparing it against watchdog event paths, because macOS resolves symlinked prefixes (e.g. `/tmp` → `/private/tmp`) in FSEvents paths but not in the raw CLI argument; without this every event was silently dropped (`relative_to` raised and was swallowed). Not yet live-verified: "during a Cowork task, only affected units show fresh `last_scan_elapsed`" (needs a live Cowork task, per §9). |
| 05 | Done (4f5347e) — deviation: `SignalsCollector` looks up Claude's current coalition ID itself (via the existing `_find_main`/`coalition_id` helpers from §2/§3) instead of through the sample, since `collect(now)` doesn't see other collectors' output; matching still works without it via coalition name/path, as the plan allows. `run()` (§9) gained a small loop turning `diag.historic`/`diag.new` into `diag_report` log records, next to the existing flag-record loop. Live-verified V-5 within the 24h window: startup scan found `Claude_2026-09-25-213433_nu.diag` (coalition 11603) as historic and logged nothing for the Docker VM's `.diag` (coalition 11507). |
| 06 | Done (4961a13) — deviations, both in §9 (Plan 01's section): `run()` now sets `sample["elapsed"]` before calling `analyzer.analyze()` (the analyzer needs a stable per-sample time reference for its rolling windows, and the sample dict had no such field until `LogWriter` added one afterwards); `run()` now takes `sample["active_flags"]` from the new `Analyzer.active_flags` property (the full current set of raised state flags) instead of rebuilding it from that tick's `flag_events` (which only ever held flags that changed state on that exact poll — a pre-existing gap in the Plan 01 scaffold, not a Plan 06 regression). `Analyzer.__init__` now takes `args` instead of being a no-arg stub. Live-verified: 12 min headless idle run showed `derived` on every sample and no flags; `--cpu-threshold 1 --cpu-window 9` raised `cpu:total` in ~9s; `--write-threshold 1KB` raised `wr:total`/`wr:main`. Not yet live-verified: `--bundle-rate` during a live Cowork task growing `sessiondata.img` (needs the user, per §9). |
| 07 | Done (24e3c06) — deviations, both in §6 (Plan 06's `Analyzer`, needed by the TUI's own data contract since neither field existed anywhere in `derived`): added `derived["swap_streak"]` (the consecutive-increase counter that drives the `swap:streak` flag, previously internal-only) and `derived[f"wrsum:{suffix}"]` (cumulative bytes written per role/total since session start, needed for the I/O panel's "Written this session" column — Analyzer only tracked write *rate* series before). Per §9 (Plan 01's `run()`): `poll_keys()`'s return value is now captured and acted on (`q`→stop, `b`→`analyzer.reset()` + marker, `m`→marker with the renderer's prompted label) since the Plan 01 scaffold discarded it. Live-verified: headless smoke run against the real running Claude Desktop confirms both new derived fields populate correctly (`wrsum:total`, `swap_streak`); rendered the real dashboard (via `Console(record=True)`) at 200- and 80-column widths — all panels populate, roles/paths/bundle rows match `du`-equivalent sizes, absent paths show `absent`, the two-column layout activates at ≥160 cols and stacks at 80, and the cropped-terminal note appears with a computed row count at 80×24. Simulated `q`/`b`/`m` through `run()` with a stub renderer: `q` ends the session with reason `quit`, `b` and `m` each write the expected `marker` record. Not yet live-verified (needs a real terminal + user, per §9 of CLAUDE.md): actual keypresses in a live TTY, the `m` prompt's terminal pause/resume, `p` PID-view toggle by eye, terminal restoration after `q`/SIGTERM/an injected exception, and `--cpu-threshold` flagging turning a row red on screen. |
| 08 | Done (dfedcab) — deviation: the #51913 citation-gate question was put to the user, who chose to keep it description-only (no link), per the plan's default. All of V-1 to V-6, the fresh-clone check and `uv run pytest` passed live against the reference machine (Claude Desktop 2.9939.2 and Docker Desktop both running); results in `plans/08-verification-results.md`. Two live-induced findings during verification, not scope bugs: V-1's first attempted run (kept in the results file for reference) had no Cowork task or markers and was redone; V-4's ad-hoc watcher script used to send a second `SIGUSR1` marker and stop the run at ~1 h had a bug (`ps -o etimes=` unreliable in this shell) and never fired — the run itself was unaffected and had already exceeded 1 h by the time this was noticed, so the second marker and `SIGTERM` were sent manually instead. |

---

# v2: macOS notifications (plans 09–12)

Requirements: [`../requirements/REQUIREMENTS-v2.md`](../requirements/REQUIREMENTS-v2.md), incremental on v1. v2 sends macOS notifications (via pync / terminal-notifier, with the Claude logo as a thumbnail) when flags are raised or events fire, and when the monitor itself exits unexpectedly.

## The plans

| # | Plan | Delivers | Checkpoint (done when) |
|---|---|---|---|
| 09 | [Notifier: delivery layer](09-notifier.md) | `pync` dependency, `assets/claude-icon.png`, `NotificationSender` (non-blocking worker, escaping and truncation, tier sounds, thumbnail, failure detection), `--notify-test` | Unit tests with a fake backend; V-8 banners confirmed |
| 10 | [Notification policy](10-notification-policy.md) | `details` on flag events; flag → notification mapping, tiers, §5 wording, per-flag cooldown, `--no-notify`, `--notify-cooldown`, `notification` log record; wired into the poll loop | V-7 unit tests; V-9 live flag and cooldown; V-11 `--no-notify` |
| 11 | [Lifecycle and UI integration](11-lifecycle-ui.md) | Unexpected-exit notification (exception, SIGTERM, SIGHUP), SIGHUP handler, `notify: on/off` in the TUI header, warn-once display | V-10 kill/hang-up/quit checks; header check |
| 12 | [README and v2 verification](12-readme-verification.md) | README Notifications section and troubleshooting; V-7 to V-12 recorded | All results recorded and passing |

## Order and dependencies

```
09 Notifier ──> 10 Policy ──> 11 Lifecycle & UI ──> 12 README & verification
```

Strictly sequential. 10 and 11 both depend only on 09, but both edit `run()` (§9) and its shutdown path, so running them in parallel would conflict. The per-plan workflow, conventions and checkpointing rules from v1 (above) apply unchanged.

**Live checks** (V-8 to V-11) produce real notifications and are done one at a time, each confirmed with the user before the next.

## Why the work is split this way

1. **Mechanism separate from policy.** 09 only delivers a ready-made notification; 10 decides which flags notify and with what text. The policy is then fully unit-testable with a fake sender, and only 09's `--notify-test` needs a real Mac.
2. **Process-level risk isolated.** 11 changes signal handling, exception paths and shutdown order: behaviour that is tested differently (`kill`, closing a tmux pane) and can leave a terminal broken if wrong. Keeping it out of 10 keeps both checkpoints small.
3. **Every plan ends runnable.** After 09 the tool can send a test notification; after 10 flags notify; after 11 silent death is covered; 12 documents and verifies.

## Shared conventions added in v2

- **Section layout:** new §7a Notifications (between §7 log writer and §8 renderers).
- **Contracts:** flag events gain an optional `details` dict (additive). Renderers gain `on_notify_warning(message)`.
- **Log:** new record type `notification`; `session_start` gains `notify`; `session_end` gains `signal`.
- **Threads:** notifications are delivered by one worker thread; the LogWriter is only ever called from the main thread.

## Requirement traceability

| Requirement IDs | Plan |
|---|---|
| N-1 – N-4, N-13, N-21, N-22/N-23 (detection), N-23 exception (`load_pync`), E-3, E-7, V-8 | 09 |
| N-5 – N-12, N-18, N-19, N-23 exception (`notify:init` record), L-7, L-8, V-7, V-9, V-11 | 10 |
| N-14 – N-17, N-20, N-22 (display), N-23 exception (display), L-7 (`dispatched`), V-10 | 11 |
| §8 deliverables, N-17/N-24 (docs), V-7 – V-12 (full run) | 12 |

## Plan-level decisions (v2): reviewed with the user 2026-09-26

| Plan | Decision |
|---|---|
| 09 | One daemon worker and a bounded queue (64); full queue → `failed`; pync `wait=True` in the worker, no per-send timeout; **eager** `import pync` at startup (failure → notifications unavailable for the session); subtitle ≤ 40 chars, body lines ≤ 60 chars; a PATH `terminal-notifier` takes precedence (pync behaviour); thumbnail at `assets/claude-icon.png`; `--notify-test` as drafted (exit 0 only if both sent) |
| 10 | `details` payloads per flag type (also in `flag` records); cooldown timestamps at dispatch; `sent`/`failed` logged when the worker reports; fall back to the v1 `message` if `details` is incomplete; unknown prefixes → warning tier; `--notify-cooldown 0` = no cooldown; shutdown waits up to 2 s for queued flag notifications (every shutdown) |
| 11 | v1 `session_end.reason` kept, plus a `signal` field; exit notification is fire-and-forget (`dispatched` status, no wait) and bypasses cooldown and queue; inherited `SIG_IGN` for SIGHUP (nohup) respected; failure warning stays in the header for the session; the 2 s flush runs after the exit notification is launched |
| 12 | V-9/V-12 induced by user-driven CPU spikes (`--cpu-window 6`, threshold idle + 5); V-12 ±0.5 s per poll over 10 min with ≥ 10 sends; results in `12-verification-results.md` |

## Status (v2)

| Plan | Status |
|---|---|
| 09 | Done (3690ede) — no code deviations. Docs deviation: pync's vendored terminal-notifier 2.0.0 is x86_64-only (Rosetta warning seen during V-8); the reference machine now uses Homebrew terminal-notifier 3.1.0 (arm64) via pync's PATH preference. N-1 and §3 of REQUIREMENTS-v2 amended (O-5); Plan 12 README to recommend it. V-8 passed with the Homebrew binary (both banners, sounds and thumbnail confirmed by the user; exit 0). `uv sync` from a clean `.venv` OK. The pytest fork `DeprecationWarning` in `test_native.py` predates v2. |
| 10 | Done (0a96c6c) — no code deviations. Live-verified: V-9 with a real running Claude Desktop and user-driven CPU spikes (`--cpu-window 6 --cpu-threshold 7 --notify-cooldown 120`, idle Total CPU measured at avg 0.6%/max 2.4%) — banner + Glass sound + thumbnail on raise, `flag` record carried `details`, `notification` `sent`, clear on stop, a second spike within the 120s window logged `suppressed_cooldown` with no banner, and (a spike that landed exactly at the 120.0s boundary) resent. V-11 with `--no-notify`: `session_start.config.notify` was `false`, flags still raised/cleared normally, zero `notification` records written. |
| 11 | Done (6a55e1f) — two real bugs found and fixed during live verification, both outside the plan's literal task list but squarely in its scope: (1) `TuiRenderer.render()` didn't tolerate a dead TTY (only `stop()` did) — a real SIGHUP (closed tmux pane) made `Live.update()` raise `OSError` mid-loop, which was caught by the generic exception handler and misclassified the clean SIGHUP shutdown as `reason: error` instead of `reason: signal, signal: SIGHUP`; fixed by catching `OSError` around `Live.update()`, same as the existing `stop()` handling. (2) The exit notification's fire-and-forget path used pync's own `Notifier.notify()`, whose internal `Popen` inherits the caller's process group; a real pty hangup (unlike `kill -TERM`, which doesn't touch a controlling terminal) kills a just-launched `terminal-notifier` before it can show anything. Fixed by having `_pync_backend` bypass pync's `execute()` for `wait=False` (fire-and-forget is the only caller) and launch `terminal-notifier` directly with `start_new_session=True`, using `pync.Notifier.bin_path` (a singleton instance attribute, not a class to instantiate — an intermediate attempt at this fix called `pync.Notifier()`, which raised `TypeError` and was silently swallowed by `fire_and_forget`'s error handling, so two live retries showed no banner for the wrong reason before this was caught). All planned tasks done as specified otherwise. Live-verified: V-10.1 (SIGTERM, headless, nohup'd) — banner, `notification` `dispatched`, `session_end` (`signal: SIGTERM`); V-10.2 (SIGHUP via tmux `kill-pane`) — banner, log complete, terminal undamaged, confirmed after both bug fixes; V-10.3 — `q` (`reason: quit`) and Ctrl-C/SIGINT (`signal: SIGINT`) both produced no banner. V-11 header: `notify: on` by default, `notify: off` with `--no-notify`. Forced-failure check (debug-patched backend, `--cpu-threshold 1 --cpu-window 6`): header showed `notify: on (failing: see log)` plus one yellow warning line after the first of several failed sends, later failures logged only. `uv run pytest` (171 tests) passing throughout. |
| 12 | Done (c491817) — no code deviations; no defects found in plans 09–11 during live verification. README gained a Notifications section (what notifies/tiers/sounds, cooldown, unexpected-exit behaviour, setup/troubleshooting steps) plus updated install, CLI, and JSONL record-type text. All of V-7 to V-12 passed live against the reference machine (Claude Desktop running); results in `plans/12-verification-results.md`. Two live-check reruns needed during V-10 (SIGHUP): the user asked to trigger the tmux-pane-kill check twice to confirm it, both passed identically — not a defect. |

---

# v3: Homebrew distribution (plans 13–17)

Requirements: [`../requirements/REQUIREMENTS-v3.md`](../requirements/REQUIREMENTS-v3.md), incremental on v1 and v2. v3 makes the monitor installable with `brew install thamwangjun/tap/claude-desktop-monitor`: the script becomes the package `claude_desktop_monitor`, logs default to `~/Library/Logs/claude-desktop-monitor/`, and a personal tap ships an arm64 macOS 26 bottle. The TUI and all monitoring behaviour are unchanged.

## The plans

| # | Plan | Delivers | Checkpoint (done when) |
|---|---|---|---|
| 13 | [Package conversion](13-package-conversion.md) | `src/claude_desktop_monitor/` (moved `monitor.py` and icon), `uv_build`, console script, `LICENSE` (GPL-3.0-only), uv 0.12.19, test imports, `dist/` ignored, docs' commands | V-13 suite, V-15 `uv build` contents, dev commands run |
| 14 | [Runtime changes for 0.3.0](14-runtime-changes.md) | Default log `~/Library/Logs/claude-desktop-monitor/`, `session_start.versions.monitor`, version 0.3.0 | V-14 dev runs; tests don't touch the real log directory |
| 15 | [README and release](15-readme-release.md) | README: Homebrew install first, developer setup, log location, license. **User** pushes and tags `v0.3.0` | Tag on GitHub; archive `sha256` recorded |
| 16 | [Formula and tap CI](16-formula-tap.md) | Rust 1.98.1 via mise; `../homebrew-tap` with `brew tap-new` workflows (`macos-26`) and the formula (resources, PATH wrapper, `test do`). **User** pushes and opens the formula PR | V-19 local source build; `brew style`/`audit` clean; PR CI green with a bottle |
| 17 | [v3 verification](17-v3-verification.md) | **User** publishes the bottle; install from the tap; results file | V-13 – V-19 recorded and passing |

## Order and dependencies

```
13 Package ──> 14 Runtime ──> 15 README & tag ══> 16 Formula & CI ══> 17 Verification
                                  (user tags)       (user pushes, PR)   (user publishes)
```

Strictly sequential. `══>` marks a user gate: the next plan cannot start until the user has pushed, tagged, opened a PR or published. The per-plan workflow, conventions and checkpointing rules from v1 (above) apply unchanged.

## Why the work is split this way

1. **One kind of change per plan.** 13 changes how the project is built and run, with no behaviour change, so git records `monitor.py` as a rename and keeps its history. 14 holds the only user-visible changes. 15 is documentation and the release. 16 lives in a different repository. Each commit is reviewable on its own.
2. **Plans end at user gates.** Pushing, tagging, opening PRs and publishing are the user's (git rules). Each gate closes a plan, so no plan stays open across a user action.
3. **The tag comes after everything it ships.** The formula downloads the tag's archive and GitHub shows the tag's README, so code (13, 14) and README (15) are final before `v0.3.0`.
4. **Every plan ends runnable.** After 13 the dev commands work; after 14 the release behaviour is in place; after 15 the source is public; after 16 the formula builds in CI; after 17 users can install it.

Considered and rejected: merging 13 and 14 (the rename would carry behaviour changes and show as a large edit); merging 15 into 14 (a plan ending at the release gate would also contain code); merging 16 and 17 (one plan open across two user actions).

## Shared conventions added in v3

- **Layout:** the application is one module, `src/claude_desktop_monitor/monitor.py` (§-sections unchanged), plus `__init__.py`, `__main__.py` and `assets/`. Tests import `from claude_desktop_monitor import monitor`; `conftest.py` is gone.
- **Commands:** `uv run claude-desktop-monitor`, `python -m claude_desktop_monitor`, `mise run monitor`. `uv run monitor.py` no longer works.
- **Logs:** default `~/Library/Logs/claude-desktop-monitor/` (the monitor's own directory; `~/Library/Logs/Claude/` stays read-only).
- **Docs follow the code:** each plan updates `CLAUDE.md`, `PLAN_EXEC_INSTRUCTIONS.md` and README lines for its own change, so no command in them is ever broken between plans.
- **Tap repository:** `../homebrew-tap`, commit messages per B-9 (Homebrew style plus the attribution lines). Formula changes reach `main` through PRs (bottles).
- **Homebrew hygiene:** Homebrew's `rust` is never installed on the reference machine; developer mode is switched off after developer commands (`tap-new`, `style`, `audit`, `update-python-resources` switch it on).

## Requirement traceability

| Requirement IDs | Plan |
|---|---|
| E-1 (uv), E-8, E-9, E-10, E-12, E-13 (no dependency change), N-2, W-2 (layout, commands), §8.5, V-13, V-15 | 13 |
| L-1, L-8, E-11, W-2 (log location), V-13 (no real log writes), V-14 | 14 |
| W-1, E-11 (tag), B-8 (first release) | 15 |
| E-1 / E-13 (Rust pin), W-1 (Rust note), W-2 (tap, Rust, developer mode), B-1 – B-7, B-9, V-19, V-16 (build) | 16 |
| V-16 (publish), V-17, V-18, V-13 – V-19 (recorded) | 17 |

## Plan-level decisions (v3), for review

| Plan | Decision |
|---|---|
| 13 | Delete `conftest.py` (editable install); add `description` and `readme` metadata; no per-file license headers; keep `monitor.py`'s `__main__` block; README gets the mechanical command replacement now, its restructure in 15 |
| 14 | `DEFAULT_LOG_DIR` constant in §1; `versions.monitor` is `"unknown"` without package metadata; the V-14 run leaves one real log in the new directory |
| 15 | Annotated tag; the user adds the remote (SSH or HTTPS) as well as pushing; README describes the Homebrew install before it exists, proved by V-17 |
| 16 | Templates from a scratch tap `thamwangjun/cdm-scratch` (`--no-git`), then untapped; workflows committed to `main`, formula via PR from `claude-desktop-monitor-0.3.0`; dev tap = local clone tapped by path; `desc` adjusted to `brew style`; V-19 uses the formula's resource versions; stop and ask if `update-python-resources` can't resolve the tag URL |
| 17 | Publish via the generated workflow rather than local `brew pr-pull`; `--notify-test` under `env -i` with a minimal PATH to prove the wrapper; V-13 – V-15 and V-19 carried from their plans, suite rerun once |

## Status (v3)

| Plan | Status |
|---|---|
| 13 | Done (8fd7972) — no code deviations. `git log --follow --oneline src/claude_desktop_monitor/monitor.py` shows the full v1/v2 history through the rename (100% similarity). All verification passed: `uv run pytest` (171 tests); `uv run claude-desktop-monitor --help`, `uv run python -m claude_desktop_monitor --help` and `mise run monitor -- --help` all print the same usage; a 15s headless run to `/tmp/cdm-p13.jsonl` + SIGINT produced a well-formed log (`session_start`, `sample`, `diag_report`, `session_end`); `monitor.NOTIFY_ICON.exists()` is `True` from the installed package; `uv build` produced one sdist and one wheel, with the wheel containing `claude_desktop_monitor/monitor.py` and `claude_desktop_monitor/assets/claude-icon.png` and the sdist containing `LICENSE`; `dist/` is gitignored. No `uv run monitor.py` remains in README.md, CLAUDE.md, PLAN_EXEC_INSTRUCTIONS.md or mise.toml. |
| 14 | Done (d1a4fcc) — no deviations. `uv run pytest` (172 tests, one new: `test_collect_versions_monitor_matches_installed_package`) passing throughout; `~/Library/Logs/claude-desktop-monitor/` didn't exist before the suite and still doesn't after (V-13 part). Live-verified headless (V-14): `uv run python -m claude_desktop_monitor --no-tui` (no `--log`) for 15s + SIGINT wrote `~/Library/Logs/claude-desktop-monitor/monitor-20260927-022114.jsonl` with `session_start.versions.monitor == "0.3.0"`; a second run with `--log /tmp/cdm-p14.jsonl` wrote only there, with no new file in the default directory. V-14 TUI live-verified by the user: dashboard rendered, header showed the new log path under `~/Library/Logs/claude-desktop-monitor/`, `q` exited cleanly with the terminal restored. |
| 15 | Done (9f8a858) — no code changes (README only). Deviation: fixed a stale doc bug from Plan 13 while editing the same section — Developer setup's `mise install` comment still said uv 0.12.18; `mise.toml` already pins 0.12.19 (E-1's bump landed in Plan 13, whose "docs' commands" task missed this line). `uv run pytest` passing (172 tests); `git status` clean before the user gate. README: Homebrew install first (`## Install`), developer setup second (`## Developer setup`), license section added, log location and `session_start.versions.monitor` noted; only the 9 approved issues are linked (`#51913` stays unlinked, as approved in Plan 08). User pushed `main` and the annotated tag `v0.3.0` (`git tag -a`/`git push origin main`/`git push origin v0.3.0`, all HTTPS to `https://github.com/thamwangjun/claude-desktop-monitor.git`). `git ls-remote --tags origin` confirms `v0.3.0` peels to commit `d1bab23` (the status-recording commit above). Archive `https://github.com/thamwangjun/claude-desktop-monitor/archive/refs/tags/v0.3.0.tar.gz` sha256 `f580bd37a4e2cb55baf8080b09a2587b7ed7f32b09f92428197b3da1114e9e1a`; contains `src/claude_desktop_monitor/monitor.py`, `src/claude_desktop_monitor/assets/claude-icon.png`, `LICENSE` and `pyproject.toml`. |
| 16 | Done (7aa50f5) — deviation: tap CI's first run of PR #1 failed `brew audit --new` (the installed-prefix check test-bot runs after a real install, which a bare local `brew audit` doesn't do) on pync's vendored x86_64 `terminal-notifier.app` landing in `libexec` via `virtualenv_install_with_resources`; fixed by removing it in the formula's `install` (`Dir.glob` + `rm_r`, since it's unreachable at runtime anyway — the PATH wrapper (B-5) always finds Homebrew's arm64 `terminal-notifier` first, and `brew test`'s import/`--help` check had already passed against the *first* run before the audit step, proving that). No other deviations. `mise.toml` gained `rust = "1.98.1"`; `mise exec -- rustc --version` confirms it; `brew list --versions rust` stayed empty throughout (start, after `tap-new`/`style`/`audit`, and at the end). `../homebrew-tap` (`thamwangjun/homebrew-tap`, previously empty) got `main` (`b41df3e`, `brew tap-new` CI templates — the generated test matrix was already `macos-26`-only, no reduction needed) and branch `claude-desktop-monitor-0.3.0` (`faf6cbc` after the audit fix, force-pushed over the user's initial push of `3de92ec`) with the formula and all 9 `brew update-python-resources` blocks, versions matching `uv.lock` exactly. V-19 passed: every resource sdist and the project's own `uv build` sdist installed into a scratch venv under mise's Rust (`pip install --no-binary :all:`), `uv_build` visibly compiled from source via `cargo` (not fetched as a wheel), `claude-desktop-monitor --help` and the package import both succeeded, Homebrew's `rust` never installed. `brew style` and `brew audit --strict --online` pass clean (one autocorrect: `formula_opt_bin` instead of `Formula[...].opt_bin`). User set the tap's repository settings, pushed `main` and the formula branch, and opened PR #1; V-16 passed live: CI green on `macos-26` (13m27s, from-source build) plus the tap's Linux tap-syntax job, with a `bottles_macos-26` artifact (1.97 MB) uploaded. Developer mode confirmed off (`brew developer`) at the end. `uv run pytest` (172 tests) passing throughout. |
| 17 | Not started |
