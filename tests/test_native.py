import os
import tempfile

import psutil
import pytest

from claude_desktop_monitor import monitor

CLAUDE_EXE_PATHS = (
    "/Applications/Claude.app/Contents/MacOS/Claude",
    str(os.path.expanduser("~/Applications/Claude.app/Contents/MacOS/Claude")),
)


def _reap_child_pid() -> int:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def test_rusage_self():
    result = monitor.rusage(os.getpid())
    assert result is not None
    assert result.footprint > 0
    assert result.resident > 0


def test_coalition_id_self():
    result = monitor.coalition_id(os.getpid())
    assert result is not None
    assert result > 0


def test_responsible_pid_self():
    result = monitor.responsible_pid(os.getpid())
    assert result is not None
    assert result > 0


def test_native_functions_return_none_for_vanished_pid():
    pid = _reap_child_pid()
    assert monitor.rusage(pid) is None
    assert monitor.coalition_id(pid) is None
    assert monitor.responsible_pid(pid) is None


def test_disk_written_increases_after_write_and_fsync():
    before = monitor.rusage(os.getpid())
    assert before is not None
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(b"\0" * (1024 * 1024))
        tmp.flush()
        os.fsync(tmp.fileno())
        after = monitor.rusage(os.getpid())
    assert after is not None
    assert after.disk_written > before.disk_written


def _find_claude_main_pid() -> int | None:
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if proc.info["exe"] in CLAUDE_EXE_PATHS:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _find_vm_processes() -> list[int]:
    pids = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] == "com.apple.Virtualization.VirtualMachine":
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


@pytest.fixture(scope="module")
def claude_main_pid():
    pid = _find_claude_main_pid()
    if pid is None:
        pytest.skip("Claude Desktop is not running")
    return pid


def test_claude_vm_matched_by_coalition(claude_main_pid):
    claude_coalition = monitor.coalition_id(claude_main_pid)
    assert claude_coalition is not None

    vm_pids = _find_vm_processes()
    matched = [pid for pid in vm_pids if monitor.coalition_id(pid) == claude_coalition]
    other = [pid for pid in vm_pids if monitor.coalition_id(pid) not in (claude_coalition, None)]

    if not vm_pids:
        pytest.skip("no Virtualization.framework VM process is running")
    if not matched:
        pytest.skip("Cowork VM is not currently running")

    for pid in matched:
        assert monitor.rusage(pid) is not None

    for pid in other:
        assert monitor.coalition_id(pid) != claude_coalition
