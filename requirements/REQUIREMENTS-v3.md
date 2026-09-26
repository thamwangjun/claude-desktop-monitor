# Claude Desktop Resource Monitor — Requirements v3

Status: agreed requirements, implemented (plans 13–17). Grey areas 1–27 resolved with the user; open items in §12 resolved.

v3 is an **incremental** change on top of [`REQUIREMENTS-v1.md`](REQUIREMENTS-v1.md) and [`REQUIREMENTS-v2.md`](REQUIREMENTS-v2.md), which remain authoritative for everything not mentioned here. Where v3 amends an item, v3 wins. v1 (plans 01–08) and v2 (plans 09–12) are fully implemented.

## 1. Purpose

Today the monitor runs only from a clone of this repository, with mise and uv (`uv run monitor.py`). v3 makes it **installable with Homebrew** from a personal tap:

```
brew install thamwangjun/tap/claude-desktop-monitor
claude-desktop-monitor            # TUI, unchanged
```

The TUI, headless mode, flags, thresholds, notifications and log format are unchanged, apart from the default log location (L-1) and one additive `session_start` field (L-8). The tool remains observability only (v1 §1, §2).

## 2. Non-goals

- No macOS `.app` bundle, no code signing or notarization, no Mac App Store.
- No submission to homebrew-core, no PyPI publishing, no Homebrew cask.
- No self-update mechanism.
- No split of the monitor into several modules: the existing single module moves into a package unchanged in structure (§-banners kept).
- No change to the notification mechanism (v2 N-1: pync → `terminal-notifier`).
- No Intel bottles (Intel Macs build from source, §4.3).
- No automated formula version bumps.
- No migration of existing `./logs/` files.
- No new CLI flags (no `--version`).

## 3. Reference environment (observed facts the design relies on)

Checked 2026-09-27.

**Repositories.** Before v3 the repository had no git remote. The user created two public, empty GitHub repositories: `thamwangjun/claude-desktop-monitor` (source) and `thamwangjun/homebrew-tap` (tap). A read-only scan of the full git history found no secrets, logs or JSONL files; the only author identity is the GitHub noreply address. `plans/08-verification-results.md` contains a local path (`/Users/thamw/…`), accepted as harmless.

**Homebrew builds Python formulae from source** (`Homebrew/brew` main, `Library/Homebrew/formula.rb` `std_pip_args`, `language/python.rb`):
- pip always gets `--no-binary=:all:`: no wheels are used, for resources or for build backends.
- `virtualenv_install_with_resources` installs with build isolation, so each package's build backend is fetched and itself built from source.
- `--uploaded-prior-to=P<N>D`: PyPI releases newer than Homebrew's release cooldown are refused.

**`uv_build` is a Rust program.** PyPI `uv-build` 0.12.19 ships only platform-tagged wheels plus an sdist whose build backend is `maturin` (`Cargo.toml`, `crates/`, 108 `.rs` files). Building it from source, as Homebrew does, needs a Rust toolchain. Development (`uv sync`, `uv build`) uses the wheel and needs no Rust.

**Bottles** (prebuilt formula binaries) for a third-party tap are built by the GitHub Actions workflows that `brew tap-new` generates, triggered by a pull request on the tap. Publishing (`brew pr-pull --head-sha=<sha>`, or the generated "brew pr-pull" workflow) uploads the bottles to GitHub Releases on the tap and commits the formula's `bottle do` block. GitHub's `macos-26` runner (Apple Silicon) is generally available (2026-02-26). A user whose platform has no bottle builds from source, and Homebrew installs Rust for them as a build dependency.

**Platform floor.** Homebrew `terminal-notifier` 3.1.0 requires macOS 26 (v2 §3). psutil and watchdog contain C extensions, so bottles are specific to OS version and CPU architecture.

**Current code** (commit `52e1761`):
- The default log path is `./logs/monitor-<ts>.jsonl`, relative to the working directory; the log's parent directory is already created on demand (`monitor.py`, `LogWriter`).
- The notification icon is found relative to the module file (`Path(__file__).resolve().parent / "assets" / "claude-icon.png"`).
- `session_start.versions` records Python, psutil, rich, watchdog, macOS, the Claude app version and native-call availability, but not the monitor's own version.
- `conftest.py` puts the repository root on `sys.path` so that the 9 test files can `import monitor`.
- pync runs `which terminal-notifier` and otherwise falls back to its vendored Intel-only 2.0.0 copy (v2 §3).

## 4. Functional requirements

### 4.1 Package, build and license

