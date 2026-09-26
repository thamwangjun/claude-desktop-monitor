#!/usr/bin/env python3
"""Claude Desktop Resource Monitor.

Watches Claude Desktop's resource usage over time, with emphasis on the
Cowork feature's local Linux VM. Observability only: never modifies,
deletes or restarts anything it observes.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import platform
import re
import select
import signal
import stat as stat_module
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ── §1 Constants, unit helpers, CLI ──

DEFAULT_SUPPORT_DIR = "~/Library/Application Support/Claude"

_DECIMAL_SUFFIXES = {"": 1, "b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}
_BINARY_SUFFIXES = {"kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


def parse_size(text: str) -> float:
    """Parse a size string into bytes (or bytes/s if a trailing /s is present).

    Accepts a plain number (bytes), a decimal suffix (KB/MB/GB/TB, 10^3n) or
    a binary suffix (KiB/MiB/GiB/TiB, 2^10n). Suffixes are case-insensitive.
    A trailing "/s" is accepted and ignored (rate flags reuse this parser).
    """
    raw = text.strip()
    if raw.lower().endswith("/s"):
        raw = raw[: -len("/s")]
    match = _SIZE_RE.match(raw)
    if not match:
        raise ValueError(f"not a valid size: {text!r}")
    number_str, suffix = match.groups()
    suffix = suffix.lower()
    if suffix in _DECIMAL_SUFFIXES:
        multiplier = _DECIMAL_SUFFIXES[suffix]
    elif suffix in _BINARY_SUFFIXES:
        multiplier = _BINARY_SUFFIXES[suffix]
    else:
        raise ValueError(f"unknown size suffix {suffix!r} in {text!r}")
    return float(number_str) * multiplier


def format_bytes(n: float | None) -> str:
    """Format a byte count using binary units (KiB/MiB/GiB)."""
    if n is None:
        return "n/a"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if n >= size:
            return f"{sign}{n / size:.2f} {unit}"
    return f"{sign}{n:.0f} B"


def _size_arg(text: str) -> float:
    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _interval_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not (2 <= value <= 5):
        raise argparse.ArgumentTypeError("--interval must be between 2 and 5 seconds")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Monitor Claude Desktop's resource usage, focused on the Cowork VM.",
    )
    parser.add_argument("--interval", type=_interval_arg, default=3.0,
                         help="poll interval in seconds, 2-5 (default 3)")
    parser.add_argument("--full-rescan", type=float, default=300.0,
                         help="periodic full directory rescan interval in seconds (default 300)")
    parser.add_argument("--cpu-threshold", type=float, default=30.0,
                         help="CPU alert threshold, %% of one core (default 30)")
    parser.add_argument("--cpu-window", type=float, default=60.0,
                         help="CPU rolling-average window in seconds (default 60)")
    parser.add_argument("--write-threshold", type=_size_arg, default=parse_size("1MB"),
                         help="disk write alert threshold, bytes/s (default 1MB)")
    parser.add_argument("--budget-warn", type=float, default=80.0,
                         help="warn at this %% of each macOS 24h write budget (default 80)")
    parser.add_argument("--mem-growth", type=float, default=50.0,
                         help="memory footprint growth-over-baseline alert, %% (default 50)")
    parser.add_argument("--bundle-growth", type=_size_arg, default=parse_size("1GB"),
                         help="bundle growth-since-start alert, bytes (default 1GB)")
    parser.add_argument("--bundle-rate", type=_size_arg, default=parse_size("100MB"),
                         help="bundle growth per 10 min alert, bytes (default 100MB)")
    parser.add_argument("--log", type=str, default=None,
                         help="log file path (default ./logs/monitor-<timestamp>.jsonl)")
    parser.add_argument("--no-tui", action="store_true",
                         help="headless logging mode, no live table")
    parser.add_argument("--include-cli", action="store_true",
                         help="also track the Claude Code CLI as a separate group")
    parser.add_argument("--trace-io", action="store_true",
                         help="elevated sudo fs_usage file-level write tracing")
    parser.add_argument("--support-dir", type=str, default=DEFAULT_SUPPORT_DIR,
                         help="override the Claude support directory (testing)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cpu_window < args.interval:
        parser.error("--cpu-window must be >= --interval")
    if args.full_rescan <= 0:
        parser.error("--full-rescan must be > 0")
    if not (0 < args.budget_warn <= 100):
        parser.error("--budget-warn must be in (0, 100]")
    if args.mem_growth <= 0:
        parser.error("--mem-growth must be > 0")
    args.support_dir = os.path.expanduser(args.support_dir)
    if args.log is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.log = str(Path("logs") / f"monitor-{timestamp}.jsonl")
    return args


# ── §2 Native macOS calls (ctypes) ── (Plan 02)

import ctypes

RUSAGE_INFO_V2 = 2
PROC_PIDCOALITIONINFO = 20

NATIVE_AVAILABLE: dict[str, bool] = {
    "proc_pid_rusage": False,
    "proc_pidinfo": False,
    "responsibility_get_pid_responsible_for_pid": False,
}

try:
    _libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
except OSError:
    _libc = None


class _RUsageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


class _ProcPidCoalitionInfo(ctypes.Structure):
    _fields_ = [
        ("coalition_id", ctypes.c_uint64 * 2),
        ("reserved1", ctypes.c_uint64),
        ("reserved2", ctypes.c_uint64),
        ("reserved3", ctypes.c_uint64),
    ]


_proc_pid_rusage = None
if _libc is not None:
    try:
        _proc_pid_rusage = _libc.proc_pid_rusage
        _proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RUsageInfoV2)]
        _proc_pid_rusage.restype = ctypes.c_int
        NATIVE_AVAILABLE["proc_pid_rusage"] = True
    except AttributeError:
        _proc_pid_rusage = None

_proc_pidinfo = None
if _libc is not None:
    try:
        _proc_pidinfo = _libc.proc_pidinfo
        _proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.POINTER(_ProcPidCoalitionInfo), ctypes.c_int,
        ]
        _proc_pidinfo.restype = ctypes.c_int
        NATIVE_AVAILABLE["proc_pidinfo"] = True
    except AttributeError:
        _proc_pidinfo = None

_responsibility_get_pid_responsible_for_pid = None
if _libc is not None:
    try:
        _responsibility_get_pid_responsible_for_pid = _libc.responsibility_get_pid_responsible_for_pid
        _responsibility_get_pid_responsible_for_pid.argtypes = [ctypes.c_int]
        _responsibility_get_pid_responsible_for_pid.restype = ctypes.c_int
        NATIVE_AVAILABLE["responsibility_get_pid_responsible_for_pid"] = True
    except AttributeError:
        _responsibility_get_pid_responsible_for_pid = None


@dataclass(frozen=True)
class RUsage:
    footprint: int
    resident: int
    disk_read: int
    disk_written: int


def rusage(pid: int) -> RUsage | None:
    """Physical footprint, resident size and cumulative disk I/O bytes for pid.

    Returns None for a vanished, inaccessible or unreadable process, or if
    the native call is unavailable. Never raises.
    """
    if _proc_pid_rusage is None:
        return None
    buf = _RUsageInfoV2()
    try:
        result = _proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(buf))
    except (OSError, ctypes.ArgumentError):
        return None
    if result != 0:
        return None
    return RUsage(
        footprint=buf.ri_phys_footprint,
        resident=buf.ri_resident_size,
        disk_read=buf.ri_diskio_bytesread,
        disk_written=buf.ri_diskio_byteswritten,
    )


def coalition_id(pid: int) -> int | None:
    """Resource coalition ID for pid, or None on failure or an empty coalition.

    PROC_PIDCOALITIONINFO is a private xnu flavor; success is signalled by
    the return value equalling sizeof(struct proc_pidcoalitioninfo) (40).
    """
    if _proc_pidinfo is None:
        return None
    buf = _ProcPidCoalitionInfo()
    try:
        result = _proc_pidinfo(pid, PROC_PIDCOALITIONINFO, 0, ctypes.byref(buf), ctypes.sizeof(buf))
    except (OSError, ctypes.ArgumentError):
        return None
    if result != ctypes.sizeof(buf):
        return None
    value = buf.coalition_id[0]
    return value if value != 0 else None


def responsible_pid(pid: int) -> int | None:
    """PID of the process responsible for launching pid, or None on failure."""
    if _responsibility_get_pid_responsible_for_pid is None:
        return None
    try:
        result = _responsibility_get_pid_responsible_for_pid(pid)
    except (OSError, ctypes.ArgumentError):
        return None
    return result if result > 0 else None


# ── §3 Process collector ── (Plan 03)

_CLAUDE_APP_CANDIDATES = ("/Applications/Claude.app", os.path.expanduser("~/Applications/Claude.app"))
_VM_PROCESS_NAME = "com.apple.Virtualization.VirtualMachine"

_UTILITY_SUBTYPE_RE = re.compile(r"--utility-sub-type=(\S+)")
_WRITE_CALL_RE = re.compile(r"\b(write|pwrite|writev|pwritev|WrData\w*)\b")
_FS_USAGE_BYTES_RE = re.compile(r"B=0x([0-9a-fA-F]+)")

_PROCESS_ATTRS = ["pid", "exe", "cmdline", "create_time", "name"]


def _info(proc: psutil.Process, field: str, default=None):
    """Read a cached psutil.Process.info field, tolerating a dead/denied process."""
    try:
        return proc.info.get(field, default)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return default


def _iter_processes() -> list[psutil.Process]:
    return list(psutil.process_iter(_PROCESS_ATTRS))


def _safe_exe(proc: psutil.Process) -> str | None:
    """Best-effort executable path: cached info, then live exe/cmdline/name."""
    exe = _info(proc, "exe")
    if exe:
        return exe
    try:
        return proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        pass
    cmdline = _safe_cmdline(proc)
    if cmdline:
        return cmdline[0]
    try:
        return proc.name()
    except (psutil.NoSuchProcess, OSError):
        return None


def _safe_cmdline(proc: psutil.Process) -> list[str]:
    cmdline = _info(proc, "cmdline")
    if cmdline:
        return cmdline
    try:
        return proc.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return []


def _is_vm_process(proc: psutil.Process) -> bool:
    if _info(proc, "name") == _VM_PROCESS_NAME:
        return True
    exe = _safe_exe(proc)
    return bool(exe) and os.path.basename(exe) == _VM_PROCESS_NAME


def _find_main(all_procs: list[psutil.Process]) -> tuple[psutil.Process | None, str | None]:
    """The oldest process whose exe is a known Claude.app main binary, plus its app root."""
    candidates = {os.path.join(root, "Contents/MacOS/Claude"): root for root in _CLAUDE_APP_CANDIDATES}
    matches = [p for p in all_procs if _info(p, "exe") in candidates]
    if not matches:
        return None, None
    matches.sort(key=lambda p: _info(p, "create_time") or 0)
    main_proc = matches[0]
    return main_proc, candidates[_info(main_proc, "exe")]


def classify_role(exe: str | None, cmdline: list[str], main_exe: str | None, app_prefix: str | None) -> str:
    """Role classification per requirements/REQUIREMENTS-v1.md P-5, evaluated in a fixed order."""
    if exe and exe == main_exe:
        return "main"
    if exe and os.path.basename(exe) == _VM_PROCESS_NAME:
        return "vm"
    if exe and os.path.basename(exe) == "chrome_crashpad_handler":
        return "crashpad"
    in_bundle = bool(app_prefix and exe and exe.startswith(app_prefix))
    cmdline_str = " ".join(cmdline)
    if in_bundle and "--type=renderer" in cmdline_str:
        return "renderer"
    if in_bundle and "--type=gpu-process" in cmdline_str:
        return "gpu"
    if in_bundle and "--type=utility" in cmdline_str:
        match = _UTILITY_SUBTYPE_RE.search(cmdline_str)
        if match:
            return f"utility:{match.group(1).split('.')[0]}"
        return "utility:unknown"
    if exe and (exe.startswith("/System/") or exe.startswith("/usr/")):
        return "xpc"
    return "other"


_CLI_BASENAMES = {"claude", "claude.exe"}  # mise/npm global installs shim as claude.exe on this machine


def is_cli_process(exe: str | None) -> bool:
    """True for the Claude Code CLI, excluding Zed's embedded claude-agent-sdk (P-2)."""
    if not exe or "claude-agent-sdk" in exe:
        return False
    return os.path.basename(exe) in _CLI_BASENAMES


