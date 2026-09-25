#!/usr/bin/env python3
"""Claude Desktop Resource Monitor.

Watches Claude Desktop's resource usage over time, with emphasis on the
Cowork feature's local Linux VM. Observability only: never modifies,
deletes or restarts anything it observes.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

class ProcessCollector:
    def collect(self, now: float) -> dict:
        return {}

    def close(self) -> None:
        pass


# ── §4 Disk collector ── (Plan 04)

class DiskCollector:
    def collect(self, now: float) -> dict:
        return {}

    def close(self) -> None:
        pass


# ── §5 System signals (swap, .diag reports) ── (Plan 05)

class SignalsCollector:
    def collect(self, now: float) -> dict:
        return {}

    def close(self) -> None:
        pass


# ── §6 Analysis and alerts ── (Plan 06)

class Analyzer:
    def analyze(self, sample: dict) -> tuple[dict, list[dict]]:
        return {}, []

    def reset(self) -> None:
        pass


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
    collectors = [ProcessCollector(), DiskCollector(), SignalsCollector()]
    analyzer = Analyzer()

    is_tty = sys.stdout.isatty()
    use_headless = args.no_tui or not is_tty
    # TUI (Plan 07) not yet implemented; fall back to headless until then.
    renderer = HeadlessRenderer(args.log, args.interval)

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
            sample: dict[str, Any] = {"type": "sample", "seq": seq}
            for collector in collectors:
                sample.update(collector.collect(now))
            derived, flag_events = analyzer.analyze(sample)
            sample["derived"] = derived
            sample["active_flags"] = [event["id"] for event in flag_events if event.get("state") == "raised"]

            log.write(sample)
            for event in flag_events:
                log.write({"type": "flag", **event})
                renderer.on_flag(event)
            renderer.render(sample, derived, sample["active_flags"])

            seq += 1
            deadline = start + seq * args.interval
            remaining = deadline - time.monotonic()
            if remaining > 0:
                renderer.poll_keys(remaining)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