| ID | Requirement |
|---|---|
| E-8 | The code becomes the package **`claude_desktop_monitor`** in a `src/` layout: `src/claude_desktop_monitor/{__init__.py, __main__.py, monitor.py, assets/claude-icon.png}`. `monitor.py` moves as one module; its content changes only where v3 requires it. Amends v1 E-4. |
| E-9 | Build backend: **`uv_build`**, `requires = ["uv_build>=0.12.19,<0.13"]` (matches the pinned uv, E-1). `pyproject.toml` drops `package = false` and declares the console script `claude-desktop-monitor = "claude_desktop_monitor.monitor:main"`. |
| E-10 | Run commands: **`uv run claude-desktop-monitor [flags]`** and `python -m claude_desktop_monitor [flags]`. The mise task runs the former. `uv run monitor.py` is no longer supported. Amends v1 E-5. |
| E-11 | Version **0.3.0** (matches requirements v3), declared once in `pyproject.toml`; the code reads it with `importlib.metadata`. Release tags are `vX.Y.Z` on the source repository. |
| E-12 | License **GPL-3.0-only**: a `LICENSE` file with the full GPL-3.0 text, and `license = "GPL-3.0-only"` in `pyproject.toml`. The sdist includes `LICENSE`. |
| E-13 | Runtime dependencies are unchanged (v2 E-3 amended). `uv_build` is a build-time requirement only. **mise pins Rust 1.98.1** (latest stable, 2026-09-01) for the local from-source check (V-19); `uv sync` and `uv build` still use the `uv_build` wheel and need no Rust. Homebrew builds (tap CI, Macs without a bottle) use Homebrew's own Rust via B-3, because Homebrew's build environment does not see mise-managed tools. |
| N-2 (amended) | The notification thumbnail moves to package data at `src/claude_desktop_monitor/assets/claude-icon.png`, still found relative to the module file, and ships in the wheel. If the file is missing, the notification is still sent without an image (unchanged). |

### 4.2 Logs

| ID | Requirement |
|---|---|
| L-1 (amended) | Default log path: **`~/Library/Logs/claude-desktop-monitor/monitor-YYYYmmdd-HHMMSS.jsonl`**. The directory is created if missing. `--log PATH` still overrides it (parent created on demand, unchanged). Existing `./logs/` files are not moved. This directory is the monitor's own, separate from `~/Library/Logs/Claude/`, which remains strictly read-only (v1 hard rules). Unit tests never write to the real directory. |
| L-8 | `session_start.versions` gains **`monitor`**: the installed package version (E-11). Additive; no existing key changes. |

### 4.3 Homebrew formula and tap

| ID | Requirement |
|---|---|
| B-1 | The formula is `Formula/claude-desktop-monitor.rb` in `thamwangjun/homebrew-tap`, drafted in a local clone next to this repository (`../homebrew-tap`). The tap's CI workflows are the templates generated by `brew tap-new`. The user pushes. |
| B-2 | `url` is the source repository's GitHub tag archive (`…/archive/refs/tags/v0.3.0.tar.gz`) with its `sha256`; `license "GPL-3.0-only"`; `homepage` is the source repository. |
| B-3 | Dependencies: `depends_on "python@3.14"`, `depends_on "terminal-notifier"`, `depends_on "rust" => :build`; macOS only. `:build` dependencies are skipped when a bottle is poured (Homebrew Formula Cookbook), so Homebrew's Rust is only installed for from-source builds (tap CI, Macs without a bottle). It is never installed on the reference machine (V-19 uses mise's Rust instead). |
| B-4 | Installed with `Language::Python::Virtualenv` into `libexec`. Every runtime dependency, including transitive ones, is a checksummed `resource` (sdist), generated with `brew update-python-resources`. |
| B-5 | The installed `claude-desktop-monitor` command is a wrapper that puts `terminal-notifier`'s Homebrew `opt_bin` first on `PATH`, so pync always uses Homebrew's arm64 3.1.0 regardless of the user's shell PATH (fixes the v2 O-5 fallback for installed copies). |
| B-6 | **Bottles**: built by the tap's GitHub Actions for **arm64 macOS 26 only** (`macos-26` runner), published to the tap's GitHub Releases. Other platforms build from source (Homebrew installs Rust as a build dependency). |
| B-7 | `test do`: checks `claude-desktop-monitor --help` output and that the package imports. It must not need Claude Desktop, send notifications, or write under `~/Library`. |
| B-8 | Release process (manual): bump the version (E-11) → the user tags `vX.Y.Z` and pushes → update the formula's `url`/`sha256` on a branch of the tap → the user opens a PR → tap CI builds the bottle → the user publishes with `brew pr-pull --head-sha=<sha>` (or the generated workflow). |
| B-9 | Tap commit messages follow Homebrew style: `claude-desktop-monitor 0.3.0 (new formula)`, `claude-desktop-monitor X.Y.Z` for bumps, `workflows: …` for CI changes. The body ends with the same `Authored by` / `Co-Authored-By` lines as this repository. |

