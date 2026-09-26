# Claude Desktop Resource Monitor — Requirements v3.1

Status: agreed requirements, not yet implemented (plans 18–19). Grey areas 1–11 resolved with the user 2026-09-27.

v3.1 is an **incremental** change on top of [`REQUIREMENTS-v1.md`](REQUIREMENTS-v1.md), [`REQUIREMENTS-v2.md`](REQUIREMENTS-v2.md) and [`REQUIREMENTS-v3.md`](REQUIREMENTS-v3.md), which remain authoritative for everything not mentioned here. Where v3.1 amends an item, v3.1 wins. v1 (plans 01–08), v2 (plans 09–12) and v3 (plans 13–17, 0.3.0 published to the tap) are fully implemented.

## 1. Purpose

Raise the default CPU alert threshold from **30%** to **90% of one core**, and ship it as **0.3.1** through the Homebrew tap.

Everything else is unchanged: the CPU unit, the rolling window, baselines, the flag state machine, notification tiers and wording, the log format and all other flags.

## 2. Non-goals

- No change to the CPU unit (v1 C-1: % of one core, can exceed 100%). The threshold is not normalised by core count.
- No per-role or per-total thresholds and no new CLI flags; one `--cpu-threshold` still applies to every role and to Total Claude.
- No change to clear hysteresis (10%, shared by all rules).
- No change to the notification tier of `cpu:*` (v2 N-12: warning).
- No live induced-CPU check and no real banners in v3.1 verification.
- **Idle climbs below 90% no longer raise a flag by default.** The #22543 pattern (Total Claude idle 24% → 55%) stays visible in the TUI's current-vs-baseline column and in the log, and `--cpu-threshold 30` (or lower) restores alerting on it. The #87794 (~120%) and #26194 (~316%) patterns still flag at the default.

## 3. Reference environment

Checked 2026-09-27.

**Current code** (commit `9e1a4c9`):
- `--cpu-threshold` defaults to `30.0` (`monitor.py` §1 `build_parser`; help text "CPU alert threshold, % of one core (default 30)").
- `Analyzer.__init__` (§6) has its own fallback `argparse.Namespace` for when no `args` are passed, also with `cpu_threshold=30.0`. Nothing currently calls `Analyzer()` without args.
- Tests pass explicit thresholds (`make_args(cpu_threshold=30.0)` in `test_analysis.py`, `test_tui.py`, `test_lifecycle.py`, `test_notify.py`); they test the rule, not the default. Only `tests/test_cli.py::test_defaults` asserts the default (`== 30.0`).
- `Analyzer` raises `cpu:<role>` when the rolling average is `> cpu_threshold` and clears it when `< 0.9 × cpu_threshold` (`_CLEAR_HYSTERESIS`).
- `session_start.config` records every CLI argument, including `cpu_threshold`; `session_start.versions.monitor` records the package version (v3 L-8).
- Package version 0.3.0; tag `v0.3.0`; the tap's `main` has the 0.3.0 formula with an `arm64_tahoe` bottle.

## 4. Functional requirements

| ID | Requirement |
|---|---|
| C-3 (amended) | Flag a role/total when its rolling average exceeds **90%** of one core (`--cpu-threshold`, default **`90`**). It applies to every role and Total Claude, as before. The flag clears below 81% (unchanged 10% hysteresis). Amends v1 C-3. |
| E-11 (applied) | Version **0.3.1**, declared once in `pyproject.toml` (v3 E-11). Tag `v0.3.1`. |

## 5. Documentation

| ID | Requirement |
|---|---|
| W-3 | README: the CPU row of the alert-rules table and the `--cpu-threshold` row of the flags table show 90. The metric → issue map's #22543 entry and the "CPU climb" bullet in "Reading the signals" say that at the default an idle climb like #22543's is seen through current vs. baseline (TUI and log), not a flag, and that `--cpu-threshold 30` (or lower) alerts on it. The 9-citation rule (v1 §11) is unchanged. |
| W-4 | `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md`: add `REQUIREMENTS-v3.1.md` to the authority chain, record v3 (plans 13–17) as done and v3.1 (plans 18–19) as the current work, add the v3.1 plans, user steps and decision-recording rule. Done together with the plans (v3 precedent, commit `3f21fac`). Plan 18 bumps the stated package version to 0.3.1; Plan 19 marks v3.1 done. `plans/index.md` gets a v3.1 section. |

## 6. CLI summary

No new flags. Changed default:

| Flag | Default |
|---|---|
| `--cpu-threshold PCT` | `90` (was `30`), % of one core |

## 7. Release

| ID | Requirement |
|---|---|
| B-10 | Release 0.3.1 through the v3 B-8 process: the user tags `v0.3.1` and pushes → the formula's `url`/`sha256` are updated on the tap branch `claude-desktop-monitor-0.3.1` → the user opens the PR → tap CI builds the arm64 macOS 26 bottle → the user publishes (generated "brew pr-pull" workflow or `brew pr-pull --head-sha=<sha>`). Commit message `claude-desktop-monitor 0.3.1` (v3 B-9). Runtime dependencies are unchanged, so the formula's `resource` blocks stay the same. This is confirmed by `git diff v0.3.0 v0.3.1 -- uv.lock`, which should show only the project's own version. (`brew update-python-resources` re-resolves against current PyPI and could pull in releases newer than `uv.lock`, so it is used only if that diff shows a dependency change.) |

