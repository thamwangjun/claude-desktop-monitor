# Plan 02: macOS native layer (ctypes)

Part of the Claude Desktop Resource Monitor build. Full requirements: [`../requirements/REQUIREMENTS-v1.md`](../requirements/REQUIREMENTS-v1.md). Execution order and rationale: [`index.md`](index.md).

## Context

The monitor needs three facts about each process that psutil cannot provide on macOS:

1. **Disk I/O bytes**: `psutil.Process.io_counters()` is not implemented on macOS.
2. **Physical memory footprint**: the "Memory" figure Activity Monitor shows. It is more accurate than RSS on macOS.
3. **Ownership**: which app a process works for. Claude's VM runs as the system process `com.apple.Virtualization.VirtualMachine` with parent PID 1, and Docker Desktop runs a process with the same name. macOS groups processes into **resource coalitions** and charges resources per coalition. That is how its own diagnostic reports attribute Docker's VM to `com.docker.docker` and Claude's to `com.anthropic.claudefordesktop`.

All three are available through libSystem calls via Python's standard `ctypes`, with no root.

## Risk status

This plan was the highest-risk item. A probe on 2026-09-25 (reference machine: Apple M5, macOS 26.7, Claude Desktop 2.9939.2) retired the risk:

| Call | Result |
|---|---|
| `proc_pidinfo(pid, 20, 0, &buf, 40)` | Returned 40. `coalition_id[0]` was 11603 for Claude main, helpers and Claude's VM, matching the `.diag` report's `"com.anthropic.claudefordesktop"(11603)`. Docker's VM returned 11507 (`com.docker.docker`) |
| `responsibility_get_pid_responsible_for_pid(pid)` | Claude's VM → Claude main PID. Docker's VM → a Docker PID |
| `proc_pid_rusage(pid, 2, &buf)` | Returned 0 for Claude's VM (footprint ~1 GB, write counter readable) and for Claude main (~11.9 GB written) |

The implementation must still handle failure. These are private or semi-private interfaces and may change in future macOS releases.

## Goal

A small, well-tested §2 section of `monitor.py` exposing three functions that never raise and return `None` on any failure.

## Prerequisites

Plan 01 complete (project scaffold, `monitor.py` sections, pytest).

## Requirements covered

| ID | Requirement (summary) |
|---|---|
| I-1 | Per-process cumulative bytes read/written via `proc_pid_rusage` (`rusage_info_v2` `ri_diskio_bytesread`/`ri_diskio_byteswritten`), no sudo. |
| I-4 | Unreadable processes yield "n/a", never an error. (This plan supplies `None`; callers display n/a.) |
| M-1 | Physical footprint (`ri_phys_footprint`) as the primary memory value, RSS secondary. (This plan supplies both.) |
| P-1 | Tracked set = Claude's resource coalition. (This plan supplies the coalition lookup.) |
| P-3/P-4 | VM attribution by coalition, then responsible PID, then name. (This plan supplies the lookups.) |
| V-2 | `proc_pid_rusage` readable for the VM process without root. (Re-confirmed by this plan's tests.) |

## Design

### Library loading

`libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)`, loaded once at import. Each symbol is looked up with `getattr` inside `try/except AttributeError`. A missing symbol makes the corresponding function always return `None`. `NATIVE_AVAILABLE: dict[str, bool]` records which symbols loaded, for `session_start`.

### `rusage(pid) -> RUsage | None`

- Flavor `RUSAGE_INFO_V2 = 2` (public header `sys/resource.h`).
- Struct `rusage_info_v2`: `uint8_t ri_uuid[16]` followed by 18 `uint64_t` fields, in this order: `ri_user_time`, `ri_system_time`, `ri_pkg_idle_wkups`, `ri_interrupt_wkups`, `ri_pageins`, `ri_wired_size`, `ri_resident_size`, `ri_phys_footprint`, `ri_proc_start_abstime`, `ri_proc_exit_abstime`, `ri_child_user_time`, `ri_child_system_time`, `ri_child_pkg_idle_wkups`, `ri_child_interrupt_wkups`, `ri_child_pageins`, `ri_child_elapsed_abstime`, `ri_diskio_bytesread`, `ri_diskio_byteswritten`.
- Signature: `int proc_pid_rusage(int pid, int flavor, rusage_info_t *buffer)`; 0 = success.
- Returns a frozen dataclass `RUsage(footprint, resident, disk_read, disk_written)`. All values are bytes. CPU time is **not** taken from here: `ri_user_time` is in Mach time units, and CPU % comes from psutil (Plan 03).
- Non-zero return (e.g. `ESRCH` for an exited process, `EPERM` for another user's process) → `None`.

### `coalition_id(pid) -> int | None`

- `proc_pidinfo(int pid, int flavor, uint64_t arg, void *buffer, int size)` with flavor `PROC_PIDCOALITIONINFO = 20`. This constant is **private**: it comes from xnu's `sys/proc_info_private.h` and is not in the public SDK.
- Struct `proc_pidcoalitioninfo`: `uint64_t coalition_id[2]; uint64_t reserved1, reserved2, reserved3;` (40 bytes). Index `0` = `COALITION_TYPE_RESOURCE`, index `1` = jetsam coalition.
- Success requires the return value to equal `sizeof(struct) == 40`; otherwise `None`. A coalition ID of `0` → `None`.

### `responsible_pid(pid) -> int | None`

- `int responsibility_get_pid_responsible_for_pid(int pid)`, exported by libSystem (private API).
- Returns `None` if the symbol is missing or the result is ≤ 0.

### Behaviour guarantees

- None of the three functions raises. All exceptions (`OSError`, `ctypes.ArgumentError`) are caught and turned into `None`.
- They are safe to call hundreds of times per poll. Each is a single syscall; no caching at this layer (caching lives in Plan 03).

## Tasks

1. §2: library loading, `NATIVE_AVAILABLE`, the three structs and functions, argtypes/restype set explicitly.
2. Add `NATIVE_AVAILABLE` to the `session_start` record (`versions.native`).
3. `tests/test_native.py`:
   - For `os.getpid()`: `rusage` returns positive footprint and resident values, `coalition_id` returns an int > 0, `responsible_pid` returns an int > 0.
   - For a non-existent PID (e.g. spawn and reap a child, reuse its PID): all three return `None`.
   - `disk_written` increases after the test writes and `fsync`s a 1 MiB temp file.
   - **Live checks, skipped when Claude Desktop is not running**: Claude main PID found by exe path `/Applications/Claude.app/Contents/MacOS/Claude`; every running `com.apple.Virtualization.VirtualMachine` process is classified by `coalition_id == coalition_id(main)`. At least one matches when Cowork's VM is up, and `rusage` is non-`None` for it (V-2). If a VM with a different coalition exists (e.g. Docker), it is not matched (early evidence for V-6).

## Verification (done when)

- [ ] `uv run pytest tests/test_native.py -v` passes with Claude Desktop running, including the live checks.
- [ ] With Docker Desktop's VM also running, the test output shows it is excluded.
- [ ] `session_start` in a fresh log lists all three native symbols as available.
- [ ] Committed.

## Plan-level decisions (not specified in requirements/REQUIREMENTS-v1.md)

- `rusage_info_v2` is used rather than newer versions: it is the oldest struct with the needed fields, which keeps the layout stable.
- The jetsam coalition ID (index 1) is ignored.

## Out of scope

Process enumeration, role classification, rates and aggregation (Plan 03).