## 5. Documentation

| ID | Requirement |
|---|---|
| W-1 | README: installation via Homebrew first; developer setup (mise + uv, `uv run claude-desktop-monitor`) second, noting that `mise install` also installs Rust 1.98.1, which only the from-source check (V-19) needs; the new default log location; the license. All `uv run monitor.py` examples change to the new command. v1 §11 (only the 9 approved issue citations) still holds. |
| W-2 | `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md` updated for the new layout, commands, test import path and log location. |

## 6. CLI summary

No new flags. Changed default:

| Flag | Default |
|---|---|
| `--log PATH` | `~/Library/Logs/claude-desktop-monitor/monitor-<ts>.jsonl` (was `./logs/monitor-<ts>.jsonl`) |

## 7. Tooling and packaging changes

| ID | Change |
|---|---|
| E-1 | Amended: mise pins Python 3.14.7, **uv 0.12.19** (was 0.12.18; latest as of 2026-09-27) and **Rust 1.98.1** (E-13). `uv.lock` is regenerated. |
| E-2 | Unchanged: `uv sync` from `uv.lock`; the project is now installed into `.venv` as a package. |
| E-4 | Replaced by E-8 (package, build step via `uv_build`). |
| E-5 | Replaced by E-10. |
| v1 §2 "Not a shipped product… no build step" | Replaced: shipped through a Homebrew tap; `uv_build` build step. |
| v2 §2 "No build step (v1 E-4 still holds)" | Replaced by E-8/E-9. |

## 8. Deliverables

Source repository:
1. `src/claude_desktop_monitor/` (E-8), with `monitor.py` moved from the root, `L-1`/`L-8` changes, and the icon (N-2).
2. `pyproject.toml` (E-9, E-11, E-12), `uv.lock`, `mise.toml` (Rust pin, E-13; task, E-10).
3. `LICENSE` (E-12).
4. `tests/` and `conftest.py` updated for the package import.
5. `.gitignore`: + `dist/` (output of `uv build`).
6. `README.md` (W-1), `CLAUDE.md` and `PLAN_EXEC_INSTRUCTIONS.md` (W-2).
7. This `requirements/REQUIREMENTS-v3.md`; plans 13 onward and `plans/index.md`.

Tap repository (`../homebrew-tap`):
8. `Formula/claude-desktop-monitor.rb` (B-1 to B-7), commits per B-9, and the `brew tap-new` CI workflows (B-6).

## 9. Verification

Live checks are done **one at a time, each confirmed with the user**, as in v2. Anything that pushes, tags, opens PRs or publishes is done by the user.

