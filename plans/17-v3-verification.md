# Plan 17: v3 verification

Part of the Claude Desktop Resource Monitor v3 build. Full requirements: [`../requirements/REQUIREMENTS-v3.md`](../requirements/REQUIREMENTS-v3.md) (incremental on v1 and v2). Execution order and rationale: [`index.md`](index.md).

## Context

Plan 16 ends with a formula PR whose CI built the bottle. This plan has the user publish it, then checks the published artefact end to end on the reference machine, as a user would install it, and records all v3 results in one file, as Plans 08 and 12 did.

## Goal

A published bottle, a verified `brew install` on the reference machine, and `plans/17-verification-results.md` recording V-13 to V-19 as passing.

## Prerequisites

Plan 16 complete: formula PR open with green CI.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| V-16 (publish) | Publishing commits the `bottle do` block and uploads the bottle to the tap's Releases. |
| V-17 | `brew install` pours the bottle; Homebrew's `rust` never installed; `brew test` and `brew audit --strict` pass. |
| V-18 | Installed command: TUI runs; wrapper puts Homebrew `terminal-notifier` first on PATH; `--notify-test` shows both banners with the thumbnail (with the user's agreement). |
| V-13 – V-19 | All v3 results recorded. |

## Verification checklist

Live checks are done **one at a time and confirmed with the user**.

| ID | How |
|---|---|
| V-16 | The user publishes (the generated "brew pr-pull" workflow with the PR number and head SHA, or `brew pr-pull --tap=thamwangjun/tap --head-sha=<sha> <PR>`). Then, read-only: the tap's `main` has the formula with a `bottle do` block containing an `arm64_tahoe` entry; `gh release list -R thamwangjun/homebrew-tap` shows the bottle release. |
| V-17 | Replace the dev tap: `brew untap thamwangjun/tap`, `brew tap thamwangjun/tap`. `brew list --versions rust` empty. `brew install thamwangjun/tap/claude-desktop-monitor`: output shows the bottle being poured and no Rust. `brew list --versions rust` still empty. `brew test thamwangjun/tap/claude-desktop-monitor` and `brew audit --strict --online thamwangjun/tap/claude-desktop-monitor` pass. |
| V-18 | `cat "$(brew --prefix)/bin/claude-desktop-monitor"` shows the wrapper exporting `PATH` with `terminal-notifier`'s `opt/.../bin` first. The user runs `claude-desktop-monitor` (TUI renders, log path in the header is under `~/Library/Logs/claude-desktop-monitor/`, `q` exits). Headless, no `--log`, ~15 s: `session_start.versions.monitor == "0.3.0"`. With the user's agreement: `env -i HOME="$HOME" PATH=/usr/bin:/bin "$(brew --prefix)/bin/claude-desktop-monitor" --notify-test` shows the warning (Glass) and critical (Basso) banners with the Claude thumbnail, exit 0, and no Rosetta prompt: the minimal PATH proves the wrapper, not the shell, supplied Homebrew's `terminal-notifier`. |
| V-13 – V-15, V-19 | Carried from Plans 13, 14 and 16 (evidence copied from their status notes); `uv run pytest` rerun once on the final tree. |

Results go in `plans/17-verification-results.md`: machine, versions (macOS, Homebrew, Python, `terminal-notifier`, Claude Desktop), date, pass/fail per check, evidence excerpts.

## Tasks

1. User gate: publish (V-16). Then the read-only checks.
2. V-17 and V-18, one at a time.
3. `brew developer off` if any developer command switched it on; confirm with `brew developer`.
4. Write `plans/17-verification-results.md`; set `requirements/REQUIREMENTS-v3.md` status to "agreed requirements, implemented (plans 13–17)".
5. A defect found here is fixed in the plan that owns it (source repository or tap), named in the commit message; a tap fix goes through a new tap PR (B-8).

## Verification (done when)

- [ ] V-16 to V-18 pass; V-13 to V-19 recorded as passing in `plans/17-verification-results.md`.
- [ ] Homebrew's `rust` not installed; developer mode off.
- [ ] Committed, message prefixed `plan 17:`; status recorded.

## Plan-level decisions (not specified in REQUIREMENTS-v3)

- Publishing through the generated workflow is recommended over a local `brew pr-pull`, which needs a GitHub token in the user's shell and developer mode.
- The notification check runs with `env -i` and a minimal PATH, to prove B-5 rather than rely on the user's shell PATH.
- V-13 to V-15 and V-19 are not repeated in full; the suite is rerun once on the final tree.

## Out of scope

New features; Intel bottles; homebrew-core. Anything found here that needs a requirement change goes back to REQUIREMENTS-v3 first.