def compute_io_deltas(
    prev_read: int | None,
    prev_written: int | None,
    cur_read: int,
    cur_written: int,
    create_time: float,
    session_start_epoch: float,
    first_seen: bool,
) -> tuple[int, int]:
    """Delta rules: pre-existing processes contribute 0 on first observation."""
    if first_seen:
        if create_time >= session_start_epoch:
            return cur_read, cur_written
        return 0, 0
    read_delta = cur_read - prev_read if cur_read >= prev_read else 0
    write_delta = cur_written - prev_written if cur_written >= prev_written else 0
    return read_delta, write_delta


def sum_or_none(values) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def aggregate_processes(processes: list[dict], dt: float | None) -> dict:
    agg = {
        "count": len(processes),
        "cpu_pct": sum_or_none(p["cpu_pct"] for p in processes),
        "footprint": sum_or_none(p["footprint"] for p in processes),
        "rss": sum_or_none(p["rss"] for p in processes),
        "read_delta": sum_or_none(p["read_delta"] for p in processes),
        "write_delta": sum_or_none(p["write_delta"] for p in processes),
    }
    agg["read_rate"] = agg["read_delta"] / dt if agg["read_delta"] is not None and dt else None
    agg["write_rate"] = agg["write_delta"] / dt if agg["write_delta"] is not None and dt else None
    return agg


def parse_fs_usage_line(line: str) -> tuple[str, int] | None:
    """Parse one `fs_usage -w -f filesys` line for a write-family call.

    fs_usage's output format is undocumented and varies by macOS version, so
    this is deliberately best-effort: returns None for anything that isn't a
    write call, or that doesn't carry a byte count and a path we can find.
    """
    if not _WRITE_CALL_RE.search(line):
        return None
    bytes_match = _FS_USAGE_BYTES_RE.search(line)
    if not bytes_match:
        return None
    # fs_usage pads columns with runs of spaces; splitting on single whitespace
    # would break paths with embedded spaces (e.g. "Application Support").
    columns = re.split(r"\s{2,}", line.strip())
    path = next((col for col in columns if col.startswith("/")), None)
    if path is None:
        return None
    try:
        num_bytes = int(bytes_match.group(1), 16)
    except ValueError:
        return None
    return path, num_bytes


@dataclass
class _ProcState:
    process: psutil.Process
    last_read: int | None = None
    last_written: int | None = None
    io_seen: bool = False


