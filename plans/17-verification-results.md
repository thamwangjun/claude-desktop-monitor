# Plan 17: v3 verification results

Machine: Apple M5 (Mac17,4), 10 CPUs, 32 GB RAM, macOS 26.7 (25G229). Homebrew 7.0.6-70-gce46735. Python 3.14.7. Homebrew `terminal-notifier` 3.1.0. Claude Desktop 2.9939.2. Date: 2026-09-27.

| Check | Result |
|---|---|
| V-13 | **Pass** — carried from Plan 13; reconfirmed by the full-suite rerun below (no test writes to `~/Library/Logs/claude-desktop-monitor/`). |
| V-14 | **Pass** — carried from Plan 14 (dev-run and TUI checks already verified there). |
| V-15 | **Pass** — carried from Plan 15 (`uv build` contents, tag, archive sha256 already verified there). |
| V-16 | **Pass** — see below. |
| V-17 | **Pass** — see below. |
| V-18 | **Pass** — see below. |
| V-19 | **Pass** — carried from Plan 16 (local from-source build with mise's Rust, before the first formula PR). |

## V-16 — publish

The user triggered the tap's generated "brew pr-pull" workflow (`gh workflow run publish.yml -R thamwangjun/homebrew-tap -f pull_request=1 -f head_sha=faf6cbc8928fce7ff33e423afcfc01d25bbc5d95`), run [36265716650](https://github.com/thamwangjun/homebrew-tap/actions/runs/36265716650), which completed `success`.

Read-only checks after the run:

```
$ git log origin/main --oneline -3
e9f9a9c claude-desktop-monitor: add 0.3.0 bottle.
1fd1631 claude-desktop-monitor 0.3.0 (new formula)
b41df3e workflows: add brew tap-new CI

$ git show origin/main:Formula/claude-desktop-monitor.rb | sed -n '/bottle do/,/end/p'
  bottle do
    root_url "https://github.com/thamwangjun/homebrew-tap/releases/download/claude-desktop-monitor-0.3.0"
    sha256 cellar: :any_skip_relocation, arm64_tahoe: "c6952a97063b708f0dd3a51e1c9d0a4442f311d8722f2e9e180592ce7b824785"
  end

$ gh release list -R thamwangjun/homebrew-tap
claude-desktop-monitor-0.3.0	Latest	claude-desktop-monitor-0.3.0	2026-09-26T19:21:31Z
```

PR #1 closed (not merged via GitHub's merge button — `brew pr-pull` pushes the bottle commit directly to `main` and closes the PR, which is its documented behaviour). **Pass.**

## V-17 — install from the tap

```
$ brew untap thamwangjun/tap
Untapping thamwangjun/tap...
Untapped 1 formula (51 files, 34.9KB).

$ brew list --versions rust
(exit 1 — not installed)

$ brew tap thamwangjun/tap
==> Tapping thamwangjun/tap
Tapped 1 formula (18 files, 23.4KB).

$ brew install thamwangjun/tap/claude-desktop-monitor
==> Trusted formula thamwangjun/tap/claude-desktop-monitor
==> Fetching downloads for: claude-desktop-monitor
✔︎ Bottle claude-desktop-monitor (0.3.0)
==> Pouring claude-desktop-monitor-0.3.0.arm64_tahoe.bottle.tar.gz
🍺  /opt/homebrew/Cellar/claude-desktop-monitor/0.3.0: 663 files, 7.7MB

$ brew list --versions rust
(exit 1 — still not installed)
```

The bottle was poured (no source build, no Rust step in the install log). `ca-certificates` was upgraded as an ordinary dependency refresh, unrelated to this formula's own build.

```
$ brew test thamwangjun/tap/claude-desktop-monitor
==> Testing thamwangjun/tap/claude-desktop-monitor
==> /opt/homebrew/Cellar/claude-desktop-monitor/0.3.0/bin/claude-desktop-monitor
==> /opt/homebrew/Cellar/claude-desktop-monitor/0.3.0/libexec/bin/python -c impo...
(no error; exit 0)

$ brew audit --strict --online thamwangjun/tap/claude-desktop-monitor
(no output; exit 0)
```

**Pass.**

## V-18 — installed command

```
$ cat "$(brew --prefix)/bin/claude-desktop-monitor"
#!/bin/bash
PATH="/opt/homebrew/opt/terminal-notifier/bin:$PATH" exec "/opt/homebrew/Cellar/claude-desktop-monitor/0.3.0/libexec/bin/claude-desktop-monitor" "$@"
```

Homebrew `terminal-notifier`'s `opt` bin is prepended to `PATH` ahead of `exec`, confirming B-5.

User ran `claude-desktop-monitor` interactively: dashboard rendered, header showed the log path under `~/Library/Logs/claude-desktop-monitor/`, `q` exited cleanly with the terminal restored. **Pass** (user-confirmed).

Headless, no `--log`, ~15 s + SIGINT:

```
$ "$(brew --prefix)/bin/claude-desktop-monitor" --no-tui   # 15s, then SIGINT
```

Wrote `~/Library/Logs/claude-desktop-monitor/monitor-20260927-032332.jsonl`. `session_start.versions` (first record):

```json
{"python": "3.14.7", "psutil": "7.2.2", "monitor": "0.3.0", ...}
```

`session_start.versions.monitor == "0.3.0"`. Last record: `{"type": "session_end", "reason": "signal", "signal": "SIGINT"}` — clean shutdown. **Pass.**

Notification banners, with the user's agreement, minimal PATH to prove the wrapper (not the shell) supplies Homebrew's `terminal-notifier`:

```
$ env -i HOME="$HOME" PATH=/usr/bin:/bin "$(brew --prefix)/bin/claude-desktop-monitor" --notify-test
warning: sent
critical: sent
A successful send cannot prove the banner was shown. If nothing appeared, check System Settings → Notifications → terminal-notifier, and any Focus mode.
exit:0
```

User confirmed both banners appeared (warning/Glass then critical/Basso) with the Claude-logo thumbnail, and no Rosetta/Intel warning prompt — proving the arm64 Homebrew `terminal-notifier` was used via the wrapper's `PATH`, not any copy on the user's own shell `PATH` (deliberately emptied by `env -i`). **Pass.**

## Housekeeping

`brew developer` reported disabled at the start. `brew test` (a developer command) turned it back on; `brew developer off` was run afterward and `brew developer` confirmed disabled again. `brew list --versions rust` stayed empty throughout V-17/V-18. `uv run pytest` on the final tree: 172 passed, 1 warning (pre-existing, unrelated `os.fork()` `DeprecationWarning` in `test_native.py`), 3.47s.

## Defects found

None. Plans 13–16's implementation held up under live end-to-end verification with no code or formula changes required in this plan.
