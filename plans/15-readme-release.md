# Plan 15: README and release

Part of the Claude Desktop Resource Monitor v3 build. Full requirements: [`../requirements/REQUIREMENTS-v3.md`](../requirements/REQUIREMENTS-v3.md) (incremental on v1 and v2). Execution order and rationale: [`index.md`](index.md).

## Context

Plans 13–14 make the code releasable. The first public release is tag `v0.3.0` on `thamwangjun/claude-desktop-monitor`: the Homebrew formula (Plan 16) downloads that tag's archive, and the tag's tree is what people see on GitHub. So the README must describe the final install and behaviour **before** the tag. Pushing and tagging are the user's (git rules: never push unless asked), so this plan ends at a user gate.

## Goal

A README that leads with the Homebrew install, and a pushed `v0.3.0` tag whose archive checksum is recorded for Plan 16.

## Prerequisites

Plans 13 and 14 complete. The user has created the empty public repositories (done 2026-09-27).

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| W-1 | README: Homebrew install first; developer setup (mise + uv, `uv run claude-desktop-monitor`) second; default log location; license; no `uv run monitor.py`; v1 §11 citations rule unchanged. |
| E-11 | Release tag `v0.3.0`. |
| B-8 (first release) | The user tags and pushes. |

## README changes

1. **Install** (new first section):
   - `brew install thamwangjun/tap/claude-desktop-monitor`, then `claude-desktop-monitor`.
   - Requirements: macOS 26+ (Homebrew `terminal-notifier` 3.1.0 needs it). Apple Silicon gets a prebuilt bottle; other Macs build from source, and Homebrew installs Rust temporarily as a build dependency.
   - Upgrading and uninstalling (`brew upgrade`, `brew uninstall`); logs stay in `~/Library/Logs/claude-desktop-monitor/`.
2. **Developer setup** (the existing mise + uv instructions): `mise install`, `uv sync`, `uv run claude-desktop-monitor`, `python -m claude_desktop_monitor`, `uv run pytest`. The Rust note is added in Plan 16, when the pin lands.
3. **Notifications → setup and troubleshooting**: a Homebrew install already depends on `terminal-notifier` and the command always uses it (B-5). The `brew install terminal-notifier` advice stays for developer runs.
4. **Logs**: default location `~/Library/Logs/claude-desktop-monitor/`, `--log` to override; `session_start.versions.monitor`.
5. **License**: GPL-3.0-only, pointing to `LICENSE`.
6. Every command example uses `claude-desktop-monitor` (installed) or `uv run claude-desktop-monitor` (developer).

The metric → issue map and its 9 citations are unchanged.

## Tasks

1. README changes above.
2. Pre-release checks (below).
3. Commit `plan 15: …` (README + status `Done (pending tag)`).
4. **User gate.** Ask the user to run (fish-compatible; SSH or HTTPS remote is their choice):

   ```
   git remote add origin git@github.com:thamwangjun/claude-desktop-monitor.git
   git push -u origin main
   git tag -a v0.3.0 -m "v0.3.0"
   git push origin v0.3.0
   ```

5. After the user reports done, read-only checks: `git ls-remote --tags origin` shows `v0.3.0`; download `https://github.com/thamwangjun/claude-desktop-monitor/archive/refs/tags/v0.3.0.tar.gz`, record its `sha256` (`shasum -a 256`) and check it contains `src/claude_desktop_monitor/monitor.py`, the icon, `LICENSE` and `pyproject.toml`.
6. Record the tag, commit hash and archive `sha256` in the plan 15 status (a follow-up `plan 15: record status` commit, as in v1).

## Verification (done when)

- [ ] `uv run pytest` passes; `git status` clean before the gate.
- [ ] README: Homebrew install first, developer setup second, log location, license; `grep -c 'monitor\.py' README.md` finds no run command; only the 9 approved issues are cited.
- [ ] `v0.3.0` exists on GitHub and points at the plan 15 commit.
- [ ] Archive `sha256` recorded; archive contents as in task 5.
- [ ] Status recorded in `plans/index.md`.

## Plan-level decisions (not specified in REQUIREMENTS-v3)

- The tag is annotated (`git tag -a`), so it records who tagged and when.
- The user adds the remote as well as pushing, choosing SSH or HTTPS.
- The README states the Homebrew install before the formula exists; it is proved by V-17 in Plan 17.

## Out of scope

The formula and tap (Plan 16). Changing the metric → issue map.
