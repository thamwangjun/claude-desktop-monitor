# Plan 14: Runtime changes for 0.3.0

Part of the Claude Desktop Resource Monitor v3 build. Full requirements: [`../requirements/REQUIREMENTS-v3.md`](../requirements/REQUIREMENTS-v3.md) (incremental on v1 and v2). Execution order and rationale: [`index.md`](index.md).

## Context

After Plan 13 the monitor is a package but behaves exactly as before. An installed command, though, is run from any directory, and today's default log path `./logs/monitor-<ts>.jsonl` is relative to the working directory, so logs would scatter (for example into `~/logs/`). Logs from different installs should also say which monitor version wrote them. These are the only user-visible changes in v3, kept out of Plan 13 so the rename commit contains no behaviour change.

Current code (REQUIREMENTS-v3 §3):

- `parse_args()` (§1) sets `args.log = str(Path("logs") / f"monitor-{timestamp}.jsonl")` when `--log` is absent.
- `LogWriter` (§7) already creates the log's parent directory (`Path(path).parent.mkdir(parents=True, exist_ok=True)`).
- `_collect_versions()` (§9) records python, psutil, rich, watchdog, macOS, Claude app and native-call availability.

## Goal

With no `--log`, logs land in `~/Library/Logs/claude-desktop-monitor/`; `session_start.versions.monitor` is `"0.3.0"`; the package version is 0.3.0.

## Prerequisites

Plan 13 complete.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| L-1 (amended) | Default log `~/Library/Logs/claude-desktop-monitor/monitor-YYYYmmdd-HHMMSS.jsonl`; directory created if missing; `--log` overrides; no migration; unit tests never write the real directory. |
| L-8 | `session_start.versions.monitor` = installed package version. |
| E-11 | Version 0.3.0, declared once in `pyproject.toml`, read with `importlib.metadata`. |
| W-2 (part) | `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md`: log location (and that it is the monitor's own directory, not `~/Library/Logs/Claude/`). |
| V-14 | Dev runs: TUI, headless via `python -m`, default log location, `versions.monitor == "0.3.0"`. |
| V-13 (part) | No test writes to `~/Library/Logs/claude-desktop-monitor/`. |

## Design

§1:

```python
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "claude-desktop-monitor"
```

`parse_args()` uses `DEFAULT_LOG_DIR / f"monitor-{timestamp}.jsonl"`. The `--log` help text shows `~/Library/Logs/claude-desktop-monitor/monitor-<timestamp>.jsonl`. Directory creation is unchanged (`LogWriter`).

§9, `_collect_versions()`:

```python
try:
    versions["monitor"] = importlib.metadata.version("claude-desktop-monitor")
except importlib.metadata.PackageNotFoundError:
    versions["monitor"] = "unknown"
```

`pyproject.toml`: `version = "0.3.0"`; `uv sync` updates `uv.lock`.

## Tasks

1. Before touching tests: record whether `~/Library/Logs/claude-desktop-monitor/` exists (expected: it doesn't).
2. `DEFAULT_LOG_DIR`, `parse_args()` default and `--log` help text.
3. `versions["monitor"]` in `_collect_versions()`.
4. Version 0.3.0 in `pyproject.toml`; `uv sync`.
5. Tests:
   - `tests/test_cli.py`: the default-log assertion becomes "under `DEFAULT_LOG_DIR`, named `monitor-<ts>.jsonl`" (`parse_args` only builds the string; nothing is created).
   - A test that `session_start.versions["monitor"]` equals `importlib.metadata.version("claude-desktop-monitor")`.
   - Audit every test that reaches `run()`/`main()`/`LogWriter`: each must pass `--log` under `tmp_path`. Fix any that don't.
6. Docs: `CLAUDE.md`, `PLAN_EXEC_INSTRUCTIONS.md` (log location, hard-rule note that `~/Library/Logs/claude-desktop-monitor/` is the monitor's own directory and `~/Library/Logs/Claude/` stays read-only); `README.md` lines that state the default log path.

## Verification (done when)

- [ ] `uv run pytest` passes (whole suite).
- [ ] V-13 (part): `~/Library/Logs/claude-desktop-monitor/` still doesn't exist after the suite (or is unchanged if it existed at task 1).
- [ ] V-14, headless: `uv run python -m claude_desktop_monitor --no-tui` (no `--log`) for ~15 s, then SIGINT. A new `monitor-<ts>.jsonl` exists in `~/Library/Logs/claude-desktop-monitor/`; its `session_start.versions.monitor` is `"0.3.0"`.
- [ ] V-14, TUI (user): `uv run claude-desktop-monitor`: the dashboard renders, the header shows the new log path, `q` exits cleanly.
- [ ] `--log /tmp/cdm-p14.jsonl` still writes there and nothing new appears in the default directory.
- [ ] Committed, message prefixed `plan 14:`; status recorded in `plans/index.md`.

## Plan-level decisions (not specified in REQUIREMENTS-v3)

- `DEFAULT_LOG_DIR` is a module constant in §1, so tests can refer to it (and monkeypatch it if ever needed).
- If the package metadata is missing (running the file outside an install), `versions.monitor` is `"unknown"`, matching how `_collect_versions()` reports missing dependencies.
- The V-14 headless run writes one real log into `~/Library/Logs/claude-desktop-monitor/`; it is left there (it is the monitor's own directory and proves the default).

## Out of scope

Migrating old `./logs/` files (non-goal). A `--version` flag (non-goal). README restructure (Plan 15).