class FsUsageTracer:
    """Runs `sudo fs_usage -w -f filesys` for the tracked PIDs (I-5).

    Only constructed/used when --trace-io is set; never invoked otherwise.
    """

    def __init__(self, window: float = 60.0, top_n: int = 10, restart_debounce: float = 30.0):
        self._window = window
        self._top_n = top_n
        self._restart_debounce = restart_debounce
        self._lock = threading.Lock()
        self._events: deque[tuple[float, str, int]] = deque()
        self._unparsed = 0
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._current_pids: frozenset[int] = frozenset()
        self._last_restart = float("-inf")

    def update_pids(self, pids: set[int], now: float) -> None:
        pids = frozenset(pids)
        if pids == self._current_pids:
            return
        if now - self._last_restart < self._restart_debounce:
            return
        self._current_pids = pids
        self._restart(now)

    def _restart(self, now: float) -> None:
        self._stop_process()
        self._last_restart = now
        if not self._current_pids:
            return
        cmd = ["sudo", "-n", "fs_usage", "-w", "-f", "filesys"] + [str(pid) for pid in sorted(self._current_pids)]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self._proc = None
            return
        self._thread = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
        self._thread.start()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self._ingest_line(line)
        except (OSError, ValueError):
            pass

    def _ingest_line(self, line: str) -> None:
        result = parse_fs_usage_line(line)
        now = time.monotonic()
        with self._lock:
            if result is not None:
                path, num_bytes = result
                self._events.append((now, path, num_bytes))
            elif _WRITE_CALL_RE.search(line):
                self._unparsed += 1

    def snapshot(self, now: float) -> dict:
        with self._lock:
            while self._events and now - self._events[0][0] > self._window:
                self._events.popleft()
            totals: dict[str, int] = {}
            for _, path, num_bytes in self._events:
                totals[path] = totals.get(path, 0) + num_bytes
            unparsed = self._unparsed
        top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[: self._top_n]
        return {
            "active": self._proc is not None and self._proc.poll() is None,
            "top": [{"path": path, "bytes": num_bytes} for path, num_bytes in top],
            "unparsed": unparsed,
        }

    def _stop_process(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        except OSError:
            pass
        self._proc = None
        self._thread = None

    def close(self) -> None:
        self._stop_process()


class ProcessCollector:
    def __init__(self, include_cli: bool = False, trace_io: bool = False):
        self._include_cli = include_cli
        self._trace_io = trace_io
        self._proc_cache: dict[tuple[int, float], _ProcState] = {}
        self._coalition_cache: dict[tuple[int, float], int | None] = {}
        self._session_start_epoch = time.time()
        self._last_poll: float | None = None
        self._tracer = FsUsageTracer() if trace_io else None

    def collect(self, now: float) -> dict:
        dt = None if self._last_poll is None else (now - self._last_poll)
        all_procs = _iter_processes()
        seen_keys = {
            (pid, ct)
            for pid, ct in ((_info(p, "pid"), _info(p, "create_time")) for p in all_procs)
            if pid is not None and ct is not None
        }
        self._prune_caches(seen_keys)

        main_proc, app_path = _find_main(all_procs)
        if main_proc is None:
            sample = {
                "claude": {"running": False, "main_pid": None, "coalition_id": None, "app_path": app_path},
                "processes": [],
                "roles": {},
                "total": aggregate_processes([], dt),
            }
            tracked_pids: set[int] = set()
        else:
            main_pid = _info(main_proc, "pid")
            main_key = (main_pid, _info(main_proc, "create_time"))
            cid = self._coalition_id_cached(main_key, main_pid)
            if cid is not None:
                tracked, attribution = self._tracked_by_coalition(all_procs, cid)
            else:
                tracked, attribution = self._tracked_by_fallback(all_procs, app_path, main_proc)
            sample = self._build_sample(main_proc, app_path, cid, tracked, attribution, dt)
            tracked_pids = {p["pid"] for p in sample["processes"]}

        if self._include_cli:
            sample["cli"] = self._collect_cli(all_procs, tracked_pids, dt)

        if self._trace_io and self._tracer is not None:
            self._tracer.update_pids(tracked_pids, now)
            sample["trace_io"] = self._tracer.snapshot(now)

        self._last_poll = now
        return sample

    def _prune_caches(self, seen_keys: set[tuple[int, float]]) -> None:
        for cache in (self._proc_cache, self._coalition_cache):
            for key in [k for k in cache if k not in seen_keys]:
                del cache[key]

    def _coalition_id_cached(self, key: tuple[int, float], pid: int) -> int | None:
        if key in self._coalition_cache:
            return self._coalition_cache[key]
        cid = coalition_id(pid)
        self._coalition_cache[key] = cid
        return cid

    def _tracked_by_coalition(
        self, all_procs: list[psutil.Process], cid: int
    ) -> tuple[list[psutil.Process], dict[int, str]]:
        tracked = []
        attribution: dict[int, str] = {}
        for p in all_procs:
            pid, create_time = _info(p, "pid"), _info(p, "create_time")
            if pid is None or create_time is None:
                continue
            if self._coalition_id_cached((pid, create_time), pid) == cid:
                tracked.append(p)
                attribution[pid] = "coalition"
        return tracked, attribution

    def _tracked_by_fallback(
        self, all_procs: list[psutil.Process], app_path: str | None, main_proc: psutil.Process
    ) -> tuple[list[psutil.Process], dict[int, str]]:
        tracked = [main_proc]
        main_pid = _info(main_proc, "pid")
        attribution: dict[int, str] = {main_pid: "path"}
        app_prefix = f"{app_path}/" if app_path else None
        for p in all_procs:
            pid = _info(p, "pid")
            if pid is None or pid == main_pid:
                continue
            exe = _safe_exe(p)
            if app_prefix and exe and exe.startswith(app_prefix):
                tracked.append(p)
                attribution[pid] = "path"
        for p in all_procs:
            pid = _info(p, "pid")
            if pid is None or pid in attribution or not _is_vm_process(p):
                continue
            tracked.append(p)
            attribution[pid] = "responsible" if responsible_pid(pid) == main_pid else "name?"
        return tracked, attribution

    def _build_sample(
        self,
        main_proc: psutil.Process,
        app_path: str | None,
        cid: int | None,
        tracked: list[psutil.Process],
        attribution: dict[int, str],
        dt: float | None,
    ) -> dict:
        main_pid = _info(main_proc, "pid")
        main_exe = _safe_exe(main_proc)
        app_prefix = f"{app_path}/" if app_path else None
        processes = []
        for p in tracked:
            pid, create_time = _info(p, "pid"), _info(p, "create_time")
            if pid is None or create_time is None:
                continue
            exe = _safe_exe(p)
            role = classify_role(exe, _safe_cmdline(p), main_exe, app_prefix)
            metrics = self._process_metrics(
                (pid, create_time), pid, create_time, exe, role, attribution.get(pid, "coalition"),
            )
            if metrics is not None:
                processes.append(metrics)
        by_role: dict[str, list[dict]] = {}
        for proc in processes:
            by_role.setdefault(proc["role"], []).append(proc)
        roles = {role: aggregate_processes(procs, dt) for role, procs in by_role.items()}
        return {
            "claude": {"running": True, "main_pid": main_pid, "coalition_id": cid, "app_path": app_path},
            "processes": processes,
            "roles": roles,
            "total": aggregate_processes(processes, dt),
        }

    def _collect_cli(self, all_procs: list[psutil.Process], tracked_pids: set[int], dt: float | None) -> dict:
        processes = []
        for p in all_procs:
            pid, create_time = _info(p, "pid"), _info(p, "create_time")
            if pid is None or create_time is None or pid in tracked_pids:
                continue
            exe = _safe_exe(p)
            if not is_cli_process(exe):
                continue
            metrics = self._process_metrics((pid, create_time), pid, create_time, exe, "cli", "name")
            if metrics is not None:
                processes.append(metrics)
        return {"processes": processes, "aggregate": aggregate_processes(processes, dt)}

    def _process_metrics(
        self,
        key: tuple[int, float],
        pid: int,
        create_time: float,
        exe: str | None,
        role: str,
        attribution: str,
    ) -> dict | None:
        state = self._proc_cache.get(key)
        if state is None:
            try:
                handle = psutil.Process(pid)
            except psutil.Error:
                return None
            state = _ProcState(process=handle)
            self._proc_cache[key] = state
            try:
                state.process.cpu_percent(None)  # prime; first reading is meaningless
            except psutil.Error:
                return None
            cpu_pct = None
        else:
            try:
                cpu_pct = state.process.cpu_percent(None)
            except psutil.Error:
                return None

        ru = rusage(pid)
        if ru is not None:
            footprint, rss = ru.footprint, ru.resident
        else:
            footprint = None
            try:
                rss = state.process.memory_info().rss
            except psutil.Error:
                rss = None

        if ru is None:
            disk_read = disk_written = read_delta = write_delta = None
        else:
            disk_read, disk_written = ru.disk_read, ru.disk_written
            read_delta, write_delta = compute_io_deltas(
                state.last_read, state.last_written, disk_read, disk_written,
                create_time, self._session_start_epoch, first_seen=not state.io_seen,
            )
            state.last_read, state.last_written, state.io_seen = disk_read, disk_written, True

        return {
            "pid": pid,
            "role": role,
            "exe": exe,
            "attribution": attribution,
            "cpu_pct": cpu_pct,
            "footprint": footprint,
            "rss": rss,
            "disk_read": disk_read,
            "disk_written": disk_written,
            "read_delta": read_delta,
            "write_delta": write_delta,
        }

    def close(self) -> None:
        if self._tracer is not None:
            self._tracer.close()


# ── §4 Disk collector ── (Plan 04)

# Immediate children of support/ that get a dedicated "paths" entry. Every
# other immediate child (and vm_bundles/, split one level further) still
# becomes a scan unit so support_total and vm_bundles add up correctly.
_TRACKED_DIR_UNITS = {
    "cache": "Cache",
    "code_cache": "Code Cache",
    "claude_code_vm": "claude-code-vm",
    "agent_sessions": "local-agent-mode-sessions",
    "indexeddb": "IndexedDB",
    "gpucache": "GPUCache",
}

_BUNDLE_UNIT_KEY = "vm_bundles/claudevm.bundle"


def walk_size(root: Path) -> dict:
    """Recursively sum apparent (st_size) and allocated (st_blocks*512) size under root.

    Symlinks aren't followed (only their own small size is counted). Hard
    links (nlink > 1) are counted once via an (st_dev, st_ino) set. Entries
    that vanish mid-walk or can't be read are skipped and counted in
    "skipped". A missing root returns {"present": False}.
    """
    try:
        root_stat = os.stat(root, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return {"present": False}

    apparent = 0
    allocated = 0
    files = 0
    skipped = 0
    seen_inodes: set[tuple[int, int]] = set()

    def add(st: os.stat_result, is_file: bool) -> None:
        nonlocal apparent, allocated, files
        if st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            if key in seen_inodes:
                return
            seen_inodes.add(key)
        allocated += st.st_blocks * 512
        if is_file:
            apparent += st.st_size
            files += 1

    add(root_stat, is_file=False)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            skipped += 1
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except (FileNotFoundError, PermissionError):
                skipped += 1
                continue
            if entry.is_symlink():
                add(st, is_file=False)
            elif stat_module.S_ISDIR(st.st_mode):
                add(st, is_file=False)
                stack.append(entry.path)
            else:
                add(st, is_file=True)
    return {"present": True, "apparent": apparent, "allocated": allocated, "files": files, "skipped": skipped}


def _walk_loose_files(directory: Path) -> dict:
    """Like walk_size, but only immediate files in directory (subdirs are their own units)."""
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return {"present": False}
    apparent = 0
    allocated = 0
    files = 0
    seen_inodes: set[tuple[int, int]] = set()
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
        except (FileNotFoundError, PermissionError):
            continue
        if not entry.is_symlink() and stat_module.S_ISDIR(st.st_mode):
            continue
        if st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            if key in seen_inodes:
                continue
            seen_inodes.add(key)
        allocated += st.st_blocks * 512
        apparent += st.st_size
        files += 1
    return {"present": True, "apparent": apparent, "allocated": allocated, "files": files, "skipped": 0}


def _vm_bundles_unit_specs(vm_bundles: Path) -> list[tuple[str, Path]]:
    specs: list[tuple[str, Path]] = []
    try:
        entries = list(os.scandir(vm_bundles))
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return specs
    loose_files = False
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            specs.append((f"vm_bundles/{entry.name}", Path(entry.path)))
        else:
            loose_files = True
    if loose_files:
        specs.append(("vm_bundles:files", vm_bundles))
    return specs


def _unit_specs(support: Path) -> dict[str, Path]:
    """Every scan unit currently on disk: immediate children of support/, with
    vm_bundles/ split one level further (D-5 scan-unit granularity)."""
    specs: list[tuple[str, Path]] = []
    try:
        entries = list(os.scandir(support))
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return {}
    loose_files = False
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if not is_dir:
            loose_files = True
            continue
        if entry.name == "vm_bundles":
            specs.extend(_vm_bundles_unit_specs(Path(entry.path)))
        else:
            specs.append((entry.name, Path(entry.path)))
    if loose_files:
        specs.append(("support:files", support))
    return dict(specs)


def _partial_files(root: Path, relative_to: Path) -> list[dict]:
    """*.partial files anywhere under root (D-8), with age in seconds."""
    if not root.is_dir():
        return []
    now = time.time()
    results = []
    try:
        candidates = list(root.rglob("*.partial"))
    except OSError:
        return results
    for path in candidates:
        try:
            st = path.stat()
        except (FileNotFoundError, PermissionError):
            continue
        results.append({
            "path": str(path.relative_to(relative_to)),
            "age_s": round(max(0.0, now - st.st_mtime), 1),
            "apparent": st.st_size,
            "allocated": st.st_blocks * 512,
        })
    return results


@dataclass
class _UnitStats:
    apparent: int = 0
    allocated: int = 0
    files: int = 0
    scan_ms: float | None = None
    walked_at: float | None = None  # monotonic time of the last walk


class _DirtyHandler(FileSystemEventHandler):
    """Marks scan units dirty from watchdog events. Runs on watchdog's thread; never walks."""

    def __init__(self, collector: "DiskCollector"):
        self._collector = collector

    def on_created(self, event) -> None:
        self._collector._mark_dirty(event.src_path)

    def on_deleted(self, event) -> None:
        self._collector._mark_dirty(event.src_path)

    def on_modified(self, event) -> None:
        self._collector._mark_dirty(event.src_path)

    def on_moved(self, event) -> None:
        self._collector._mark_dirty(event.src_path)
        self._collector._mark_dirty(event.dest_path)


class DiskCollector:
    """Disk footprint of the Claude support directory and Cowork VM bundle (§4)."""

    def __init__(self, support_dir: str, full_rescan: float = 300.0):
        # Resolved so it matches the (symlink-resolved) paths watchdog's FSEvents
        # backend reports on macOS, e.g. /tmp -> /private/tmp.
        self._support = Path(support_dir).resolve()
        self._full_rescan = full_rescan
        self._lock = threading.Lock()
        self._dirty: set[str] = set()
        self._units: dict[str, _UnitStats] = {}
        self._observer: Observer | None = None
        self._watch_error = False
        self._last_full_rescan = float("-inf")

    def collect(self, now: float) -> dict:
        if not self._support.is_dir():
            self._teardown_observer()
            self._units.clear()
            return {"paths": self._absent_paths(), "bundle": {"present": False}}

        self._ensure_observer()
        dirty = self._pop_dirty()
        force_all = "__all__" in dirty
        if now - self._last_full_rescan >= self._full_rescan:
            force_all = True
            self._last_full_rescan = now

        bundle = self._collect_bundle(now)

        specs = _unit_specs(self._support)
        for key, path in specs.items():
            if key == _BUNDLE_UNIT_KEY:
                continue  # fed by _collect_bundle, walked every poll regardless
            if force_all or key in dirty or key not in self._units:
                self._rewalk_unit(key, path, now)
        for key in [k for k in self._units if k not in specs]:
            del self._units[key]

        return {"paths": self._build_paths(now), "bundle": bundle}

    def close(self) -> None:
        self._teardown_observer()

    # -- observer plumbing --

    def _ensure_observer(self) -> None:
        if self._observer is not None or self._watch_error:
            return
        try:
            observer = Observer()
            observer.schedule(_DirtyHandler(self), str(self._support), recursive=True)
            observer.start()
        except Exception:
            self._watch_error = True
            return
        self._observer = observer

    def _teardown_observer(self) -> None:
        self._watch_error = False
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=2)
        except Exception:
            pass
        self._observer = None

    def _mark_dirty(self, event_path: str) -> None:
        try:
            rel = Path(event_path).relative_to(self._support)
        except ValueError:
            return
        parts = rel.parts
        if not parts:
            keys = ["__all__"]
        elif parts[0] == "vm_bundles":
            if len(parts) == 1:
                keys = ["__all__"]
            elif len(parts) == 2:
                keys = [f"vm_bundles/{parts[1]}", "vm_bundles:files"]
            else:
                keys = [f"vm_bundles/{parts[1]}"]
        elif len(parts) == 1:
            keys = [parts[0], "support:files"]
        else:
            keys = [parts[0]]
        with self._lock:
            self._dirty.update(keys)

    def _pop_dirty(self) -> set[str]:
        with self._lock:
            dirty, self._dirty = self._dirty, set()
        return dirty

    # -- walking --

    def _rewalk_unit(self, key: str, path: Path, now: float) -> None:
        started = time.monotonic()
        result = _walk_loose_files(path) if key.endswith(":files") else walk_size(path)
        scan_ms = (time.monotonic() - started) * 1000
        if result.get("present"):
            self._units[key] = _UnitStats(
                apparent=result["apparent"], allocated=result["allocated"],
                files=result["files"], scan_ms=scan_ms, walked_at=now,
            )
        else:
            self._units.pop(key, None)

    def _collect_bundle(self, now: float) -> dict:
        bundle_path = self._support / "vm_bundles" / "claudevm.bundle"
        started = time.monotonic()
        if not bundle_path.is_dir():
            self._units.pop(_BUNDLE_UNIT_KEY, None)
            return {"present": False}

        files: dict[str, dict] = {}
        rows = {name: {"apparent": 0, "allocated": 0} for name in ("rootfs.img", "sessiondata.img", "other")}
        total_apparent = 0
        total_allocated = 0
        for entry_path in bundle_path.rglob("*"):
            try:
                st = entry_path.lstat()
            except (FileNotFoundError, PermissionError):
                continue
            if not entry_path.is_symlink() and stat_module.S_ISDIR(st.st_mode):
                continue
            apparent = st.st_size
            allocated = st.st_blocks * 512
            rel = str(entry_path.relative_to(bundle_path))
            files[rel] = {"apparent": apparent, "allocated": allocated}
            total_apparent += apparent
            total_allocated += allocated
            row = entry_path.name if entry_path.name in rows else "other"
            rows[row]["apparent"] += apparent
            rows[row]["allocated"] += allocated
        rows["total"] = {"apparent": total_apparent, "allocated": total_allocated}

        zst_present = (bundle_path / "rootfs.img.zst").exists()
        partials = _partial_files(self._support / "vm_bundles", self._support)
        scan_ms = (time.monotonic() - started) * 1000

        self._units[_BUNDLE_UNIT_KEY] = _UnitStats(
            apparent=total_apparent, allocated=total_allocated,
            files=len(files), scan_ms=scan_ms, walked_at=now,
        )
        return {
            "present": True, "rows": rows, "files": files,
            "zst_present": zst_present, "partials": partials, "scan_ms": round(scan_ms, 2),
        }

    # -- reporting --

    def _unit_dict(self, unit: _UnitStats, now: float) -> dict:
        return {
            "present": True,
            "apparent": unit.apparent,
            "allocated": unit.allocated,
            "files": unit.files,
            "scan_ms": round(unit.scan_ms, 2) if unit.scan_ms is not None else None,
            "last_scan_elapsed": round(now - unit.walked_at, 1) if unit.walked_at is not None else None,
        }

    def _absent_paths(self) -> dict:
        names = ["support_total", "vm_bundles", "vm_warm", *_TRACKED_DIR_UNITS.keys()]
        return {name: {"present": False} for name in names}

    def _build_paths(self, now: float) -> dict:
        paths: dict[str, dict] = {}
        for name, dirname in _TRACKED_DIR_UNITS.items():
            unit = self._units.get(dirname)
            paths[name] = self._unit_dict(unit, now) if unit is not None else {"present": False}

        warm_unit = self._units.get("vm_bundles/warm")
        paths["vm_warm"] = self._unit_dict(warm_unit, now) if warm_unit is not None else {"present": False}

        if (self._support / "vm_bundles").is_dir():
            bundle_keys = [k for k in self._units if k == "vm_bundles:files" or k.startswith("vm_bundles/")]
            paths["vm_bundles"] = {
                "present": True,
                "apparent": sum(self._units[k].apparent for k in bundle_keys),
                "allocated": sum(self._units[k].allocated for k in bundle_keys),
                "files": sum(self._units[k].files for k in bundle_keys),
            }
        else:
            paths["vm_bundles"] = {"present": False}

        paths["support_total"] = {
            "present": True,
            "apparent": sum(unit.apparent for unit in self._units.values()),
            "allocated": sum(unit.allocated for unit in self._units.values()),
            "files": sum(unit.files for unit in self._units.values()),
        }
        return paths


# ── §5 System signals (swap, .diag reports) ── (Plan 05)

_DIAG_DIR = Path("/Library/Logs/DiagnosticReports")
_CLAUDE_COALITION_NAME = "com.anthropic.claudefordesktop"
_DIAG_RETRY_LIMIT = 2
_DIAG_HISTORIC_WINDOW_S = 86400.0

_DIAG_KV_RE = re.compile(r"^([A-Za-z][A-Za-z /]*?):\s*(.*)$")
_COALITION_RE = re.compile(r'^"(.*)"\((\d+)\)$')
_WRITES_RE = re.compile(
    r"([\d.]+)\s*MB.*?over\s+([\d.]+)\s*seconds\s*\(([\d.]+)\s*KB per second average\).*?"
    r"exceeding limit of\s+([\d.]+)\s*KB per second over\s+(\d+)\s*seconds"
)
_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")


def _vm_stat_pages(output: str, label: str) -> int | None:
    match = re.search(rf"^{re.escape(label)}:\s*([\d,]+)\.", output, re.MULTILINE)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_vm_stat(output: str) -> dict:
    """Parse `vm_stat` output into byte counts (M-2).

    swapins_bytes/swapouts_bytes are true swap (vm_stat's Swapins/Swapouts).
    pageins_bytes/pageouts_bytes are ordinary file paging (vm_stat's
    Pageins/Pageouts) -- these are what psutil calls sin/sout on macOS, and
    they are NOT swap. Any field that can't be found is None.
    """
    page_match = _VM_STAT_PAGE_SIZE_RE.search(output)
    page_size = int(page_match.group(1)) if page_match else None

    def _bytes(label: str) -> int | None:
        pages = _vm_stat_pages(output, label)
        if pages is None or page_size is None:
            return None
        return pages * page_size

    return {
        "swapins_bytes": _bytes("Swapins"),
        "swapouts_bytes": _bytes("Swapouts"),
        "pageins_bytes": _bytes("Pageins"),
        "pageouts_bytes": _bytes("Pageouts"),
    }


class SwapSampler:
    """Swap total/used/free (psutil, correct on macOS) plus true swap-ins/outs
    and pageins/outs (vm_stat, since psutil's sin/sout are file paging)."""

    def collect(self) -> dict:
        try:
            vm = psutil.swap_memory()
            result: dict[str, Any] = {"total": vm.total, "used": vm.used, "free": vm.free}
        except Exception:
            result = {"total": None, "used": None, "free": None}
        try:
            proc = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
            output = proc.stdout if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            output = ""
        if output:
            result.update(parse_vm_stat(output))
        else:
            result.update({"swapins_bytes": None, "swapouts_bytes": None,
                            "pageins_bytes": None, "pageouts_bytes": None})
        return result


def parse_diag_header(text: str) -> dict:
    """Parse the `Key: value` header lines of a macOS .diag report.

    Reads lines until the first blank line that comes *after* an `Event:`
    key has been seen (blank lines before that, e.g. between header blocks,
    don't stop the scan).
    """
    fields: dict[str, str] = {}
    seen_event = False
    for line in text.splitlines():
        if seen_event and not line.strip():
            break
        match = _DIAG_KV_RE.match(line)
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        fields[key] = value
        if key == "Event":
            seen_event = True
    return fields


def _split_coalition(raw: str | None) -> dict | None:
    if not raw:
        return None
    match = _COALITION_RE.match(raw)
    if not match:
        return {"name": raw, "id": None}
    return {"name": match.group(1), "id": int(match.group(2))}


def parse_writes_line(raw: str | None) -> dict | None:
    """Extract the numbers out of a diag report's `Writes:` line."""
    if not raw:
        return None
    match = _WRITES_RE.search(raw)
    if not match:
        return None
    mb, over_s, avg_kbps, limit_kbps, limit_duration_s = match.groups()
    return {
        "writes_mb": float(mb),
        "writes_over_s": float(over_s),
        "avg_kbps": float(avg_kbps),
        "limit_kbps": float(limit_kbps),
        "limit_duration_s": int(limit_duration_s),
    }


def is_claude_diag_report(fields: dict, claude_coalition_id: int | None = None) -> bool:
    """True when a parsed .diag report's header is Claude Desktop's (X-2)."""
    coalition = _split_coalition(fields.get("Resource Coalition"))
    if coalition is not None:
        if coalition["name"] == _CLAUDE_COALITION_NAME:
            return True
        if claude_coalition_id is not None and coalition["id"] == claude_coalition_id:
            return True
    path = fields.get("Path")
    if path and "/Claude.app/" in path:
        return True
    return False


def _build_diag_record(path: Path, fields: dict, historic: bool) -> dict:
    coalition = _split_coalition(fields.get("Resource Coalition"))
    pid = fields.get("PID")
    raw = {key: fields[key] for key in ("Writes", "Event") if key in fields}
    record = {
        "historic": historic,
        "file": str(path),
        "event": fields.get("Event"),
        "action": fields.get("Action taken"),
        "command": fields.get("Command"),
        "pid": int(pid) if pid and pid.isdigit() else None,
        "coalition": coalition,
        "start": fields.get("Date/Time"),
        "end": fields.get("End time"),
        "raw": raw,
    }
    writes = parse_writes_line(fields.get("Writes"))
    if writes is not None:
        record.update(writes)
    return record


def _current_claude_coalition_id() -> int | None:
    """Best-effort lookup of Claude's current resource coalition ID, used only
    to strengthen X-2 matching; report matching works without it (name/path)."""
    try:
        all_procs = _iter_processes()
    except Exception:
        return None
    main_proc, _app_path = _find_main(all_procs)
    if main_proc is None:
        return None
    pid = _info(main_proc, "pid")
    if pid is None:
        return None
    return coalition_id(pid)


class _DiagEventHandler(FileSystemEventHandler):
    def __init__(self, watcher: "DiagReportWatcher"):
        self._watcher = watcher

    def on_created(self, event) -> None:
        self._watcher._enqueue(event.src_path)

    def on_moved(self, event) -> None:
        self._watcher._enqueue(event.dest_path)


class DiagReportWatcher:
    """Watches for new Claude macOS diagnostic reports (X-1 - X-5)."""

    def __init__(self, directory: Path = _DIAG_DIR):
        self._directory = Path(directory)
        self._lock = threading.Lock()
        self._queued: set[str] = set()
        self._pending: dict[str, int] = {}
        self._seen: set[str] = set()
        self._observer: Observer | None = None
        self._watch_error = False
        self._historic_done = False

    def collect(self, now: float) -> dict:
        if not self._directory_readable():
            self._teardown_observer()
            return {"status": "unreadable", "new": [], "historic": []}

        self._ensure_observer()
        claude_coalition_id = _current_claude_coalition_id()

        historic: list[dict] = []
        if not self._historic_done:
            historic = self._startup_scan(claude_coalition_id)
            self._historic_done = True

        for path in self._pop_queued():
            if path not in self._seen:
                self._pending.setdefault(path, 0)

        new: list[dict] = []
        still_pending: dict[str, int] = {}
        for path, retries in self._pending.items():
            status, record = self._try_parse(path, historic=False, claude_coalition_id=claude_coalition_id)
            if status == "retry":
                if retries + 1 <= _DIAG_RETRY_LIMIT:
                    still_pending[path] = retries + 1
                    continue
                # gave up: treat as seen so it's never retried again
            self._seen.add(path)
            if record is not None:
                new.append(record)
        self._pending = still_pending

        return {"status": "ok", "new": new, "historic": historic}

    def close(self) -> None:
        self._teardown_observer()

    def _directory_readable(self) -> bool:
        try:
            os.listdir(self._directory)
            return True
        except OSError:
            return False

    def _ensure_observer(self) -> None:
        if self._observer is not None or self._watch_error:
            return
        try:
            observer = Observer()
            observer.schedule(_DiagEventHandler(self), str(self._directory), recursive=False)
            observer.start()
        except Exception:
            self._watch_error = True
            return
        self._observer = observer

    def _teardown_observer(self) -> None:
        self._watch_error = False
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=2)
        except Exception:
            pass
        self._observer = None

    def _enqueue(self, path: str) -> None:
        if not path.endswith(".diag"):
            return
        with self._lock:
            self._queued.add(path)

    def _pop_queued(self) -> set[str]:
        with self._lock:
            queued, self._queued = self._queued, set()
        return queued

    def _startup_scan(self, claude_coalition_id: int | None) -> list[dict]:
        """List Claude reports from the preceding 24h once, without flagging (X-4)."""
        cutoff = time.time() - _DIAG_HISTORIC_WINDOW_S
        results: list[dict] = []
        try:
            candidates = list(self._directory.glob("*.diag"))
        except OSError:
            return results
        for path in candidates:
            try:
                recent = path.stat().st_mtime >= cutoff
            except OSError:
                recent = False
            self._seen.add(str(path))
            if not recent:
                continue
            _status, record = self._try_parse(str(path), historic=True, claude_coalition_id=claude_coalition_id)
            if record is not None:
                results.append(record)
        return results

    def _try_parse(
        self, path: str, historic: bool, claude_coalition_id: int | None
    ) -> tuple[str, dict | None]:
        """Returns ("retry", None) for a not-yet-complete file, else ("done", record-or-None)."""
        p = Path(path)
        try:
            with open(p, "r", errors="replace") as fh:
                text = fh.read(16384)
        except (FileNotFoundError, PermissionError, OSError):
            return "retry", None
        fields = parse_diag_header("\n".join(text.splitlines()[:80]))
        if "Event" not in fields:
            return "retry", None
        if not is_claude_diag_report(fields, claude_coalition_id):
            return "done", None
        return "done", _build_diag_record(p, fields, historic=historic)


