# Plan 13: Package conversion

Part of the Claude Desktop Resource Monitor v3 build. Full requirements: [`../requirements/REQUIREMENTS-v3.md`](../requirements/REQUIREMENTS-v3.md) (incremental on v1 and v2). Execution order and rationale: [`index.md`](index.md).

## Context

v3 distributes the monitor through a Homebrew tap. Homebrew installs Python applications with pip, so the single script `monitor.py` (`package = false`) must become an installable package with a console script. This plan changes **how the project is built and run** and nothing else: no runtime behaviour changes (those are Plan 14). Keeping it a pure move lets git record `monitor.py` as a rename, so `git log --follow` keeps its history.

Facts this plan relies on (REQUIREMENTS-v3 §3):

- `uv_build` expects `src/<package_name>/__init__.py`, with the package name derived from the project name (`claude-desktop-monitor` → `claude_desktop_monitor`).
- The wheel contains the whole module directory, so a PNG next to the code ships without extra configuration. Only `__pycache__`, `*.pyc` and `*.pyo` are excluded by default.
- The sdist contains `pyproject.toml`, the module, and the files named by `project.readme` and `project.license-files`.
- `NOTIFY_ICON = Path(__file__).resolve().parent / "assets" / "claude-icon.png"` keeps working once the icon sits in `src/claude_desktop_monitor/assets/`, so N-2 needs no code change.

## Goal

`uv run claude-desktop-monitor` and `python -m claude_desktop_monitor` run the unchanged monitor from `src/claude_desktop_monitor/`, the suite passes, and `uv build` produces a correct sdist and wheel.

## Prerequisites

v2 complete (plans 09–12). `requirements/REQUIREMENTS-v3.md` committed.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| E-1 (part) | mise pins uv **0.12.19** (was 0.12.18). |
| E-8 | Package `claude_desktop_monitor` in a `src/` layout; `monitor.py` moved as one module. |
| E-9 | Build backend `uv_build>=0.12.19,<0.13`; drop `package = false`; console script `claude-desktop-monitor`. |
| E-10 | Run with `uv run claude-desktop-monitor` or `python -m claude_desktop_monitor`; mise task updated; `uv run monitor.py` removed. |
| E-12 | `LICENSE` with the full GPL-3.0 text; `license = "GPL-3.0-only"`; `LICENSE` in the sdist. |
| E-13 (part) | No runtime dependency changes. (The Rust pin is Plan 16.) |
| N-2 (amended) | Icon moves to `src/claude_desktop_monitor/assets/claude-icon.png` and ships in the wheel. |
| W-2 (part) | `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md` updated for layout, commands and test imports. |
| §8.5 | `.gitignore` gains `dist/`. |
| V-13, V-15 | Suite passes after the move; `uv build` contents are correct. |

## Target layout

```
src/claude_desktop_monitor/
    __init__.py          # docstring only
    __main__.py          # python -m claude_desktop_monitor
    monitor.py           # git mv from ./monitor.py, content unchanged
    assets/claude-icon.png   # git mv from ./assets/claude-icon.png
LICENSE                  # new
pyproject.toml, uv.lock, mise.toml, .gitignore   # edited
tests/*.py               # import path changed
conftest.py              # deleted (see decisions)
```

`__main__.py`:

```python
import sys

from claude_desktop_monitor.monitor import main

sys.exit(main())
```

`pyproject.toml` (changed parts):

```toml
[project]
name = "claude-desktop-monitor"
version = "0.1.0"            # bumped to 0.3.0 in Plan 14
description = "Watch Claude Desktop's resource usage over time: TUI, JSONL log, flags and macOS notifications"
readme = "README.md"
license = "GPL-3.0-only"
license-files = ["LICENSE"]
requires-python = ">=3.14"
dependencies = [...]         # unchanged

[project.scripts]
claude-desktop-monitor = "claude_desktop_monitor.monitor:main"

[build-system]
requires = ["uv_build>=0.12.19,<0.13"]
build-backend = "uv_build"
```

