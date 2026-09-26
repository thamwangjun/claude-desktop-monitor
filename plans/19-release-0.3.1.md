# Plan 19: Formula bump, bottle, publish

Part of the Claude Desktop Resource Monitor v3.1 build. Full requirements: [`../requirements/REQUIREMENTS-v3.1.md`](../requirements/REQUIREMENTS-v3.1.md) (incremental on v1, v2 and v3). Execution order and rationale: [`index.md`](index.md).

## Context

Plan 18 ends with `v0.3.1` tagged and its archive sha256 recorded. Homebrew users only get the new CPU default once the tap's formula points at that tag and a new bottle is published. This follows the v3 B-8 release process for the first time as a version bump rather than a new formula. It's mechanical: two lines change in the formula (`url`, `sha256`), no dependencies change, and the formula's install logic, wrapper and `test do` stay as they are.

State of the tap (checked 2026-09-27):

- `thamwangjun/homebrew-tap` `main` has the 0.3.0 formula with a `bottle do` block (`arm64_tahoe`) from the Plan 17 publish (`e9f9a9c`).
- The local clone `../homebrew-tap` is still on the old branch `claude-desktop-monitor-0.3.0` and hasn't fetched the published `main`.
- The installed copy on the reference machine is 0.3.0 from the real GitHub tap (Plan 17 re-tapped it).

## Goal

A published 0.3.1 bottle, and the reference machine upgraded to it by pouring (no source build), with the installed command defaulting to 90.

## Prerequisites

Plan 18 complete: `v0.3.1` on GitHub, archive sha256 recorded in the plan 18 status.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| B-10 | Formula `url`/`sha256` bumped on branch `claude-desktop-monitor-0.3.1`; commit `claude-desktop-monitor 0.3.1`; user opens PR; CI builds the bottle; user publishes. Resources unchanged (checked against `uv.lock`). |
| V-22 | Tap CI builds the 0.3.1 bottle and passes; publishing commits the `bottle do` block and uploads the bottle. |
| V-23 | `brew upgrade` pours the 0.3.1 bottle (no Rust); `brew test`, `brew audit --strict` pass; installed `--help` shows 90; installed headless run logs `config.cpu_threshold == 90` and `versions.monitor == "0.3.1"`. |

## Design

`Formula/claude-desktop-monitor.rb`:

```ruby
url "https://github.com/thamwangjun/claude-desktop-monitor/archive/refs/tags/v0.3.1.tar.gz"
sha256 "<archive sha256 from plan 18>"
```