class SignalsCollector:
    """Swap counters and macOS diagnostic-report detection (§5)."""

    def __init__(self, diag_dir: Path = _DIAG_DIR):
        self._swap = SwapSampler()
        self._diag = DiagReportWatcher(directory=diag_dir)

    def collect(self, now: float) -> dict:
        return {"swap": self._swap.collect(), "diag": self._diag.collect(now)}

    def close(self) -> None:
        self._diag.close()


# ── §6 Analysis and alerts ── (Plan 06)

_MEM_WINDOW_S = 300.0
_WRITE_WINDOW_S = 60.0
_BUNDLE_RATE_WINDOW_S = 600.0
_MEM_BASELINE_FLOOR = 32 * 2**20
_CLEAR_HYSTERESIS = 0.9
_PARTIAL_STALE_S = 300.0
_BUDGET_2G = 2 * 2**30
_BUDGET_8G = 8 * 2**30
_WRITE_BUCKET_WINDOW_MIN = 1440
_BUNDLE_ROW_NAMES = ("rootfs.img", "sessiondata.img", "other", "total")


class TimeWindow:
    """A rolling window of (t, value) samples, evicted by elapsed time."""

    def __init__(self, seconds: float, interval: float):
        self.seconds = seconds
        self.interval = interval
        self._dq: deque[tuple[float, float]] = deque()

    def add(self, t: float, value: float | None) -> None:
        if value is not None:
            self._dq.append((t, value))
        while self._dq and t - self._dq[0][0] > self.seconds:
            self._dq.popleft()

    def mean(self) -> float | None:
        if not self._dq:
            return None
        return sum(v for _, v in self._dq) / len(self._dq)

    def oldest(self) -> float | None:
        return self._dq[0][1] if self._dq else None

    @property
    def full(self) -> bool:
        if not self._dq:
            return False
        return (self._dq[-1][0] - self._dq[0][0]) >= (self.seconds - self.interval)