`[tool.uv] package = false` is removed. `main()` already returns an `int`, which the console-script wrapper passes to `sys.exit`.

## Tasks

1. `mise.toml`: `uv = "0.12.19"`; task `monitor` runs `uv run claude-desktop-monitor`. Run `mise install`.
2. `git mv monitor.py src/claude_desktop_monitor/monitor.py` and `git mv assets/claude-icon.png src/claude_desktop_monitor/assets/claude-icon.png` (the empty `assets/` disappears). Commit nothing else inside `monitor.py`: its `if __name__ == "__main__"` block stays.
3. Add `__init__.py` and `__main__.py` as above.
4. `LICENSE`: the full GPL-3.0 text, verbatim from `https://www.gnu.org/licenses/gpl-3.0.txt`.
5. `pyproject.toml` as above; `uv sync` (regenerates `uv.lock`, installs the project into `.venv` as an editable package).
6. Tests: `import monitor` → `from claude_desktop_monitor import monitor`; `from monitor import …` → `from claude_desktop_monitor.monitor import …`. Delete `conftest.py`: the editable install makes the package importable.
7. `.gitignore`: add `dist/`.
8. Docs (keep every command in them working):
   - `CLAUDE.md`, `PLAN_EXEC_INSTRUCTIONS.md` (W-2): run commands, the single-file statement (now "one module, `src/claude_desktop_monitor/monitor.py`"), `conftest.py` note, uv version, v3 in the documents table and plan list.
   - `README.md`: replace each `uv run monitor.py` with `uv run claude-desktop-monitor`. The install/licence restructure is Plan 15.

## Verification (done when)

- [ ] V-13 (part): `uv run pytest` passes (whole suite); no test imports `monitor` from the repository root.
- [ ] `git log --follow --oneline src/claude_desktop_monitor/monitor.py` shows the v1/v2 history (the move is a rename, similarity ≥ 90%).
- [ ] `uv run claude-desktop-monitor --help`, `uv run python -m claude_desktop_monitor --help` and `mise run monitor -- --help` print the usage.
- [ ] Headless smoke run: `uv run claude-desktop-monitor --no-tui --log /tmp/cdm-p13.jsonl` for ~15 s, then SIGINT. Every line parses; `session_start`, `sample` and `session_end` present.
- [ ] `uv run python -c "from claude_desktop_monitor import monitor; print(monitor.NOTIFY_ICON.exists())"` prints `True`.
- [ ] V-15: `uv build` → `dist/` has one sdist and one wheel; `unzip -l` on the wheel lists `claude_desktop_monitor/monitor.py` and `claude_desktop_monitor/assets/claude-icon.png`; `tar tzf` on the sdist lists `LICENSE`. `dist/` is ignored by git.
- [ ] No `uv run monitor.py` left in `README.md`, `CLAUDE.md`, `PLAN_EXEC_INSTRUCTIONS.md` or `mise.toml` (plans and results files keep their historical commands).
- [ ] Committed, message prefixed `plan 13:`; status recorded in `plans/index.md`.

## Plan-level decisions (not specified in REQUIREMENTS-v3)

- `conftest.py` is deleted rather than edited: `uv sync` installs the package editable, so the `sys.path` hack has nothing left to do.
- `description` and `readme` are added to `[project]` (package metadata; `readme` also puts `README.md` in the sdist).
- No per-file license headers; the `LICENSE` file and the SPDX `license` field carry the license.
- `monitor.py` keeps its `if __name__ == "__main__"` block (harmless, and `python -m claude_desktop_monitor.monitor` keeps working).
- README gets only the mechanical command replacement here, so it is never wrong between plans; its restructure is Plan 15.

## Out of scope

The log location, `versions.monitor` and the 0.3.0 version (Plan 14). README restructure (Plan 15). Rust pin, formula and tap (Plan 16). Splitting `monitor.py` into modules (non-goal).
