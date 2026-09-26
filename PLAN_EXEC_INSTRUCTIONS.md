# PLAN_EXEC_INSTRUCTIONS.md: Claude Desktop Resource Monitor

Guide for implementing this project one plan per session. Each session is given a plan (e.g. "execute plan 03"). This file, plus the plan file, is everything needed to do it.

## 1. What this project is

`monitor.py` is a macOS terminal tool that monitors **Claude Desktop's** resource usage over time. It focuses on the **Cowork** feature's local Linux VM, which runs on Apple's Virtualization.framework.

Public issue reports describe gradual degradation:
- the VM disk image growing without bound;
- CPU creeping up while idle;
- memory growth and swapping;
- sustained disk writes tripping macOS's disk-write budgets (2 GiB and 8 GiB per rolling 24 h).

The tool samples processes, disk footprint, swap and macOS diagnostic reports every few seconds, logs everything to JSONL, flags anomalies, and shows a live `rich` dashboard or runs headless.

**v2** (plans 09–12) adds **macOS notifications**: when a flag is raised or an event fires, and when the monitor itself exits unexpectedly. They are sent via pync / terminal-notifier, with the Claude logo as a thumbnail.

It is observability only (see §7 Hard rules).

## 2. Documents, in order of authority

| File | Role |
|---|---|
| `requirements/REQUIREMENTS-v1.md` | Authoritative spec. Requirement IDs (P-, G-, D-, C-, M-, I-, T-, U-, K-, H-, L-, X-, E-, V-), alert rules (§5), CLI flags (§6), approved citations (§11). |
| `requirements/REQUIREMENTS-v2.md` | Incremental spec for notifications (plans 09–12): N-1 – N-24, L-7, L-8, E-3 (amended), E-7, V-7 – V-12, notification wording (§5), CLI additions (§6). It amends v1: **where v2 changes something, v2 wins; everything else is still v1.** |
| `plans/index.md` | Execution order, dependencies, per-plan workflow, shared conventions, traceability, plan-level decisions, **status table**. |
| `plans/NN-*.md` | The plan to execute: context, requirements covered, design, tasks, verification checklist, plan-level decisions, out of scope. |

If a plan and the requirements conflict, the requirements win (REQUIREMENTS-v2 over v1 for anything v2 amends). Stop and raise the conflict with the user; don't pick one silently.

Plans:
- **v1:** `01-scaffold`, `02-native-layer`, `03-processes`, `04-disk-footprint`, `05-system-signals`, `06-analysis-alerts`, `07-tui`, `08-readme-verification`. Default order is 01 → 08. Plans 02, 04 and 05 each depend only on 01. All are done.
- **v2:** `09-notifier`, `10-notification-policy`, `11-lifecycle-ui`, `12-readme-verification`. **Strictly sequential**, 09 → 12: 10 and 11 both edit `run()` and the shutdown path.

## 3. How to execute a plan (every session)

1. **Orient.**
   - Read the given plan file in full, plus `plans/index.md` (status table and conventions).
   - Check the plan's **Prerequisites** against the status table and `git log --oneline`.
   - If a prerequisite plan is not done, stop and tell the user.
2. **Check the ground.** Read the current `monitor.py` sections you will touch and the existing tests. Earlier plans may have made small, recorded deviations; see their commits and the status table notes.
3. **Implement.** Only the plan's tasks, and only within its `monitor.py` section(s) (§5 below).
   - A defect found in an earlier plan's section may be fixed there; name it in the commit message.
   - Don't add features, flags or files the plan doesn't list.
4. **Verify.** Run every item in the plan's **Verification (done when)** checklist, plus `uv run pytest` for the **whole** suite.
   - Report actual outputs.
   - If a check cannot pass as specified, **stop and ask**. Never weaken, skip or reinterpret a check to make it pass.
