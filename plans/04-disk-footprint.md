# Plan 04: Disk footprint

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Execution order and rationale: [`index.md`](index.md).

## Context

Claude Desktop keeps its data in `~/Library/Application Support/Claude/`, the "support directory". Cowork's VM lives in `vm_bundles/claudevm.bundle/`. On a fresh install on the reference machine that bundle held:

| File | Apparent size | Allocated on disk | Notes |
|---|---|---|---|
| `rootfs.img` | 10 GiB | ~10 GiB | VM root disk, fully allocated (not sparse) |
| `rootfs.img.zst` | ~1.17 GiB | same | Compressed download, kept after extraction |
| `sessiondata.img` | grew 4 MB → 29 MB in seconds | about half the apparent size | Sparse, per-session VM data, actively growing |
| `initrd*`, `vmlinuz*` (+ `.zst`) | ~130 MB total | | Kernel and initial ramdisk |
| small metadata files | tiny | | |

Public issue reports describe this bundle growing without bound (the guest never trims, so the host image never shrinks), `sessiondata.img` accumulating per-session data, and stalled downloads leaving `*.partial` files. Other directories (`Cache/`, `Code Cache/`, `vm_bundles/warm/`, …) also grow.

- **Apparent size** (`st_size`) is the logical file length.
- **Allocated size** (`st_blocks × 512`) is the real disk usage. For sparse files these differ, and allocated size is what fills the disk.

## Goal

A disk collector (§4) that reports apparent and allocated sizes for the VM bundle files (every poll) and for tracked directories (via filesystem events). It handles missing paths and supports `--support-dir`.

## Prerequisites

Plan 01 complete. Independent of Plans 02, 03 and 05.

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| D-1 | Report apparent (`st_size`) and allocated (`st_blocks × 512`) size for every tracked path. Alerts and growth use allocated. |
| D-2 | Bundle rows `rootfs.img`, `sessiondata.img`, `other` (all remaining bundle files), `bundle total`, measured with `stat()` every poll. |
| D-3 | Detect `rootfs.img.zst` disappearing/reappearing. (This plan reports presence; Plan 06 raises the flag.) |
| D-4 | Tracked directories: `vm_bundles/`, `vm_bundles/warm/`, `Cache/`, `Code Cache/`, `claude-code-vm/`, `local-agent-mode-sessions/`, `IndexedDB/`, `GPUCache/`, and the whole support directory. |
| D-5 | Directory sizes maintained via FSEvents (`watchdog`): events mark subtrees dirty, and only dirty subtrees are re-walked. A full rescan every `--full-rescan` s (default 300). |
| D-6 | Missing paths show as absent, never raise, and are picked up automatically when they appear. |
| D-7 | Per path: current, Δ since last sample, Δ since start, peak. (This plan supplies current values; Plan 06 derives Δ and peak.) |
| D-8 | `*.partial` files in `vm_bundles/` older than 5 min are flagged. (This plan reports them with their age; Plan 06 flags.) |
| D-9 | All paths resolved relative to `--support-dir`. |
| T-2 | Directory sizes update from FSEvents between polls; bundle `stat()`s run each poll. |
| T-3 | No full directory walks on the fast poll path. |
| L-5 | Every bundle file is logged individually. |

## Design

### Path model

`support = Path(args.support_dir).expanduser()`; it need not exist. Tracked directories and their log/display names:

| Name | Path |
|---|---|
| `support_total` | `support/` |
| `vm_bundles` | `support/vm_bundles` |
| `vm_warm` | `support/vm_bundles/warm` |
| `cache` | `support/Cache` |
| `code_cache` | `support/Code Cache` |
| `claude_code_vm` | `support/claude-code-vm` |
| `agent_sessions` | `support/local-agent-mode-sessions` |
| `indexeddb` | `support/IndexedDB` |
| `gpucache` | `support/GPUCache` |

### Size walking

`walk_size(path) -> (apparent, allocated, file_count)`:
- Recursive `os.scandir` using `lstat` semantics; symlinks are not followed.
- Apparent = sum of `st_size` of regular files. Allocated = sum of `st_blocks × 512` of all entries.
- Hard links are counted once, using an `(st_dev, st_ino)` set.
- Entries that vanish mid-walk (`FileNotFoundError`) and unreadable entries (`PermissionError`) are skipped and counted in `skipped`.
- A missing root returns `absent`.

APFS clones share blocks but are counted per file, as `du` does. This is a known limitation, documented in the README.

### Incremental directory sizes (D-5, T-2, T-3)

- **Scan units.** Each immediate child of `support/` is one unit; `vm_bundles/` is split one level further, so each of its children is a unit. Loose files directly in `support/` form one `support:files` unit, and loose files directly in `vm_bundles/` form one unit too.
  - Each unit's size is cached.
  - Tracked-directory sizes and `support_total` are **sums of unit sizes**, so a change in `Cache/` only re-walks `Cache/`.
