import os
import time
from pathlib import Path

import pytest

from claude_desktop_monitor import monitor

FIXTURES = Path(__file__).parent / "fixtures"


def _poll_until(predicate, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


# ── vm_stat parser (M-2) ──


def test_parse_vm_stat_true_swap_vs_paging():
    output = (FIXTURES / "vm_stat_output.txt").read_text()
    result = monitor.parse_vm_stat(output)
    page_size = 16384
    assert result["swapins_bytes"] == 0
    assert result["swapouts_bytes"] == 0
    assert result["pageins_bytes"] == 16034848 * page_size
    assert result["pageouts_bytes"] == 381289 * page_size


def test_parse_vm_stat_missing_fields_are_none():
    result = monitor.parse_vm_stat("garbage, no page size header")
    assert result == {
        "swapins_bytes": None,
        "swapouts_bytes": None,
        "pageins_bytes": None,
        "pageouts_bytes": None,
    }


def test_swap_sampler_collect_shape():
    sample = monitor.SwapSampler().collect()
    assert set(sample) == {"total", "used", "free", "swapins_bytes", "swapouts_bytes",
                            "pageins_bytes", "pageouts_bytes"}


# ── .diag header parser and Writes-line regex ──


def test_parse_diag_header_claude_report():
    text = (FIXTURES / "claude_report_header.diag").read_text()
    fields = monitor.parse_diag_header(text)
    assert fields["Command"] == "Claude"
    assert fields["Event"] == "disk writes"
    assert fields["Resource Coalition"] == '"com.anthropic.claudefordesktop"(11603)'
    assert fields["PID"] == "87512"
    # stops at the blank line after the Event block, doesn't pick up later sections
    assert "Elapsed CPU time (seconds)" not in fields


def test_parse_writes_line():
    text = (FIXTURES / "claude_report_header.diag").read_text()
    fields = monitor.parse_diag_header(text)
    writes = monitor.parse_writes_line(fields["Writes"])
    assert writes == {
        "writes_mb": 2147.48,
        "writes_over_s": 1059.0,
        "avg_kbps": 2028.35,
        "limit_kbps": 24.86,
        "limit_duration_s": 86400,
    }


def test_parse_writes_line_none_when_absent():
    assert monitor.parse_writes_line(None) is None
    assert monitor.parse_writes_line("not a writes line") is None


# ── Claude match (X-2) ──


def test_claude_report_matches():
    fields = monitor.parse_diag_header((FIXTURES / "claude_report_header.diag").read_text())
    assert monitor.is_claude_diag_report(fields) is True


def test_docker_vm_report_does_not_match():
    fields = monitor.parse_diag_header((FIXTURES / "docker_vm_report_header.diag").read_text())
    assert monitor.is_claude_diag_report(fields) is False


def test_coalition_id_strengthens_match():
    fields = {"Resource Coalition": '"something.else"(4242)'}
    assert monitor.is_claude_diag_report(fields, claude_coalition_id=4242) is True
    assert monitor.is_claude_diag_report(fields, claude_coalition_id=1) is False


# ── DiagReportWatcher ──


def test_unreadable_directory_reports_status(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    os.chmod(directory, 0o000)
    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        sample = watcher.collect(time.monotonic())
        assert sample["status"] == "unreadable"
        assert sample["new"] == []
        assert sample["historic"] == []
    finally:
        os.chmod(directory, 0o755)
        watcher.close()


def test_synthetic_claude_report_detected_while_running(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        watcher.collect(time.monotonic())  # startup scan, nothing there yet

        report = directory / "Claude_2026-09-25-999999_nu.diag"
        report.write_text((FIXTURES / "claude_report_header.diag").read_text())

        def has_new():
            sample = watcher.collect(time.monotonic())
            return len(sample["new"]) == 1

        assert _poll_until(has_new)
        sample = watcher.collect(time.monotonic())
        assert sample["new"] == []  # dedup: not reported again
    finally:
        watcher.close()


def test_synthetic_docker_report_ignored(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        watcher.collect(time.monotonic())

        report = directory / "com.apple.Virtualization.VirtualMachine_2026-09-25-999999_nu.diag"
        report.write_text((FIXTURES / "docker_vm_report_header.diag").read_text())

        time.sleep(1.0)
        sample = watcher.collect(time.monotonic())
        assert sample["new"] == []
    finally:
        watcher.close()


def test_incomplete_file_retried_then_parsed(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        watcher.collect(time.monotonic())

        report = directory / "Claude_2026-09-25-888888_nu.diag"
        # written without an Event: line yet, as if still being flushed by macOS
        report.write_text("Command:          Claude\nPath:             /Applications/Claude.app/Contents/MacOS/Claude\n")
        watcher._enqueue(str(report))

        sample = watcher.collect(time.monotonic())
        assert sample["new"] == []
        assert str(report) in watcher._pending

        report.write_text((FIXTURES / "claude_report_header.diag").read_text())
        sample = watcher.collect(time.monotonic())
        assert len(sample["new"]) == 1
        assert str(report) not in watcher._pending
    finally:
        watcher.close()


def test_incomplete_file_gives_up_after_retry_limit(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        watcher.collect(time.monotonic())
        report = directory / "Claude_2026-09-25-777777_nu.diag"
        report.write_text("Command:          Claude\n")
        watcher._enqueue(str(report))

        for _ in range(monitor._DIAG_RETRY_LIMIT + 2):
            sample = watcher.collect(time.monotonic())
            assert sample["new"] == []

        assert str(report) not in watcher._pending
        assert str(report) in watcher._seen
    finally:
        watcher.close()


def test_historic_scan_finds_claude_ignores_docker(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    claude_report = directory / "Claude_2026-09-25-213433_nu.diag"
    claude_report.write_text((FIXTURES / "claude_report_header.diag").read_text())
    docker_report = directory / "com.apple.Virtualization.VirtualMachine_2026-09-25-210018_nu.diag"
    docker_report.write_text((FIXTURES / "docker_vm_report_header.diag").read_text())

    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        sample = watcher.collect(time.monotonic())
        assert len(sample["historic"]) == 1
        assert sample["historic"][0]["historic"] is True
        assert sample["historic"][0]["coalition"]["name"] == "com.anthropic.claudefordesktop"
        # historic reports never raise flags via "new"
        assert sample["new"] == []
    finally:
        watcher.close()


def test_historic_scan_skips_old_reports(tmp_path):
    directory = tmp_path / "DiagnosticReports"
    directory.mkdir()
    report = directory / "Claude_2026-09-01-000000_nu.diag"
    report.write_text((FIXTURES / "claude_report_header.diag").read_text())
    old_time = time.time() - 2 * 86400
    os.utime(report, (old_time, old_time))

    watcher = monitor.DiagReportWatcher(directory=directory)
    try:
        sample = watcher.collect(time.monotonic())
        assert sample["historic"] == []
    finally:
        watcher.close()


# ── SignalsCollector ──


def test_signals_collector_shape(tmp_path):
    collector = monitor.SignalsCollector(diag_dir=tmp_path)
    try:
        sample = collector.collect(time.monotonic())
        assert "swap" in sample and "diag" in sample
        assert sample["diag"]["status"] == "ok"
    finally:
        collector.close()