5. **Update status.** In `plans/index.md` → Status, set the plan to `Done (<commit short hash>)`. Add a short note for any deviation or plan-level decision that changed.
6. **Commit** (see §8), then **checkpoint**: tell the user what was built, the verification results (pass/fail per item), any deviations, and anything that needs their decision. Do not start the next plan unless asked.

Checks that need the user are listed in §9. Ask the user to do those; don't work around them.

## 4. Environment

| Item | Value |
|---|---|
| Machine | Apple M5 (Mac17,4), 10 CPUs, 32 GB RAM, macOS 26.7 (25G229) |
| User shell | fish. Commands in the Bash tool run fine; when giving the user commands to type, keep them POSIX-simple or fish-compatible. |
| Tool manager | mise 2026.9.11 (globally: Python 3.14.6, uv 0.11.28) |
| Project pins | `mise.toml`: **Python 3.14.7**, **uv 0.12.18**. Run `mise install` in the repo if missing. |
| Python env | uv project: `pyproject.toml` + committed `uv.lock`, `uv sync`, run with `uv run …`. Never use `pip install` into system or Homebrew Python (PEP 668). |
| Runtime deps | `psutil`, `rich`, `watchdog`, and from Plan 09 **`pync`** (which pulls in `python-dateutil`; pync 2.0.3 is an sdist, which `uv sync` builds). Dev only: `pytest` (`[dependency-groups] dev`). Nothing else without asking. |
| Claude Desktop | `/Applications/Claude.app`, version 2.9939.2, bundle ID `com.anthropic.claudefordesktop`. Cowork VM downloaded. |
| Docker Desktop | Installed and often running. Runs its **own** `com.apple.Virtualization.VirtualMachine` process, which must never be counted as Claude's. |
| Xcode CLT SDK | `/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk` (public headers only) |

Commands:

```
mise install                 # tool versions from mise.toml
uv sync                      # create/refresh .venv from uv.lock
uv run monitor.py            # TUI
uv run monitor.py --no-tui   # headless
uv run pytest                # full test suite
```

## 5. Code conventions (fixed by Plan 01)

- **Single application file** `monitor.py`, importable without side effects (`if __name__ == "__main__": main()`). Tests in `tests/`, fixtures in `tests/fixtures/`.
- **Sections**, marked by banners `# ── §N name ──`:

  | § | Section | Plan |
  |---|---|---|
  | 1 | Constants, unit helpers, CLI | 01 |
  | 2 | Native macOS calls (ctypes) | 02 |
  | 3 | Process collector | 03 |
  | 4 | Disk collector | 04 |
  | 5 | System signals (swap, `.diag` reports) | 05 |
  | 6 | Analysis and alerts | 06 |
  | 7 | JSONL log writer | 01 |
  | 7a | Notifications: sender, policy, wording | 09, 10 |
  | 8 | Renderers: headless, TUI | 01, 07, 11 |
  | 9 | Main loop, entry point | 01 |

- **Contracts:**
  - Collector: `collect(now) -> dict` (a sample fragment) and `close()`.
  - Analyzer: `analyze(sample) -> (derived, flag_events)` and `reset()`. From Plan 10, flag events may carry an optional `details` dict (additive).
  - Renderer: `start()`, `render(sample, derived, active_flags)`, `on_flag(event)`, `poll_keys(timeout) -> list[str]`, `stop()`, and from Plan 11 `on_notify_warning(message)`.
  - Notifications (Plan 09): `NotificationSender` with `send`, `drain_results`, `flush`, `fire_and_forget`, `close`; a single worker thread delivers them. **The LogWriter is only ever called from the main thread.**
