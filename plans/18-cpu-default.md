# Plan 18: CPU default and 0.3.1 prep

Part of the Claude Desktop Resource Monitor v3.1 build. Full requirements: [`../requirements/REQUIREMENTS-v3.1.md`](../requirements/REQUIREMENTS-v3.1.md) (incremental on v1, v2 and v3). Execution order and rationale: [`index.md`](index.md).

## Context

The default CPU threshold of 30% of one core (v1 C-3) is lower than the user wants. v3.1 raises the default to 90% of one core and ships it as 0.3.1. The unit, the rolling window, the 10% clear hysteresis, the notification tier and the wording all stay the same (REQUIREMENTS-v3.1 §2).

The Homebrew formula (Plan 19) downloads the tag's archive, so code, version and docs must all be final before `v0.3.1`. This plan ends at that user gate.

Current code (REQUIREMENTS-v3.1 §3):

- `build_parser()` (§1): `--cpu-threshold` `default=30.0`, help "CPU alert threshold, %% of one core (default 30)".
- `Analyzer.__init__` (§6): fallback `argparse.Namespace(... cpu_threshold=30.0 ...)` for when no `args` are passed. Nothing currently calls `Analyzer()` without args.
- `Analyzer._cpu_flag` (§6): raises when `avg > cpu_threshold` and clears when `avg < _CLEAR_HYSTERESIS * cpu_threshold` (0.9).
- Tests pass explicit thresholds through `make_args(cpu_threshold=30.0)` in `test_analysis.py`, `test_tui.py`, `test_lifecycle.py` and `test_notify.py`. These test the rule, not the default. Only `tests/test_cli.py::test_defaults` asserts the default.
- `pyproject.toml` `version = "0.3.0"`; `uv.lock` records the project version.

## Goal

With no `--cpu-threshold`, a CPU flag raises above 90% of one core and clears below 81%. The package version is 0.3.1, the docs match, and `v0.3.1` is pushed with its archive sha256 recorded for Plan 19.

## Prerequisites