## 8. Deliverables

Source repository:
1. `src/claude_desktop_monitor/monitor.py`: `--cpu-threshold` default and help text, and the matching `cpu_threshold` in `Analyzer`'s fallback Namespace (C-3), so the two defaults can't drift apart.
2. `pyproject.toml` and `uv.lock`: version 0.3.1.
3. `tests/`: `test_cli.py::test_defaults` updated to 90, plus a test of the rule at the new default (V-20). Fixtures with explicit thresholds stay as they are.
4. `README.md` (W-3), `CLAUDE.md`, `PLAN_EXEC_INSTRUCTIONS.md` and `plans/index.md` (W-4).
5. This `requirements/REQUIREMENTS-v3.1.md`, plus `plans/18-cpu-default.md` and `plans/19-release-0.3.1.md`.

Tap repository (`../homebrew-tap`):
6. `Formula/claude-desktop-monitor.rb` bumped to 0.3.1 (B-10), then the `bottle do` block added when the user publishes.

## 9. Verification

As in v3, live checks are done **one at a time, each confirmed with the user**. The user does anything that pushes, tags, opens PRs or publishes.

| ID | Requirement |
|---|---|
| V-20 | Full test suite passes. Tests check that the parser default is 90, and that at the default threshold a steady average of 89 does not raise `cpu:total`, a steady 91 raises it, a steady 82 does not clear it, and a steady 80 clears it. No test writes to `~/Library/Logs/claude-desktop-monitor/`. |
| V-21 | Dev copy: `uv run claude-desktop-monitor --help` shows default 90. A short headless run with `--log` under `/tmp` writes `session_start.config.cpu_threshold == 90` and `session_start.versions.monitor == "0.3.1"`. |
| V-22 | Tap CI on the 0.3.1 PR builds the bottle and passes. Publishing commits the `bottle do` block and uploads the bottle to the tap's Releases. |
| V-23 | On the reference machine: `brew upgrade claude-desktop-monitor` **pours the 0.3.1 bottle** (`brew list --versions rust` empty before and after); `brew test` and `brew audit --strict` pass; the installed `claude-desktop-monitor --help` shows default 90; a short headless run of the installed copy (`--log` under `/tmp`) writes `session_start.config.cpu_threshold == 90` and `session_start.versions.monitor == "0.3.1"`. |

## 10. Plans

| Plan | Scope | Ends at |
|---|---|---|
| 18: CPU default and 0.3.1 prep | C-3, E-11, W-3, W-4; V-20, V-21 | User pushes `main` and tags `v0.3.1`; archive sha256 recorded |
| 19: Formula bump, bottle, publish | B-10; V-22, V-23 | 0.3.1 bottle poured and verified on the reference machine |

The split falls at the `v0.3.1` tag. Everything before it happens in one repository, can be undone, and is verified locally. Everything after it depends on an immutable tag and runs through user gates (PR, publish). The docs stay in plan 18 because they must match the tagged code. The formula bump and publishing stay together because a version bump is mechanical: v3 split them (16/17) only because of the new formula, V-19 and the first audit.

## 11. Decision log (grey areas)

| # | Grey area | Decision |
|---|---|---|
| 1 | Meaning of "90% per core" | 90% of one core, the existing unit (v1 C-1); only the default changes |
| 2 | Rows the default applies to | All roles and Total Claude, one value; no new flags |
| 3 | Clear hysteresis | Keep 10% (clears below 81%) |
| 4 | `cpu:*` notification tier | Keep warning (v2 N-12) |
| 5 | Release scope | Bump to 0.3.1 and ship through the tap (B-10) |
| 6 | Document name | `requirements/REQUIREMENTS-v3.1.md` |
| 7 | Plan split | Two plans, 18 and 19, split at the `v0.3.1` tag (§10) |
| 8 | Verification | Tests plus dev- and installed-copy checks; no live induced CPU load, no real banners |
| 9 | #22543 idle climb no longer flags at 90 | Accepted; documented in §2 and the README (W-3), with `--cpu-threshold 30` as the way to alert on it |
| 10 | Resource check for the bump (B-10) | `git diff v0.3.0 v0.3.1 -- uv.lock`; `brew update-python-resources` only if a dependency changed |
| 11 | When the execution guide is updated | With the plans, as in v3; Plan 18 only bumps the version, Plan 19 marks v3.1 done |

## 12. Relation to earlier requirements

| Earlier item | Change |
|---|---|
| v1 C-3 CPU threshold | Default 30 → 90 (§4) |
| v1 §5 alert-rules table, CPU row | Default "30% of one core" → "90% of one core" |
| v1 §6 CLI `--cpu-threshold` | Default 30 → 90 (§6) |
| v3 E-11 version | 0.3.0 → 0.3.1 |
| v3 B-8 release process | Followed as written (B-10) |
| v2 V-9/V-12 procedure (`--cpu-threshold <idle + 5>`) | Unaffected; it overrides the threshold explicitly |
| v1 §11 / README issue map, #22543 "CPU climb" | Still cited; wording notes that the default no longer flags it (W-3) |