- **Sample data contract**: the JSON shape in Plan 01 (`claude`, `processes`, `roles`, `total`, `cli`, `trace_io`, `paths`, `bundle`, `swap`, `diag`, `derived`, `active_flags`). Each plan fills its own keys. Don't rename keys defined by an earlier plan.
- **Log record types**: `session_start`, `sample`, `flag` (`raised|cleared|event`), `marker`, `diag_report`, `session_end`. From v2, also `notification` (`sent|suppressed_cooldown|failed|dispatched`); `session_start` gains `notify`, `session_end` gains `signal`, and `flag` may carry `details`. Every record has `ts` (ISO-8601 with timezone) and `elapsed` (monotonic seconds). Flush after each record.
- **Units**: bytes, bytes/s, CPU % of **one core** (can exceed 100%), seconds. Display uses binary units (KiB/MiB/GiB).
- **Errors**: collectors never raise for vanished processes, missing paths or unreadable data. They report `None` / `absent` / `n/a`.
- **Style**: plain, readable Python 3.14 with type hints where they help; comments only where the *why* isn't obvious. Match the existing code.

## 6. Verified technical facts (don't rediscover; rely on these)

All verified on this machine on 2026-09-25.

### Native calls (ctypes on `/usr/lib/libSystem.B.dylib`, no root)

| Call | Details | Observed |
|---|---|---|
| `proc_pid_rusage(pid, 2, &rusage_info_v2)` | `RUSAGE_INFO_V2 = 2` (public `sys/resource.h`). Struct: `uint8_t ri_uuid[16]` then 18 × `uint64_t`: `user_time, system_time, pkg_idle_wkups, interrupt_wkups, pageins, wired_size, resident_size, phys_footprint, proc_start_abstime, proc_exit_abstime, child_user_time, child_system_time, child_pkg_idle_wkups, child_interrupt_wkups, child_pageins, child_elapsed_abstime, diskio_bytesread, diskio_byteswritten`. Returns 0 on success. | Works for Claude's VM (footprint ~1 GB) and Claude main (~11.9 GB written before monitoring started) |
| `proc_pidinfo(pid, 20, 0, &buf, 40)` | `PROC_PIDCOALITIONINFO = 20` is **private** (xnu `sys/proc_info_private.h`). Struct: `uint64_t coalition_id[2]; uint64_t reserved[3]` (40 bytes). Index 0 = resource coalition. Success iff return == 40. | Claude main, helpers and Claude's VM = **11603**. Docker's VM = **11507** |
| `responsibility_get_pid_responsible_for_pid(pid)` | Private, exported by libSystem. | Claude's VM → Claude main PID; Docker's VM → a Docker PID |

`psutil.Process.io_counters()` is **not available on macOS**.

### Claude's resource coalition (the tracked set, P-1)

Contains:
- `Claude` (main);
- `chrome_crashpad_handler`;
- `Claude Helper` with `--type=gpu-process` or `--type=utility --utility-sub-type=network.mojom.NetworkService | node.mojom.NodeService (×2) | audio.mojom.AudioService | video_capture.mojom.VideoCaptureService`, plus one helper without a visible type;
- `Claude Helper (Renderer)` ×2 (`--type=renderer`);
- `com.apple.Virtualization.VirtualMachine` (Claude's VM; PPID 1, so parentage doesn't identify it);
- macOS XPC services working for Claude: `MTLCompilerService` (several), `com.apple.appkit.xpc.openAndSavePanelService`, `QuickLookUIService`, `CSExattrCryptoService`. These are role `xpc`.

Processes that match a naive "claude" name search but are **not** Claude Desktop: `Claude Usage.app`, Zed's embedded agent (path contains `claude-agent-sdk`), and the Claude Code CLI `claude` (including the session implementing this project).

### Swap

- `psutil.swap_memory().sin/.sout` on macOS = `vm_stat` **Pageins/Pageouts** (file paging, e.g. 263 GB). **They are not swap.**
- Real swap: `vm_stat` `Swapins`/`Swapouts` × page size (currently 0). Page size comes from the header `(page size of N bytes)`.
- `total`/`used`/`free` from psutil (`vm.swapusage`) are correct.

### VM bundle (`~/Library/Application Support/Claude/vm_bundles/claudevm.bundle/`, ~11 GB fresh)