class _Series:
    """Per-metric bookkeeping: current/delta/peak, plus an optional rolling window."""

    def __init__(self, window_s: float | None, interval: float):
        self.window = TimeWindow(window_s, interval) if window_s else None
        self.first_value: float | None = None
        self.prev_value: float | None = None
        self.peak: float | None = None

    def update(self, elapsed: float, value: float | None) -> dict:
        d_poll = value - self.prev_value if value is not None and self.prev_value is not None else None
        if value is not None:
            if self.first_value is None:
                self.first_value = value
            if self.peak is None or value > self.peak:
                self.peak = value
        d_start = value - self.first_value if value is not None and self.first_value is not None else None
        self.prev_value = value
        if self.window is not None:
            self.window.add(elapsed, value)
        return {"cur": value, "d_poll": d_poll, "d_start": d_start, "peak": self.peak}

    def reset_peak(self) -> None:
        self.peak = None


def _combined_roles(sample: dict) -> dict[str, dict]:
    combined = {"total": sample.get("total") or {}}
    combined.update(sample.get("roles") or {})
    return combined


class Analyzer:
    """Turns the raw sample stream into deltas/peaks/windows/baselines and alert flags."""

    def __init__(self, args: argparse.Namespace | None = None):
        args = args or argparse.Namespace(
            interval=3.0, cpu_threshold=30.0, cpu_window=60.0, write_threshold=parse_size("1MB"),
            budget_warn=80.0, mem_growth=50.0, bundle_growth=parse_size("1GB"), bundle_rate=parse_size("100MB"),
        )
        self.interval = args.interval
        self.cpu_threshold = args.cpu_threshold
        self.cpu_window_s = args.cpu_window
        self.write_threshold = args.write_threshold
        self.budget_warn = args.budget_warn
        self.mem_growth = args.mem_growth
        self.bundle_growth = args.bundle_growth
        self.bundle_rate = args.bundle_rate

        self._series: dict[str, _Series] = {}
        self._baselines: dict[str, float] = {}
        self._raised: set[str] = set()
        self._active_partials: set[str] = set()

        self._write_totals: dict[str, float] = {}
        self._write_buckets: dict[int, float] = {}
        self._bundle_rate_window = TimeWindow(_BUNDLE_RATE_WINDOW_S, self.interval)
        self._bundle_growth_start: float | None = None
        self._bundle_zst_prev: bool | None = None
        self._swap_streak = 0
        self._prev_swapout: float | None = None
        self._poll_index = 0

    @property
    def active_flags(self) -> list[str]:
        return sorted(self._raised)

    def reset(self) -> None:
        self._baselines.clear()
        for series in self._series.values():
            series.reset_peak()

    def analyze(self, sample: dict) -> tuple[dict, list[dict]]:
        elapsed = sample.get("elapsed")
        if elapsed is None:
            elapsed = self._poll_index * self.interval
        derived: dict[str, Any] = {}
        events: list[dict] = []

        for suffix, metrics in _combined_roles(sample).items():
            self._update_role(derived, events, elapsed, suffix, metrics)

        for name, info in (sample.get("paths") or {}).items():
            alloc = info.get("allocated") if info.get("present") else None
            app = info.get("apparent") if info.get("present") else None
            derived[f"path:{name}:alloc"] = self._series_for(f"path:{name}:alloc", None).update(elapsed, alloc)
            derived[f"path:{name}:app"] = self._series_for(f"path:{name}:app", None).update(elapsed, app)

        bundle = sample.get("bundle") or {}
        rows = bundle.get("rows") or {} if bundle.get("present") else {}
        for row in _BUNDLE_ROW_NAMES:
            row_info = rows.get(row) or {}
            alloc = row_info.get("allocated") if bundle.get("present") else None
            app = row_info.get("apparent") if bundle.get("present") else None
            derived[f"bundle:{row}:alloc"] = self._series_for(f"bundle:{row}:alloc", None).update(elapsed, alloc)
            derived[f"bundle:{row}:app"] = self._series_for(f"bundle:{row}:app", None).update(elapsed, app)

        swap = sample.get("swap") or {}
        derived["swap:used"] = self._series_for("swap:used", None).update(elapsed, swap.get("used"))
        derived["swap:swapins"] = self._series_for("swap:swapins", None).update(elapsed, swap.get("swapins_bytes"))
        swapouts = swap.get("swapouts_bytes")
        derived["swap:swapouts"] = self._series_for("swap:swapouts", None).update(elapsed, swapouts)

        bundle_total_alloc = derived["bundle:total:alloc"]["cur"]
        self._bundle_flags(events, elapsed, bundle, bundle_total_alloc)
        self._swap_streak_flag(events, swapouts)
        derived["swap_streak"] = self._swap_streak
        self._attribution_flag(events, sample.get("processes") or [])
        self._diag_events(events, sample.get("diag") or {})

        total = sample.get("total") or {}
        derived["write_budget"] = self._write_budget(elapsed, total.get("write_delta"))
        self._budget_flags(events, derived["write_budget"])

        self._poll_index += 1
        return derived, events

    # -- series plumbing --

    def _series_for(self, key: str, window_s: float | None) -> _Series:
        series = self._series.get(key)
        if series is None:
            series = _Series(window_s, self.interval)
            self._series[key] = series
        return series

    def _update_role(self, derived: dict, events: list[dict], elapsed: float, suffix: str, metrics: dict) -> None:
        cpu_id, mem_id, rss_id, wr_id, rd_id = (f"{p}:{suffix}" for p in ("cpu", "mem", "rss", "wr", "rd"))

        cpu_val = metrics.get("cpu_pct")
        cpu_series = self._series_for(cpu_id, self.cpu_window_s)
        cpu_out = cpu_series.update(elapsed, cpu_val)
        self._attach_baseline(cpu_out, cpu_id, cpu_series.window, kind="cpu")
        derived[cpu_id] = cpu_out

        mem_val = metrics.get("footprint")
        if mem_val is None:
            mem_val = metrics.get("rss")
        mem_series = self._series_for(mem_id, _MEM_WINDOW_S)
        mem_out = mem_series.update(elapsed, mem_val)
        self._attach_baseline(mem_out, mem_id, mem_series.window, kind="mem")
        derived[mem_id] = mem_out

        derived[rss_id] = self._series_for(rss_id, None).update(elapsed, metrics.get("rss"))

        wr_val = metrics.get("write_rate")
        wr_series = self._series_for(wr_id, _WRITE_WINDOW_S)
        wr_out = wr_series.update(elapsed, wr_val)
        wr_out["avg"] = wr_series.window.mean()
        derived[wr_id] = wr_out

        derived[rd_id] = self._series_for(rd_id, None).update(elapsed, metrics.get("read_rate"))

        write_delta = metrics.get("write_delta")
        if write_delta:
            self._write_totals[suffix] = self._write_totals.get(suffix, 0.0) + write_delta
        derived[f"wrsum:{suffix}"] = self._write_totals.get(suffix, 0.0)

        self._cpu_flag(events, suffix, cpu_id, cpu_out)
        self._mem_flag(events, suffix, mem_id, mem_out)
        self._write_flag(events, suffix, wr_id, wr_out)

    def _attach_baseline(self, out: dict, series_id: str, window: TimeWindow, kind: str) -> None:
        avg = window.mean()
        out["avg"] = avg
        if window.full and avg is not None:
            baseline = self._baselines.get(series_id)
            self._baselines[series_id] = avg if baseline is None else min(baseline, avg)
        baseline = self._baselines.get(series_id)
        out["baseline"] = baseline if baseline is not None else "warming"
        if avg is None or baseline is None:
            out["vs_baseline"] = None
        elif kind == "cpu":
            out["vs_baseline"] = avg - baseline
        else:
            out["vs_baseline"] = (avg / baseline - 1) if baseline else None

    # -- flag helpers --

    def _raise(self, events: list[dict], flag_id: str, metric: str, value, threshold, message: str) -> None:
        if flag_id not in self._raised:
            self._raised.add(flag_id)
            events.append({
                "id": flag_id, "state": "raised", "metric": metric,
                "value": value, "threshold": threshold, "message": message,
            })

    def _clear(self, events: list[dict], flag_id: str, metric: str, value, threshold, message: str) -> None:
        if flag_id in self._raised:
            self._raised.discard(flag_id)
            events.append({
                "id": flag_id, "state": "cleared", "metric": metric,
                "value": value, "threshold": threshold, "message": message,
            })

    def _event(self, events: list[dict], flag_id: str, metric: str, value, threshold, message: str) -> None:
        events.append({
            "id": flag_id, "state": "event", "metric": metric,
            "value": value, "threshold": threshold, "message": message,
        })

    def _cpu_flag(self, events: list[dict], suffix: str, flag_id: str, out: dict) -> None:
        avg = out["avg"]
        if not self._series[flag_id].window.full or avg is None:
            return
        if avg > self.cpu_threshold:
            self._raise(events, flag_id, "cpu_pct", avg, self.cpu_threshold,
                        f"{suffix} CPU {self.cpu_window_s:.0f}s avg {avg:.1f}% > {self.cpu_threshold:.0f}%")
        elif avg < _CLEAR_HYSTERESIS * self.cpu_threshold:
            self._clear(events, flag_id, "cpu_pct", avg, self.cpu_threshold,
                        f"{suffix} CPU {self.cpu_window_s:.0f}s avg {avg:.1f}% recovered")

    def _mem_flag(self, events: list[dict], suffix: str, flag_id: str, out: dict) -> None:
        avg = out["avg"]
        baseline = self._baselines.get(flag_id)
        if avg is None or baseline is None or baseline < _MEM_BASELINE_FLOOR:
            return
        ratio = avg / baseline - 1
        threshold_frac = self.mem_growth / 100
        if ratio > threshold_frac:
            self._raise(events, flag_id, "footprint", avg, self.mem_growth,
                        f"{suffix} memory 5m avg {format_bytes(avg)} > baseline "
                        f"{format_bytes(baseline)} + {self.mem_growth:.0f}%")
        elif ratio < _CLEAR_HYSTERESIS * threshold_frac:
            self._clear(events, flag_id, "footprint", avg, self.mem_growth,
                        f"{suffix} memory 5m avg {format_bytes(avg)} recovered toward baseline")

    def _write_flag(self, events: list[dict], suffix: str, flag_id: str, out: dict) -> None:
        avg = out["avg"]
        if avg is None:
            return
        if avg > self.write_threshold:
            self._raise(events, flag_id, "write_rate", avg, self.write_threshold,
                        f"{suffix} write rate 60s avg {format_bytes(avg)}/s > {format_bytes(self.write_threshold)}/s")
        elif avg < _CLEAR_HYSTERESIS * self.write_threshold:
            self._clear(events, flag_id, "write_rate", avg, self.write_threshold,
                        f"{suffix} write rate 60s avg {format_bytes(avg)}/s recovered")

    def _write_budget(self, elapsed: float, write_delta: float | None) -> dict:
        idx = int(elapsed // 60)
        if write_delta:
            self._write_buckets[idx] = self._write_buckets.get(idx, 0.0) + write_delta
        cutoff = idx - _WRITE_BUCKET_WINDOW_MIN
        for k in [k for k in self._write_buckets if k < cutoff]:
            del self._write_buckets[k]
        total_bytes = sum(self._write_buckets.values())
        window_s = min(elapsed, 86400.0)
        return {
            "window_s": round(window_s, 1),
            "bytes": total_bytes,
            "pct_2g": total_bytes / _BUDGET_2G * 100,
            "pct_8g": total_bytes / _BUDGET_8G * 100,
        }

    def _budget_flags(self, events: list[dict], write_budget: dict) -> None:
        for tier, pct_key, budget in (("2g", "pct_2g", _BUDGET_2G), ("8g", "pct_8g", _BUDGET_8G)):
            flag_id = f"budget:{tier}"
            pct = write_budget[pct_key]
            if pct >= self.budget_warn:
                self._raise(events, flag_id, pct_key, pct, self.budget_warn,
                            f"Total Claude writes {pct:.1f}% of {format_bytes(budget)}/24h budget")
            elif pct < _CLEAR_HYSTERESIS * self.budget_warn:
                self._clear(events, flag_id, pct_key, pct, self.budget_warn,
                            f"Total Claude writes {pct:.1f}% of {format_bytes(budget)}/24h budget, recovered")

    def _bundle_flags(self, events: list[dict], elapsed: float, bundle: dict, total_alloc: float | None) -> None:
        if self._poll_index == 0:
            self._bundle_growth_start = total_alloc if total_alloc is not None else 0.0

        if total_alloc is not None:
            growth = total_alloc - (self._bundle_growth_start or 0.0)
            if growth > self.bundle_growth:
                self._raise(events, "bundle:growth", "allocated", growth, self.bundle_growth,
                            f"VM bundle grown {format_bytes(growth)} since start > {format_bytes(self.bundle_growth)}")
            elif growth < _CLEAR_HYSTERESIS * self.bundle_growth:
                self._clear(events, "bundle:growth", "allocated", growth, self.bundle_growth,
                            f"VM bundle growth {format_bytes(growth)} since start, recovered")

            self._bundle_rate_window.add(elapsed, total_alloc)
            oldest = self._bundle_rate_window.oldest()
            if oldest is not None:
                rate = total_alloc - oldest
                if rate > self.bundle_rate:
                    self._raise(events, "bundle:rate", "allocated", rate, self.bundle_rate,
                                f"VM bundle grew {format_bytes(rate)} in the last 10m > {format_bytes(self.bundle_rate)}")
                elif rate < _CLEAR_HYSTERESIS * self.bundle_rate:
                    self._clear(events, "bundle:rate", "allocated", rate, self.bundle_rate,
                                f"VM bundle 10m growth {format_bytes(rate)}, recovered")

        if bundle.get("present"):
            zst_present = bundle.get("zst_present")
            if self._bundle_zst_prev is not None and zst_present != self._bundle_zst_prev:
                verb = "appeared" if zst_present else "disappeared"
                self._event(events, "bundle:zst", "zst_present", zst_present, None,
                            f"rootfs.img.zst {verb}")
            self._bundle_zst_prev = zst_present

            stale_now = {
                p["path"] for p in bundle.get("partials", []) if p.get("age_s", 0) > _PARTIAL_STALE_S
            }
            partials_by_path = {p["path"]: p for p in bundle.get("partials", [])}
            for path in stale_now - self._active_partials:
                flag_id = f"partial:{path}"
                age = partials_by_path[path]["age_s"]
                self._raise(events, flag_id, "age_s", age, _PARTIAL_STALE_S,
                            f"stale partial download {path} ({age:.0f}s old)")
            for path in self._active_partials - stale_now:
                flag_id = f"partial:{path}"
                self._clear(events, flag_id, "age_s", None, _PARTIAL_STALE_S,
                            f"partial download {path} no longer stale")
            self._active_partials = stale_now

    def _swap_streak_flag(self, events: list[dict], swapouts: float | None) -> None:
        if swapouts is not None and self._prev_swapout is not None and swapouts > self._prev_swapout:
            self._swap_streak += 1
        else:
            self._swap_streak = 0
        self._prev_swapout = swapouts

        if self._swap_streak >= 3:
            self._raise(events, "swap:streak", "swapouts_bytes", swapouts, 3,
                        f"swap-outs increased on {self._swap_streak} consecutive polls")
        else:
            self._clear(events, "swap:streak", "swapouts_bytes", swapouts, 3,
                        "swap-outs stopped increasing")

    def _attribution_flag(self, events: list[dict], processes: list[dict]) -> None:
        unresolved = [p for p in processes if p.get("attribution") == "name?"]
        if unresolved:
            self._raise(events, "attr:vm?", "attribution", len(unresolved), 0,
                        f"{len(unresolved)} process(es) attributed by name only (coalition/responsible-PID lookup failed)")
        else:
            self._clear(events, "attr:vm?", "attribution", 0, 0, "VM attribution resolved")

    def _diag_events(self, events: list[dict], diag: dict) -> None:
        for report in diag.get("new", []):
            filename = Path(report.get("file", "")).name
            writes_mb = report.get("writes_mb")
            if writes_mb is not None:
                message = (
                    f"Claude {report.get('event')} report: {writes_mb:.0f} MB over "
                    f"{report.get('writes_over_s', 0):.0f} s, limit {report.get('limit_kbps', 0):.2f} KB/s, "
                    f"action {report.get('action') or 'none'}"
                )
            else:
                message = f"Claude diagnostic report: {report.get('event')}, action {report.get('action') or 'none'}"
            self._event(events, f"diag:{filename}", "diag_report", report.get("event"), None, message)


# ── §7 JSONL log writer ──

class LogWriter:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", buffering=1)
        self._start_monotonic = time.monotonic()

    def write(self, record: dict) -> None:
        record = dict(record)
        record.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"))
        record.setdefault("elapsed", round(time.monotonic() - self._start_monotonic, 3))
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ── §8 Renderers: headless, TUI ──

class HeadlessRenderer:
    def __init__(self, log_path: str, interval: float):
        self._log_path = log_path
        self._interval = interval

    def start(self) -> None:
        print(f"claude-desktop-monitor: logging to {self._log_path} (interval {self._interval}s)",
              file=sys.stderr)

    def render(self, sample: dict, derived: dict, active_flags: list[str]) -> None:
        pass

    def on_flag(self, event: dict) -> None:
        ts = time.strftime("%H:%M:%S")
        print(
            f"{ts} FLAG {event.get('state')} {event.get('id')} "
            f"value={event.get('value')} threshold={event.get('threshold')} "
            f"{event.get('message', '')}",
            file=sys.stderr,
        )

    def poll_keys(self, timeout: float) -> list[str]:
        if timeout > 0:
            time.sleep(timeout)
        return []

    def stop(self) -> None:
        pass


# -- TUI formatting helpers --

_PATH_ROW_NAMES = (
    "vm_bundles", "vm_warm", "cache", "code_cache", "claude_code_vm",
    "agent_sessions", "indexeddb", "gpucache", "support_total",
)
_BUNDLE_DISPLAY_ROWS = (
    ("rootfs.img", "rootfs.img"), ("sessiondata.img", "sessiondata.img"),
    ("other", "other"), ("total", "bundle total"),
)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _fmt_cpu(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{format_bytes(value)}/s"


def _fmt_delta(value: float | None, unit: str = "bytes") -> str:
    if value is None:
        return "n/a"
    if value == 0:
        return "·"
    sign = "+" if value > 0 else "−"
    if unit == "pct":
        return f"{sign}{abs(value):.1f}%"
    return f"{sign}{format_bytes(abs(value))}"


def _fmt_baseline(baseline, kind: str) -> str:
    if baseline is None or baseline == "warming":
        return "…"
    return f"{baseline:.1f}%" if kind == "cpu" else format_bytes(baseline)


def _fmt_vs_baseline(vs_baseline: float | None, kind: str) -> str:
    if vs_baseline is None:
        return "…"
    return _fmt_delta(vs_baseline, "pct") if kind == "cpu" else _fmt_delta(vs_baseline * 100, "pct")


class TuiRenderer:
    """Live `rich` dashboard (§8, Plan 07). Computes no analysis of its own;
    every value shown comes from the sample or `derived` passed to render()."""

    def __init__(self, args: argparse.Namespace, log_path: str):
        self.args = args
        self._log_path = log_path
        self._console = Console()
        self._live: Live | None = None
        self._show_pids = False
        self._is_tty_stdin = sys.stdin.isatty()
        self._orig_termios = None
        self._pending_marker: str | None = None
        self._flag_info: dict[str, dict] = {}
        self._event_log: list[tuple[float, dict]] = []
        self._historic_diag: list[dict] = []
        self._last_sample: dict | None = None
        self._last_derived: dict | None = None
        self._last_active_flags: list[str] = []
        self._start_wall = None
        atexit.register(self._restore_terminal)

    # -- Renderer contract --

    def start(self) -> None:
        self._start_wall = datetime.now().astimezone()
        if self._is_tty_stdin:
            try:
                self._orig_termios = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except (termios.error, ValueError, OSError):
                self._is_tty_stdin = False
                self._orig_termios = None
        self._live = Live(console=self._console, auto_refresh=False, screen=True,
                           transient=False, vertical_overflow="crop")
        self._live.start()

    def render(self, sample: dict, derived: dict, active_flags: list[str]) -> None:
        self._last_sample = sample
        self._last_derived = derived
        self._last_active_flags = active_flags
        if (sample.get("diag") or {}).get("historic"):
            self._historic_diag = sample["diag"]["historic"]
        renderable = self._build_renderable(sample, derived, active_flags)
        if self._live is not None:
            self._live.update(renderable, refresh=True)

    def on_flag(self, event: dict) -> None:
        now = time.monotonic()
        self._event_log.append((now, event))
        cutoff = now - 600.0
        self._event_log = [(t, e) for t, e in self._event_log if t >= cutoff]
        flag_id = event.get("id")
        if event.get("state") == "raised":
            self._flag_info[flag_id] = {"message": event.get("message"), "raised_at": now}
        elif event.get("state") == "cleared":
            self._flag_info.pop(flag_id, None)

    def poll_keys(self, timeout: float) -> list[str]:
        if not self._is_tty_stdin:
            if timeout > 0:
                time.sleep(timeout)
            return []
        keys: list[str] = []
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([sys.stdin], [], [], remaining)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                ch = sys.stdin.read(1)
            except OSError:
                break
            if not ch:
                break
            if ch == "q":
                keys.append("q")
                break
            elif ch == "p":
                self._show_pids = not self._show_pids
                keys.append("p")
                self._rerender_last()
            elif ch == "b":
                keys.append("b")
            elif ch == "m":
                self._pending_marker = self._prompt_marker()
                keys.append("m")
        return keys

    def stop(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._restore_terminal()

    def take_pending_marker(self) -> str | None:
        label = self._pending_marker
        self._pending_marker = None
        return label

    # -- terminal handling --

    def _restore_terminal(self) -> None:
        if self._orig_termios is not None and self._is_tty_stdin:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._orig_termios)
            except (termios.error, OSError):
                pass

    def _prompt_marker(self) -> str:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
        self._restore_terminal()
        try:
            label = input("Marker label (Enter for none): ")
        except (EOFError, OSError):
            label = ""
        if self._is_tty_stdin:
            try:
                tty.setcbreak(sys.stdin.fileno())
            except (termios.error, OSError, ValueError):
                pass
        if self._live is not None:
            try:
                self._live.start()
            except Exception:
                pass
        self._rerender_last()
        return label.strip()

    def _rerender_last(self) -> None:
        if self._last_sample is not None:
            self.render(self._last_sample, self._last_derived or {}, self._last_active_flags)

    # -- rendering --

    def _build_renderable(self, sample: dict, derived: dict, active_flags: list[str]):
        active = set(active_flags)
        body = self._build_body(sample, derived, active)
        header = self._build_header(sample, hidden_rows=0)
        combined = Group(header, body)
        height = self._console.size.height
        hidden = 0
        if height:
            try:
                lines = self._console.render_lines(combined, self._console.options.update(height=None))
                hidden = max(0, len(lines) - height)
            except Exception:
                hidden = 0
        if hidden:
            header = self._build_header(sample, hidden_rows=hidden)
            combined = Group(header, body)
        return combined

    def _build_header(self, sample: dict, hidden_rows: int):
        claude = sample.get("claude") or {}
        running = claude.get("running")
        status = "running" if running else "not running"
        version = _claude_app_version() or "n/a"
        main_pid = claude.get("main_pid")
        cid = claude.get("coalition_id")
        elapsed = sample.get("elapsed") or 0.0
        start_str = self._start_wall.strftime("%Y-%m-%d %H:%M:%S %Z") if self._start_wall else "n/a"
        lines = [
            f"Session start: {start_str}  Elapsed: {_fmt_duration(elapsed)}  "
            f"Interval: {self.args.interval:.0f}s  Log: {self._log_path}",
            f"Claude: {status}  App: {version}  "
            f"Main PID: {main_pid if main_pid is not None else 'n/a'}  "
            f"Coalition: {cid if cid is not None else 'n/a'}  "
            f"trace-io: {'on' if self.args.trace_io else 'off'}",
            f"q quit · p PIDs ({'shown' if self._show_pids else 'hidden'}) · m marker · b reset"
            + ("" if self._is_tty_stdin else "  (keys disabled: stdin is not a TTY)"),
        ]
        if hidden_rows:
            lines.append(f"(terminal too small: {hidden_rows} rows hidden)")
        return Panel("\n".join(lines), title="claude-desktop-monitor")

    def _build_body(self, sample: dict, derived: dict, active: set[str]):
        left = [
            Panel(self._cpu_table(sample, derived, active), title="CPU (% of one core)"),
            Panel(self._mem_table(sample, derived, active), title="Memory"),
            Panel(self._io_table(sample, derived, active), title="Disk I/O"),
        ]
        if self.args.include_cli and sample.get("cli"):
            left.append(Panel(self._cli_table(sample), title="Claude Code CLI"))
        right = [
            Panel(self._footprint_table(sample, derived, active), title="Disk footprint"),
            Panel(self._system_table(sample, derived, active), title="System"),
            Panel(self._flags_renderable(sample, active), title="Flags and events"),
        ]
        if self._console.size.width >= 160:
            grid = Table.grid(expand=True, padding=(0, 1))
            grid.add_column(ratio=1)
            grid.add_column(ratio=1)
            grid.add_row(Group(*left), Group(*right))
            return grid
        return Group(*left, *right)

    def _role_order(self, sample: dict) -> list[str]:
        return ["total"] + sorted((sample.get("roles") or {}).keys())

    def _add_pid_subrows(
        self, table: Table, sample: dict, suffix: str, value_col: int, value_fn: Callable[[dict], str],
    ) -> None:
        n_cols = len(table.columns)
        processes = sorted(
            (p for p in (sample.get("processes") or []) if p.get("role") == suffix),
            key=lambda p: p.get("pid") or 0,
        )
        for proc in processes:
            attribution = proc.get("attribution")
            attr_label = "?" if attribution == "name?" else (attribution or "")
            exe_name = Path(proc["exe"]).name if proc.get("exe") else "n/a"
            row = [""] * n_cols
            row[0] = f"  {proc.get('pid')} {exe_name} [{attr_label}]"
            row[value_col] = value_fn(proc)
            table.add_row(*row, style="yellow" if attribution == "name?" else "dim")

    def _cpu_table(self, sample: dict, derived: dict, active: set[str]) -> Table:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for col in ("Role", "n", "Current", "Δpoll", "Δstart", "Peak", "Avg(60s)", "Baseline", "vs base"):
            table.add_column(col, justify="left" if col == "Role" else "right")
        roles = sample.get("roles") or {}
        for suffix in self._role_order(sample):
            out = derived.get(f"cpu:{suffix}")
            if out is None:
                continue
            n = sample.get("total", {}).get("count") if suffix == "total" else (roles.get(suffix) or {}).get("count")
            flag_id = f"cpu:{suffix}"
            style = "red" if flag_id in active else ("bold" if suffix == "total" else None)
            table.add_row(
                suffix, str(n) if n is not None else "n/a",
                _fmt_cpu(out["cur"]), _fmt_delta(out["d_poll"], "pct"), _fmt_delta(out["d_start"], "pct"),
                _fmt_cpu(out["peak"]), _fmt_cpu(out["avg"]),
                _fmt_baseline(out["baseline"], "cpu"), _fmt_vs_baseline(out["vs_baseline"], "cpu"),
                style=style,
            )
            if self._show_pids:
                self._add_pid_subrows(table, sample, suffix, 2, lambda p: _fmt_cpu(p.get("cpu_pct")))
        return table

    def _mem_table(self, sample: dict, derived: dict, active: set[str]) -> Table:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for col in ("Role", "Footprint", "Δpoll", "Δstart", "Peak", "RSS", "Avg(5m)", "vs base"):
            table.add_column(col, justify="left" if col == "Role" else "right")
        for suffix in self._role_order(sample):
            out = derived.get(f"mem:{suffix}")
            if out is None:
                continue
            rss = derived.get(f"rss:{suffix}") or {}
            flag_id = f"mem:{suffix}"
            style = "red" if flag_id in active else ("bold" if suffix == "total" else None)
            table.add_row(
                suffix, format_bytes(out["cur"]), _fmt_delta(out["d_poll"]), _fmt_delta(out["d_start"]),
                format_bytes(out["peak"]), format_bytes(rss.get("cur")), format_bytes(out["avg"]),
                _fmt_vs_baseline(out["vs_baseline"], "mem"),
                style=style,
            )
            if self._show_pids:
                def _mem_value(p: dict) -> str:
                    val = p.get("footprint")
                    if val is None:
                        val = p.get("rss")
                    return format_bytes(val)
                self._add_pid_subrows(table, sample, suffix, 1, _mem_value)
        return table

    def _io_table(self, sample: dict, derived: dict, active: set[str]) -> Group:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for col in ("Role", "Write/s", "Avg(60s)", "Read/s", "Written (session)", "Peak write/s"):
            table.add_column(col, justify="left" if col == "Role" else "right")
        for suffix in self._role_order(sample):
            wr = derived.get(f"wr:{suffix}")
            if wr is None:
                continue
            rd = derived.get(f"rd:{suffix}") or {}
            written = derived.get(f"wrsum:{suffix}")
            flag_id = f"wr:{suffix}"
            style = "red" if flag_id in active else ("bold" if suffix == "total" else None)
            table.add_row(
                suffix, _fmt_rate(wr["cur"]), _fmt_rate(wr.get("avg")), _fmt_rate(rd.get("cur")),
                format_bytes(written), _fmt_rate(wr["peak"]),
                style=style,
            )
            if self._show_pids:
                interval = self.args.interval

                def _write_rate(p: dict, interval: float = interval) -> str:
                    delta = p.get("write_delta")
                    return _fmt_rate(delta / interval if delta is not None and interval else None)

                self._add_pid_subrows(table, sample, suffix, 1, _write_rate)
        budget = derived.get("write_budget") or {}
        footer = Text(
            f"24h written: {format_bytes(budget.get('bytes'))} "
            f"({budget.get('pct_2g', 0.0):.1f}% of 2 GiB · {budget.get('pct_8g', 0.0):.1f}% of 8 GiB)"
        )
        return Group(table, footer)

    def _footprint_table(self, sample: dict, derived: dict, active: set[str]) -> Table:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for col in ("Path", "Allocated", "Apparent", "Δpoll", "Δstart", "Peak"):
            table.add_column(col, justify="left" if col == "Path" else "right")
        bundle_present = bool((sample.get("bundle") or {}).get("present"))
        bundle_active = ("bundle:growth" in active) or ("bundle:rate" in active)
        for row_name, label in _BUNDLE_DISPLAY_ROWS:
            if not bundle_present:
                table.add_row(label, "absent", "absent", "·", "·", "absent", style="dim")
                continue
            alloc = derived.get(f"bundle:{row_name}:alloc") or {}
            app = derived.get(f"bundle:{row_name}:app") or {}
            style = "red" if (row_name == "total" and bundle_active) else None
            table.add_row(
                label, format_bytes(alloc.get("cur")), format_bytes(app.get("cur")),
                _fmt_delta(alloc.get("d_poll")), _fmt_delta(alloc.get("d_start")), format_bytes(alloc.get("peak")),
                style=style,
            )
        paths = sample.get("paths") or {}
        for name in _PATH_ROW_NAMES:
            info = paths.get(name) or {}
            present = bool(info.get("present"))
            if not present:
                table.add_row(name, "absent", "absent", "·", "·", "absent", style="dim")
                continue
            alloc = derived.get(f"path:{name}:alloc") or {}
            app = derived.get(f"path:{name}:app") or {}
            style = "red" if (name == "vm_bundles" and bundle_active) else None
            table.add_row(
                name, format_bytes(alloc.get("cur")), format_bytes(app.get("cur")),
                _fmt_delta(alloc.get("d_poll")), _fmt_delta(alloc.get("d_start")), format_bytes(alloc.get("peak")),
                style=style,
            )
        return table

    def _system_table(self, sample: dict, derived: dict, active: set[str]) -> Table:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_column("Δpoll", justify="right")
        swap = sample.get("swap") or {}
        used = derived.get("swap:used") or {}
        ins = derived.get("swap:swapins") or {}
        outs = derived.get("swap:swapouts") or {}
        streak = derived.get("swap_streak", 0)
        style = "red" if "swap:streak" in active else None
        table.add_row(
            "Swap used/total", f"{format_bytes(used.get('cur'))} / {format_bytes(swap.get('total'))}",
            _fmt_delta(used.get("d_poll")), style=style,
        )
        table.add_row("Swap-ins (cum)", format_bytes(ins.get("cur")), _fmt_delta(ins.get("d_poll")), style=style)
        table.add_row("Swap-outs (cum)", format_bytes(outs.get("cur")), _fmt_delta(outs.get("d_poll")), style=style)
        table.add_row("Swap streak", str(streak), "", style=style)
        return table

    def _flags_renderable(self, sample: dict, active: set[str]) -> Group:
        parts: list[Any] = []
        if active:
            t = Table(box=box.SIMPLE_HEAVY, expand=True, title="Active flags")
            t.add_column("Flag")
            t.add_column("Since", justify="right")
            t.add_column("Message")
            now = time.monotonic()
            for flag_id in sorted(active):
                info = self._flag_info.get(flag_id, {})
                raised_at = info.get("raised_at")
                age = now - raised_at if raised_at is not None else None
                t.add_row(flag_id, _fmt_duration(age), info.get("message") or "", style="red")
            parts.append(t)
        else:
            parts.append(Text("No active flags."))

        now = time.monotonic()
        recent = [(t, e) for t, e in self._event_log if now - t <= 600.0]
        if recent:
            t2 = Table(box=box.SIMPLE_HEAVY, expand=True, title="Recent flag events (10 min)")
            t2.add_column("Ago", justify="right")
            t2.add_column("Flag")
            t2.add_column("Message")
            for ts, event in sorted(recent, key=lambda item: -item[0]):
                t2.add_row(_fmt_duration(now - ts), str(event.get("id")), event.get("message") or "")
            parts.append(t2)

        if self._historic_diag:
            t3 = Table(box=box.SIMPLE_HEAVY, expand=True, title="Historic .diag reports (before session)")
            t3.add_column("File")
            t3.add_column("Event")
            t3.add_column("Action")
            for report in self._historic_diag:
                t3.add_row(Path(report.get("file", "")).name, str(report.get("event")), str(report.get("action")))
            parts.append(t3)

        trace = sample.get("trace_io")
        if trace:
            t4 = Table(box=box.SIMPLE_HEAVY, expand=True, title="Top written files (60s, --trace-io)")
            t4.add_column("Path")
            t4.add_column("Bytes", justify="right")
            for item in trace.get("top", []):
                t4.add_row(item.get("path", ""), format_bytes(item.get("bytes")))
            if not trace.get("active"):
                t4.add_row("(fs_usage not active)", "", style="dim")
            parts.append(t4)

        return Group(*parts)

    def _cli_table(self, sample: dict) -> Table:
        cli = sample.get("cli") or {}
        agg = cli.get("aggregate") or {}
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Processes", str(agg.get("count", 0)))
        table.add_row("CPU", _fmt_cpu(agg.get("cpu_pct")))
        table.add_row("Footprint", format_bytes(agg.get("footprint")))
        table.add_row("Write/s", _fmt_rate(agg.get("write_rate")))
        return table


# ── §9 Main loop, entry point ──

def _collect_versions() -> dict:
    versions: dict[str, Any] = {"python": platform.python_version()}
    for name in ("psutil", "rich", "watchdog"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "unknown")
        except ImportError:
            versions[name] = "not installed"
    versions["macos"] = platform.mac_ver()[0]
    versions["claude_app"] = _claude_app_version()
    versions["native"] = dict(NATIVE_AVAILABLE)
    return versions


def _claude_app_version() -> str | None:
    plist_path = Path("/Applications/Claude.app/Contents/Info.plist")
    if not plist_path.exists():
        home_path = Path.home() / "Applications/Claude.app/Contents/Info.plist"
        plist_path = home_path if home_path.exists() else plist_path
    if not plist_path.exists():
        return None
    try:
        import plistlib

        with open(plist_path, "rb") as fh:
            info = plistlib.load(fh)
        return info.get("CFBundleShortVersionString")
    except (OSError, ValueError):
        return None


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _collect_host() -> dict:
    cpu_count = os.cpu_count()
    memsize = _sysctl("hw.memsize")
    return {
        "model": _sysctl("hw.model"),
        "cpu_count": cpu_count,
        "memsize": int(memsize) if memsize and memsize.isdigit() else None,
    }


@dataclass
class _RunState:
    stop_requested: bool = False
    stop_reason: str = "quit"
    marker_pending: str | None = None


def _install_signal_handlers(state: _RunState) -> None:
    def _on_term(signum, _frame):
        state.stop_requested = True
        state.stop_reason = "signal"

    def _on_usr1(signum, _frame):
        state.marker_pending = "SIGUSR1"

    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGTERM, _on_term)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _on_usr1)