- **FSEvents.** A `watchdog` `Observer` is scheduled recursively on `support/`. The event handler, running on watchdog's thread, maps `src_path` (and `dest_path` for moves) to its unit and adds the unit to a lock-protected dirty set. No walking happens on the event thread.
- **Poll thread.** Each poll takes the dirty set and re-walks only those units (T-3).
  - Units whose events arrive continuously (e.g. the bundle while the VM writes) are re-walked at most once per poll, which is cheap for the ~25-file bundle.
  - Walk time per unit is recorded as `scan_ms`.
- **Full rescan.** Every `--full-rescan` seconds all units are marked dirty, as a safety net against missed or coalesced events.
- **Support dir absent or wiped (D-6).**
  - If `support/` doesn't exist, the observer isn't scheduled, and each poll checks `support.is_dir()`. When it appears, schedule the observer and mark everything dirty.
  - If it disappears while being watched (the reference machine saw the app data wiped while the app ran), catch the observer error, unschedule, and go back to polling for existence.
  - New units (e.g. `vm_bundles/` created by a first Cowork run) are discovered through events on their parent and by the full rescan.

### VM bundle, every poll (D-2, D-3, D-8, L-5)

- `bundle = support/vm_bundles/claudevm.bundle`. Each poll, walk it recursively; it is small. For each file, record apparent and allocated size.
- Rows: `rootfs.img`, `sessiondata.img`, `other` (sum of everything else), `total`.
- `zst_present`: `rootfs.img.zst` exists.
- `partials`: every `*.partial` under `support/vm_bundles/` with `age_s = now − st_mtime`. Taken from the bundle walk, plus a `glob` of `vm_bundles/**/*.partial` done during that directory's unit walk. The last known list is kept between walks.
- If the bundle is absent: `bundle.present = false`, rows absent.

### Sample fragment

```json
"paths": {"cache": {"present": true, "apparent": 123, "allocated": 456, "files": 789, "scan_ms": 3.1, "last_scan_elapsed": 11.9}, ...},
"bundle": {"present": true,
           "rows": {"rootfs.img": {"apparent": 10737418240, "allocated": 10732580864},
                    "sessiondata.img": {...}, "other": {...}, "total": {...}},
           "files": {"rootfs.img": {...}, "rootfs.img.zst": {...}, "...": {...}},
           "zst_present": true,
           "partials": [{"path": "vm_bundles/claudevm.bundle/vmlinuz.zst.f380613cca66.partial", "age_s": 412.0, "apparent": 0, "allocated": 0}]}
```

## Tasks

1. §4: `walk_size`, unit model, `DiskCollector` with watchdog observer, dirty set, full rescan, absent/appear handling.
2. Bundle per-poll walk, rows, `zst_present`, `partials`.
3. Wire `--support-dir` and `--full-rescan`.
4. `tests/test_disk.py` (uses `tmp_path` as the support dir):
   - A sparse file made with `os.truncate` to 1 GiB plus a 1 MiB write: apparent ≈ 1 GiB, allocated ≈ 1 MiB.
   - Hard link counted once.
   - Absent support dir → all absent, no exception. Directories created afterwards appear within one poll plus FSEvents latency (poll with a short timeout loop).
   - Writing inside `Cache/` dirties only the `Cache` unit.
   - Bundle rows and `other` sum correctly.
   - `.partial` age reported.
   - `zst_present` toggles.

## Verification (done when)

- [ ] On the real support dir, `support_total` and `vm_bundles` allocated sizes are within 1% of `du -sk`, and apparent sizes within 1% of `du -A -sk` (macOS `du -A` = apparent size).
- [ ] `rootfs.img` shows ~10 GiB apparent and allocated. `sessiondata.img` allocated is below apparent.
- [ ] V-3: `--support-dir /tmp/cdm-test` (absent) runs cleanly with every path absent. Then `mkdir -p "/tmp/cdm-test/vm_bundles/claudevm.bundle"` and write files there while running: they appear in the next samples.
- [ ] During a Cowork task, only affected units show fresh `last_scan_elapsed`. `Cache` isn't re-walked unless it changed.
- [ ] Per-poll time spent in the disk collector stays under ~50 ms on the reference machine, outside full rescans.
- [ ] `uv run pytest` passes. Committed.

## Plan-level decisions (not specified in REQUIREMENTS.md)

- Scan-unit granularity: immediate children of `support/`, and of `vm_bundles/`.
- Hard links are de-duplicated; APFS clones are not.
- `scan_ms`, `files` and `last_scan_elapsed` are added to the log for performance review.
- The `.partial` search covers all of `vm_bundles/`, not just the bundle.

## Out of scope

Deltas, peaks, growth rates and flags (Plan 06); display (Plan 07).