- `rootfs.img`: 10 GiB apparent **and** allocated (not sparse).
- `rootfs.img.zst`: ~1.17 GiB, kept after extraction.
- `sessiondata.img`: sparse and growing.
- `initrd`, `initrd-micro`, `vmlinuz` (+ `.zst`).
- Metadata: `.*.origin`, `.cowork-adopted`, `gvisorMacAddress`, `machineIdentifier`, `vmIP`.
- Stalled downloads leave `*.partial` files.

### Support directory

`~/Library/Application Support/Claude/` contains, among others: `vm_bundles/` (also `vm_bundles/warm/` per reports), `Cache/`, `Code Cache/`, `claude-code-vm/`, `local-agent-mode-sessions/`, `IndexedDB/`, `GPUCache/`, `claude-code/`. It was observed being **wiped and recreated while the app was running**, so code must tolerate paths disappearing.

### macOS diagnostic reports (`/Library/Logs/DiagnosticReports/`)

- Readable without sudo: files are `root:_analyticsusers`, mode 660, and the user is in the group.
- Two write budgets per 86,400 s: **2 GiB (24.86 KB/s)** and **8 GiB (99.42 KB/s)**.
- Existing reports:
  - `Claude_2026-09-25-213433_nu.diag`: `Event: disk writes`, 2147.48 MB over 1059 s, limit 24.86 KB/s, `Action taken: none`, `Resource Coalition: "com.anthropic.claudefordesktop"(11603)`. This is the Cowork first-run provisioning.
  - `com.apple.Virtualization.VirtualMachine_2026-09-25-210018_nu.diag`: `Resource Coalition: "com.docker.docker"(11507)`. **Docker's; must be ignored.**
- The header format is shown in Plan 05.

### macOS notifications (verified 2026-09-26; basis for v2)

- The project interpreter (mise Python 3.14.7, python-build-standalone) is **ad-hoc signed and not an app bundle**. UNUserNotificationCenter / NSUserNotificationCenter only deliver for app bundles:
  - desktop-notifier silently uses a dummy backend;
  - macos-notifications crashes.

  Don't re-evaluate these libraries.
- **pync 2.0.3** bundles `terminal-notifier.app` 2.0.0, which delivers notifications. Behaviour:
  - `import pync` runs `which terminal-notifier`, prefers a PATH copy (e.g. Homebrew) over the vendored one, and **raises** if unusable.
  - `notify(msg, **kw)` maps each kwarg to `-<name> <value>` and launches with `Popen`. Only `wait=True` waits, and raises on a non-zero exit.
- Icons:
  - **Only `contentImage`** (a thumbnail) can show the Claude logo.
  - `appIcon` is ignored.
  - `sender=com.anthropic.claudefordesktop` shows nothing.
  - A custom `osacompile` applet never registered.
- terminal-notifier treats a message starting with `-` or `[` specially; escape it.
- Notifications disabled in System Settings, Focus and Do Not Disturb produce **no error**; only a launch failure or non-zero exit is detectable.
- Claude's icon: `/Applications/Claude.app/Contents/Resources/electron.icns`. The thumbnail is committed as `assets/claude-icon.png` (`sips -s format png -Z 256 …`).
- Sounds: `Basso` (critical), `Glass` (warning).

### Claude logs

`~/Library/Logs/Claude/`: `cowork_vm_swift.log`, `cowork_vm_node.log`, `coworkd.log`, `vzgvisor.log`, `main.log`. **Not parsed by the monitor** (non-goal); the README only points to them.

## 7. Hard rules

- **Observability only.** Never modify, delete, move or "clean up" anything under `~/Library/Application Support/Claude/`, `~/Library/Logs/Claude/` or `/Library/Logs/DiagnosticReports/`, in code **or** while testing. Tests use `tmp_path`; V-3 uses `/tmp/...` via `--support-dir`.
- **No sudo in default mode.** Only `--trace-io` may call `sudo`, and only after an explicit `sudo -v` prompt. Never run sudo yourself during a session; the user must type the password (§9).
- **Don't kill or restart** Claude Desktop, its VM or Docker. When a check needs that, ask the user.
- **Don't reverse-engineer or modify** Claude Desktop or its VM.
- **Notifications:**
  - Never change macOS notification settings (System Settings → Notifications, Focus); ask the user.
  - Live checks send real banners: run them only as the plan specifies, one at a time, and confirm each with the user.
  - Unit tests always use a fake sender or backend, never real notifications.