v3 complete (plans 13–17; 0.3.0 published). `origin` remote configured (Plan 15).

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| C-3 (amended) | `--cpu-threshold` default 90 (% of one core); same value for every role and total; clears below 81%. |
| E-11 (applied) | Version 0.3.1 in `pyproject.toml`; tag `v0.3.1`. |
| W-3 | README alert-rules and flags tables show 90. |
| W-3 (#22543) | README issue map and "CPU climb" bullet: at the default, an idle climb is seen via current vs. baseline, not a flag; `--cpu-threshold 30` alerts on it. |
| W-4 (version) | `CLAUDE.md`, `PLAN_EXEC_INSTRUCTIONS.md`: stated package version 0.3.1. (The authority chain, v3 status and v3.1 plans were updated with the plans.) |
| V-20 | Suite passes; tests for the default and for raise/clear at the default. |
| V-21 | Dev copy: `--help` shows 90; headless run logs `config.cpu_threshold == 90` and `versions.monitor == "0.3.1"`. |
| B-8 / B-10 (tag) | The user pushes `main` and tags `v0.3.1`. |

## Design

§1, `build_parser()`:

```python
parser.add_argument("--cpu-threshold", type=float, default=90.0,
                     help="CPU alert threshold, %% of one core (default 90)")
```

§6, `Analyzer.__init__` fallback Namespace: `cpu_threshold=90.0`, so it can't drift from the CLI default.

No other code changes. The notification wording already includes the threshold value (`> <threshold>%`).

`pyproject.toml`: `version = "0.3.1"`; `uv sync` updates the project version in `uv.lock`, and nothing else in the lockfile should change.

## Tasks

1. §1 default and help text; §6 fallback Namespace.
2. `pyproject.toml` version 0.3.1; `uv sync`. Check `git diff uv.lock`: only the `claude-desktop-monitor` version line changes. If anything else changes, stop and ask.
3. Tests:
   - `tests/test_cli.py::test_defaults`: `args.cpu_threshold == 90.0`.
   - New `tests/test_analysis.py::test_cpu_default_threshold_raise_and_clear`, modelled on `test_cpu_raise_and_clear_with_hysteresis`. The threshold is taken from `monitor.parse_args([]).cpu_threshold`, so the test exercises the real default, with `make_args(cpu_window=4, interval=1, cpu_threshold=<that>)`. Steps on `total.cpu_pct`:
     1. 5.0 until the window is full, then a steady 89.0: no `cpu:total` raised. At the old default of 30 this would have raised.
     2. Steady 91.0: `cpu:total` raised, `details["avg"] > 90`.
     3. Steady 82.0 for a full window and more: not cleared (the average stays above 81).
     4. Steady 80.0: cleared.
   - Existing fixtures with explicit `cpu_threshold=30.0` stay as they are.
4. Docs:
   - `README.md`: the alert-rules CPU row ("30% of one core" → "90% of one core") and the flags-table `--cpu-threshold` row (`30` → `90`). `grep -n '30' README.md` afterwards to confirm no other CPU-default mentions.
   - `README.md` (#22543, REQUIREMENTS-v3.1 §2): in the issue map, the #22543 metric cell says the idle climb is seen via current vs. baseline. In "Reading the signals", the "CPU climb" bullet adds that the default threshold (90) doesn't flag a climb like 24% → 55%, so use `--cpu-threshold 30` (or lower) to be alerted. The `vm`-above-100% sentence stays as it is (still flagged).
   - `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md`: "the package version is 0.3.0" → 0.3.1.
5. Pre-release checks (below), then commit `plan 18: …` with status `Done (pending tag)`.
6. **User gate.** Ask the user to run:

   ```
   git push origin main
   git tag -a v0.3.1 -m "v0.3.1"
   git push origin v0.3.1
   ```

7. After the user reports done, read-only: `git ls-remote --tags origin` shows `v0.3.1`, pointing at the plan 18 commit. Download `https://github.com/thamwangjun/claude-desktop-monitor/archive/refs/tags/v0.3.1.tar.gz`, record its `sha256` (`shasum -a 256`), and check that its `src/claude_desktop_monitor/monitor.py` has `default=90.0` and its `pyproject.toml` has `version = "0.3.1"`.
8. Record the tag, commit hash and archive `sha256` in the plan 18 status (follow-up `plan 18: record tag and archive sha256` commit).

## Verification (done when)

- [ ] V-20: `uv run pytest` passes (whole suite), including the new test. `~/Library/Logs/claude-desktop-monitor/` has no new files from the suite.
- [ ] V-21: `uv run claude-desktop-monitor --help` shows `(default 90)`. `uv run claude-desktop-monitor --no-tui --log /tmp/cdm-p18.jsonl` for ~15 s, then SIGINT: `session_start.config.cpu_threshold == 90.0`, `session_start.versions.monitor == "0.3.1"`, clean `session_end`.
- [ ] `git diff v0.3.0 -- uv.lock` shows only the project version.
- [ ] README (thresholds and the #22543 wording), `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md` updated; README still cites only the 9 approved issues.
- [ ] `git status` clean before the gate.
- [ ] `v0.3.1` on GitHub; archive sha256 recorded; archive contents as in task 7.
- [ ] Committed, message prefixed `plan 18:`; status recorded in `plans/index.md`.

## Plan-level decisions (not specified in REQUIREMENTS-v3.1)

- The `Analyzer` fallback Namespace changes too, although nothing uses it today, so the two defaults can't disagree.
- The new test reads the threshold from `parse_args([])` instead of hard-coding 90, so it fails if the CLI default and the rule ever part ways.
- Existing tests with explicit `cpu_threshold=30.0` are not rewritten: they test the rule's mechanics at an arbitrary threshold, and the numbers in their comments (29/27) stay correct.
- The V-21 run uses `--log /tmp/...` rather than the default directory, since the default-log behaviour was proved in Plan 14.
- Annotated tag, as for `v0.3.0`.

## Out of scope

The formula, CI and publishing (Plan 19). Any change to CPU units, windows, hysteresis, tiers or wording (REQUIREMENTS-v3.1 §2).