def run(args: argparse.Namespace) -> int:
    log = LogWriter(args.log)
    collectors = [
        ProcessCollector(include_cli=args.include_cli, trace_io=args.trace_io),
        DiskCollector(support_dir=args.support_dir, full_rescan=args.full_rescan),
        SignalsCollector(),
    ]
    analyzer = Analyzer(args)

    is_tty = sys.stdout.isatty()
    use_headless = args.no_tui or not is_tty
    renderer = HeadlessRenderer(args.log, args.interval) if use_headless else TuiRenderer(args, args.log)

    state = _RunState()
    _install_signal_handlers(state)

    log.write({
        "type": "session_start",
        "config": vars(args),
        "versions": _collect_versions(),
        "host": _collect_host(),
    })

    renderer.start()

    start = time.monotonic()
    seq = 0
    reason = "quit"
    try:
        while not state.stop_requested:
            now = time.monotonic()
            sample: dict[str, Any] = {"type": "sample", "seq": seq, "elapsed": round(now - start, 3)}
            for collector in collectors:
                sample.update(collector.collect(now))
            derived, flag_events = analyzer.analyze(sample)
            sample["derived"] = derived
            sample["active_flags"] = analyzer.active_flags

            diag = sample.get("diag") or {}
            for report in [*diag.get("historic", []), *diag.get("new", [])]:
                log.write({"type": "diag_report", **report})

            log.write(sample)
            for event in flag_events:
                log.write({"type": "flag", **event})
                renderer.on_flag(event)
            renderer.render(sample, derived, sample["active_flags"])

            seq += 1
            deadline = start + seq * args.interval
            remaining = deadline - time.monotonic()
            keys = renderer.poll_keys(remaining) if remaining > 0 else []
            if "q" in keys:
                state.stop_requested = True
                state.stop_reason = "quit"
            if "b" in keys:
                analyzer.reset()
                state.marker_pending = "baseline reset"
            if "m" in keys and isinstance(renderer, TuiRenderer):
                label = renderer.take_pending_marker()
                state.marker_pending = label if label is not None else ""
            if state.marker_pending is not None:
                log.write({"type": "marker", "label": state.marker_pending})
                state.marker_pending = None
        reason = state.stop_reason
    except Exception:
        import traceback

        log.write({"type": "session_end", "reason": "error", "traceback": traceback.format_exc()})
        renderer.stop()
        for collector in collectors:
            collector.close()
        log.close()
        raise
    else:
        log.write({"type": "session_end", "reason": reason})
    finally:
        renderer.stop()
        for collector in collectors:
            collector.close()
        log.close()
    return 0


def _sudo_preflight() -> bool:
    """Prompt for sudo before any renderer starts (E-6): the only sudo use, and only here."""
    print(
        "claude-desktop-monitor: --trace-io needs sudo for fs_usage; you may be prompted for your password.",
        file=sys.stderr,
    )
    try:
        result = subprocess.run(["sudo", "-v"])
    except OSError:
        return False
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.trace_io and not _sudo_preflight():
        print("claude-desktop-monitor: sudo authentication failed; --trace-io requires it.", file=sys.stderr)
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