| ID | Requirement |
|---|---|
| V-13 | Full test suite passes after the move, importing from the package; no test writes to `~/Library/Logs/claude-desktop-monitor/`. |
| V-14 | Dev runs: `uv run claude-desktop-monitor` shows the TUI; `python -m claude_desktop_monitor --no-tui` runs headless; with no `--log`, the log lands in `~/Library/Logs/claude-desktop-monitor/`; `session_start.versions.monitor == "0.3.0"`. |
| V-15 | `uv build` produces an sdist and a wheel; the wheel contains `claude_desktop_monitor/monitor.py` and `assets/claude-icon.png`; the sdist contains `LICENSE`. |
| V-16 | Tap CI on the formula PR builds an arm64 macOS 26 bottle **from source** (Rust as a build-only dependency, B-3) and passes; this also covers the from-source fallback for Macs without a bottle (O-1). Publishing commits the `bottle do` block and uploads the bottle to the tap's Releases. |
| V-17 | On the reference machine: `brew install thamwangjun/tap/claude-desktop-monitor` **pours the bottle** (Homebrew's `rust` is not installed; `brew list rust` fails before and after); `brew test` and `brew audit --strict` for the formula pass. |
| V-18 | The installed command: the TUI runs; the wrapper puts Homebrew `terminal-notifier` first on PATH (B-5); `--notify-test` (real banners, only with the user's agreement) shows both notifications with the thumbnail. |
| V-19 | Once, before the first formula PR (plan 16), with **mise's Rust** and no Homebrew Rust: every formula resource and the build backend (`uv_build`, via `maturin`) builds from sdist the way Homebrew does (`pip install --no-binary :all:` into a scratch virtualenv under `/tmp`), then the project's sdist from `uv build` installs into it, and `claude-desktop-monitor --help` and the package import succeed. The real `brew install --build-from-source` path is exercised only in tap CI (V-16). |

## 10. Decision log (grey areas)

| # | Grey area | Decision |
|---|---|---|
| 1 | Source hosting | Public GitHub, `thamwangjun/claude-desktop-monitor` |
| 2 | License | GPL-3.0-only |
| 3 | Formula tarball | GitHub tag archive |
| 4 | Module layout | Convert to package `claude_desktop_monitor` |
| 5 | Command name | `claude-desktop-monitor` |
| 6 | Build backend | `uv_build` (briefly switched to hatchling when source builds would need Rust, then restored once bottles were chosen) |
| 7 | Notifier on PATH | Formula wrapper puts Homebrew `terminal-notifier` first |
| 8 | Default log location | `~/Library/Logs/claude-desktop-monitor/` |
| 9 | Bottles | Yes, via tap GitHub Actions (initially "no bottles", revised) |
| 10 | Python | `python@3.14` |
| 11 | Tap repository | `thamwangjun/homebrew-tap` |
| 12 | Version bumps | Manual; through a tap PR because of bottles (B-8) |
| 13 | `brew test` | `--help` plus import check |
| 14 | Process | `REQUIREMENTS-v3.md` plus plans 13 onward |
| 15 | Package structure | `src/` layout, `monitor.py` moved as one module |
| 16 | Dev command | `uv run claude-desktop-monitor`; no root shim |
| 17 | First version | 0.3.0, to match requirements v3 |
| 18 | Formula location | Local clone of the tap repository |
| 19 | Log directory handling | Auto-create; `--log` overrides; no migration |
| 20 | v3 verification | Suite plus Homebrew install checks, one at a time. The local `brew install --build-from-source` first proposed here was superseded by bottles (#9), O-1 and #24: the reference machine installs the bottle |
| 21 | Bottle platforms | arm64 macOS 26 only |
| 22 | Monitor version in logs | Add `session_start.versions.monitor` (no `--version` flag) |
| 23 | Rust tooling | mise pins Rust 1.98.1 (latest stable) for local Rust needs; Homebrew builds use Homebrew's Rust (B-3) |
| 24 | Verifying the from-source fallback (O-1) | Tap CI (V-16), plus one local sdist build with mise's Rust before the first PR (V-19); Homebrew's Rust never on the reference machine |
| 25 | Plan split (O-2) | Five plans, 13–17, split by kind of change and by user gates (tag, PR, publish) |
| 26 | uv version | Bump mise uv to 0.12.19; `uv_build>=0.12.19,<0.13` |
| 27 | Tap commit messages | Homebrew style plus this repository's attribution lines (B-9) |

## 11. Relation to v1 and v2 requirements

| Earlier item | Change |
|---|---|
| v1 §2 non-goal "not a shipped product, no build step" | Replaced (§7) |
| v1 E-4 single script | Replaced by E-8 |
| v1 E-5 run command | Replaced by E-10 |
| v1 L-1 default log path | Amended (§4.2) |
| v1 L-2 `session_start` | + `versions.monitor` (L-8) |
| v1 §6 CLI `--log` default | Amended (§6) |
| v1 §8 deliverables | Extended (§8) |
| v1 E-1 mise tools | uv 0.12.18 → 0.12.19; + Rust 1.98.1 (E-13) |
| v2 N-1 notifier selection | Unchanged in code; installed copies always get Homebrew `terminal-notifier` via the wrapper (B-5) |
| v2 N-2 icon location | Amended (§4.1) |
| v2 §2 "no build step" | Replaced (§7) |
| v2 V-8 command | `claude-desktop-monitor --notify-test` replaces `uv run monitor.py --notify-test` |

## 12. Open items

| # | Item | Status |
|---|---|---|
| O-1 | Local from-source check | Resolved 2026-09-27: one local check in plan 16 before the formula PR (V-19), using mise's Rust 1.98.1, not Homebrew's; Homebrew's own from-source build is covered by tap CI (V-16). |
| O-2 | Plan split | Resolved 2026-09-27: 13 package conversion (E-1 uv bump, E-8, E-9, E-10, E-12, E-13 except the Rust pin, N-2, W-2; V-13, V-15); 14 runtime changes for 0.3.0 (L-1, L-8, E-11; V-14); 15 README and release (W-1; the user tags `v0.3.0`); 16 formula and tap CI (B-1 to B-9, the E-13 Rust pin; V-19, V-16 build); 17 v3 verification (V-16 publish, V-17, V-18) |