The existing `bottle do` block is left as it is. Publishing replaces it with the 0.3.1 bottle (as homebrew-core version bumps do). Resource blocks, `depends_on`, `install` (including the removal of pync's vendored `terminal-notifier.app`) and `test do` are unchanged.

## Tasks

1. `brew developer` state noted (expected: off). `brew list --versions rust` empty.
2. Resource check: `git diff v0.3.0 v0.3.1 -- uv.lock` shows only the `claude-desktop-monitor` version. If a dependency changed, stop and ask. That would need `brew update-python-resources` and falls outside B-10's assumptions.
3. In `../homebrew-tap`: `git fetch origin`, `git switch -c claude-desktop-monitor-0.3.1 origin/main`. Confirm the formula there has the `bottle do` block from Plan 17.
4. Edit `url` and `sha256`. `brew style ../homebrew-tap/Formula/claude-desktop-monitor.rb` is clean.
5. Commit in the tap: subject `claude-desktop-monitor 0.3.1`, body ending with the attribution lines (B-9).
6. `brew developer off` if task 4 switched it on.
7. **User gate.** Ask the user to push the branch and open the PR:

   ```
   git -C ../homebrew-tap push -u origin claude-desktop-monitor-0.3.1
   gh pr create -R thamwangjun/homebrew-tap --head claude-desktop-monitor-0.3.1 --fill
   ```

   `--fill` takes the title and body from the tap commit (task 5), so the PR description ends with the same attribution lines.

   (Or open the PR in the browser.)
8. V-22 (build), read-only: `gh pr checks <PR> -R thamwangjun/homebrew-tap` until done. The `macos-26` job passes from a source build and uploads a `bottles_macos-26` artifact, and the tap-syntax job passes. If CI fails, fix it on the branch with a new tap commit (the user pushes) and don't weaken any check.
9. **User gate.** The user publishes with the generated "brew pr-pull" workflow (PR number + head SHA), as in Plan 17.
10. V-22 (publish), read-only: the tap's `main` formula has `v0.3.1` in `url` and a new `arm64_tahoe` bottle sha256. `gh release list -R thamwangjun/homebrew-tap` shows `claude-desktop-monitor-0.3.1`. The PR is closed.
11. V-23, one step at a time:
    1. `brew update`; `brew list --versions rust` empty.
    2. `brew upgrade thamwangjun/tap/claude-desktop-monitor`: output shows the 0.3.1 bottle being poured and no Rust. `brew list --versions claude-desktop-monitor` shows 0.3.1, and `brew list --versions rust` is still empty.
    3. `brew test thamwangjun/tap/claude-desktop-monitor` and `brew audit --strict --online thamwangjun/tap/claude-desktop-monitor` pass.
    4. `"$(brew --prefix)/bin/claude-desktop-monitor" --help` shows `(default 90)`.
    5. `"$(brew --prefix)/bin/claude-desktop-monitor" --no-tui --log /tmp/cdm-p19.jsonl` for ~15 s, then SIGINT: `session_start.config.cpu_threshold == 90.0`, `session_start.versions.monitor == "0.3.1"`, clean `session_end`.
12. `brew developer off` (`brew test` switches it on); confirm with `brew developer`.
13. Local clone tidy-up: `git -C ../homebrew-tap switch main && git -C ../homebrew-tap pull --ff-only`, so the clone matches the published state. Delete the local branches `claude-desktop-monitor-0.3.0` and `-0.3.1` only if the user agrees.
14. `uv run pytest` once more on the final tree. Set `requirements/REQUIREMENTS-v3.1.md` status to "agreed requirements, implemented (plans 18–19)". Mark v3.1 done in the `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md` status lines (W-4). Record the plan 19 status.

## Verification (done when)

- [ ] Resources unchanged (task 2); `brew style` clean.
- [ ] V-22: PR CI green with a bottle; published: `bottle do` updated on `main`, release `claude-desktop-monitor-0.3.1` exists.
- [ ] V-23: bottle poured, Homebrew's `rust` never installed; `brew test` and `brew audit --strict --online` pass; installed `--help` shows 90; installed headless log shows `cpu_threshold == 90.0` and `versions.monitor == "0.3.1"`.
- [ ] Developer mode off at the end.
- [ ] `uv run pytest` passes.
- [ ] Committed, message prefixed `plan 19:`; status recorded in `plans/index.md`.

## Plan-level decisions (not specified in REQUIREMENTS-v3.1)

- The branch is cut from the published `origin/main`, not from the stale local `claude-desktop-monitor-0.3.0` branch, so it carries the 0.3.0 `bottle do` block and `pr-pull` has a block to replace.
- The formula is edited by hand, not with `brew bump-formula-pr`, because that command pushes and opens a PR itself, and pushing and PRs are the user's.
- Only `brew style` runs locally. `brew audit` needs the formula by tap name, and the tapped copy is the real GitHub tap, so audit runs in CI (V-22) and against the published formula (V-23).
- V-23 uses `brew upgrade` rather than untap/retap: the real GitHub tap has been installed since Plan 17, and an upgrade is the path existing users take.
- No live CPU-load check and no `--notify-test`. The notification path is unchanged since Plan 17's V-18 (REQUIREMENTS-v3.1 §2, decision 8).
- Results are recorded in the plan 19 status note, not a separate results file: the check list is short and fully listed in V-22/V-23.

## Out of scope

Formula logic changes, new bottle platforms, homebrew-core. Anything that needs a requirement change goes back to REQUIREMENTS-v3.1 first.