- **Citations:** the README may cite only the 9 issues approved in `requirements/REQUIREMENTS-v1.md` §11. #51913 needs explicit user approval first.
- **No scope creep:** no new flags, dependencies, files or behaviours beyond the plan. Anything missing → raise it at the checkpoint.

## 8. Git

- Work on `main` (the user's established practice). **Never push** unless asked. Don't create branches or worktrees unless asked.
- One commit per plan (more if natural). Subject prefixed `plan NN: …`. The body lists what was done, deviations, and any fixes to earlier sections.
- Commit only files the plan touched, plus `plans/index.md` (status). `logs/` and `.venv/` are gitignored.
- Every commit message ends with exactly:

  ```


  Authored by: Tham Wang Jun <6513021+thamwangjun@users.noreply.github.com>

  Co-Authored-By: Claude <noreply@anthropic.com>
  ```

  Any pull request description ends with the same two lines.

## 9. Checks that need the user

Ask the user to do these at the right moment, then continue from their report or the resulting log:

- Quitting and relaunching Claude Desktop (Plan 03).
- Starting or stopping Docker Desktop (Plans 03, 08).
- Running a Cowork task in Claude Desktop, e.g. to make `sessiondata.img` or writes grow (Plans 03, 06, 08).
- Anything with `sudo`, e.g. `--trace-io` (Plan 03). Suggest they run it themselves, e.g. `! uv run monitor.py --trace-io`.
- Interactive TUI checks: key presses, visual layout, terminal restoration (Plan 07). Give an exact checklist.
- Checking values against Activity Monitor (Plan 03).
- Long headless runs (Plan 08, V-4 ≥ 1 h). You can start them in the background with `run_in_background`; tell the user it is running.
- **v2 live notification checks**, each confirmed with the user **one at a time** before the next:
  - V-8 (Plan 09): `--notify-test` banners. If nothing appears, the user may need to allow **terminal-notifier** in System Settings → Notifications, or turn off Focus.
  - V-9 / V-11 (Plan 10) and V-12 (Plan 12): **user-driven CPU spikes**. First measure Claude's idle Total CPU, then run with `--cpu-window 6 --cpu-threshold <idle + 5>`. The user uses Claude briefly (scrolling, typing) to spike CPU, then stops so the flag clears.
  - V-10 (Plan 11): `kill -TERM` on a headless run; a TUI in a tmux pane whose pane is then killed (SIGHUP); `q` and Ctrl-C in the TUI.

Time-sensitive (v1 only): the Claude `.diag` report above is inside the 24 h startup window only until **2026-09-26 21:34 +08:00**. After that, verify V-5 with the Plan 05 fixtures. This does not affect v2.

## 10. Working with the user

- Communication: concise and direct. Explain the *why* behind technical choices briefly; point to file paths and functions rather than long explanations.
- **Ambiguity:** don't guess. Ask with the AskUserQuestion tool, **one question per call**, with your recommendation first, labelled "(Recommended)". If they answer with free text, e.g. "research this" or "use X", do that and re-ask the same item before moving on.
- Choices between alternatives: present a short comparison table (pros/cons, concrete numbers) and let the user decide.
- Report outcomes faithfully. Failures are shown with their actual output; skipped steps are stated as skipped.
- Record any new requirement-level decision in the requirements file for the plan's version (`requirements/REQUIREMENTS-v1.md` for plans 01–08, `requirements/REQUIREMENTS-v2.md` for plans 09–12), and any plan-level one in the plan or status note, in the same commit.
