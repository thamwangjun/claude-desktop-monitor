import builtins
import sys
import threading
import time
import types

import pytest

import monitor
from monitor import Notification, NotificationSender


def _n(flag_id="cpu:renderer", tier="warning", subtitle="High CPU — renderer", body="line 1\nline 2"):
    return Notification(flag_id, tier, monitor.NOTIFY_TITLE, subtitle, body)


class FakeBackend:
    def __init__(self, fail=False, sleep=0.0, fail_ids=()):
        self.calls = []
        self.fail = fail
        self.sleep = sleep
        self.fail_ids = set(fail_ids)

    def __call__(self, n, wait):
        self.calls.append((n, wait))
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail or n.flag_id in self.fail_ids:
            raise RuntimeError("boom")


def _wait_results(sender, count, timeout=2.0):
    results = []
    deadline = time.monotonic() + timeout
    while len(results) < count and time.monotonic() < deadline:
        results += sender.drain_results()
        time.sleep(0.01)
    return results


# ── text helpers ──

def test_escape_leading_dash_and_bracket():
    assert monitor.escape_notifier_text("-x") == "\\-x"
    assert monitor.escape_notifier_text("[x") == "\\[x"
    assert monitor.escape_notifier_text("normal -x [y") == "normal -x [y"


def test_truncate():
    assert monitor.truncate("short", 10) == "short"
    assert monitor.truncate("a" * 10, 10) == "a" * 10
    out = monitor.truncate("a" * 11, 10)
    assert len(out) == 10 and out.endswith("…")


def test_shorten_middle_keeps_file_name():
    path = "/Library/Logs/DiagnosticReports/Claude_2026-09-25-213433_nu.diag"
    out = monitor.shorten_middle(path, 55)
    assert len(out) == 55
    assert "…" in out
    assert out.startswith("/Library/")
    assert out.endswith("Claude_2026-09-25-213433_nu.diag")
    assert monitor.shorten_middle("short", 55) == "short"


def test_notifier_kwargs_limits_and_escaping(tmp_path):
    n = _n(subtitle="S" * 50, body="-" + "b" * 70 + "\nsecond\nthird")
    message, kwargs = monitor.notifier_kwargs(n, icon=tmp_path / "missing.png")
    lines = message.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("\\-")
    assert len(lines[0]) == monitor.BODY_LINE_MAX + 1  # truncated line plus the escape
    assert len(kwargs["subtitle"]) == monitor.SUBTITLE_MAX
    assert kwargs["title"] == "Claude Monitor"


def test_tier_sounds():
    assert monitor.TIER_SOUNDS == {"critical": "Basso", "warning": "Glass"}
    _, kw = monitor.notifier_kwargs(_n(tier="critical"))
    assert kw["sound"] == "Basso"
    _, kw = monitor.notifier_kwargs(_n(tier="warning"))
    assert kw["sound"] == "Glass"


def test_content_image_present_and_omitted(tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    _, kw = monitor.notifier_kwargs(_n(), icon=icon)
    assert kw["contentImage"] == str(icon)
    _, kw = monitor.notifier_kwargs(_n(), icon=tmp_path / "missing.png")
    assert "contentImage" not in kw


def test_committed_icon_exists():
    assert monitor.NOTIFY_ICON.exists()


# ── load_pync ──

def test_load_pync_success(monkeypatch):
    fake = types.ModuleType("pync")
    monkeypatch.setitem(sys.modules, "pync", fake)
    module, error = monitor.load_pync()
    assert module is fake and error is None


def test_load_pync_failure(monkeypatch):
    # pync raises a plain Exception at import when terminal-notifier is unusable.
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "pync":
            raise Exception("pync was not properly installed.")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "pync", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    module, error = monitor.load_pync()
    assert module is None
    assert error == "Exception: pync was not properly installed."


def test_pync_backend_passes_wait_and_kwargs(monkeypatch):
    calls = []
    fake = types.ModuleType("pync")
    fake.Notifier = types.SimpleNamespace(notify=lambda msg, **kw: calls.append((msg, kw)))
    monkeypatch.setitem(sys.modules, "pync", fake)
    monitor._pync_backend(_n(tier="critical"), True)
    msg, kw = calls[0]
    assert msg == "line 1\nline 2"
    assert kw["wait"] is True and kw["sound"] == "Basso" and kw["subtitle"] == "High CPU — renderer"


# ── sender ──

def test_send_is_non_blocking():
    backend = FakeBackend(sleep=1.0)
    sender = NotificationSender(backend)
    start = time.monotonic()
    sender.send(_n())
    assert time.monotonic() - start < 0.05
    sender.close()


def test_worker_calls_backend_with_wait_true():
    backend = FakeBackend()
    sender = NotificationSender(backend)
    sender.send(_n())
    sender.flush(timeout=2)
    assert backend.calls[0][1] is True
    sender.close()


def test_success_produces_sent():
    sender = NotificationSender(FakeBackend())
    n = _n()
    sender.send(n)
    results = _wait_results(sender, 1)
    assert len(results) == 1
    assert results[0].notification == n
    assert results[0].status == "sent" and results[0].error is None and not results[0].first_failure
    sender.close()


def test_failure_and_first_failure_only_once():
    sender = NotificationSender(FakeBackend(fail=True))
    sender.send(_n(flag_id="a"))
    sender.send(_n(flag_id="b"))
    results = _wait_results(sender, 2)
    assert [r.status for r in results] == ["failed", "failed"]
    assert results[0].error == "RuntimeError: boom"
    assert [r.first_failure for r in results] == [True, False]
    sender.close()


def test_queue_full_fails_immediately():
    release = threading.Event()
    backend = FakeBackend()
    backend_calls = backend.__call__

    def blocking(n, wait):
        release.wait(2)
        backend_calls(n, wait)

    sender = NotificationSender(blocking, max_queue=1)
    sender.send(_n(flag_id="in-worker"))
    deadline = time.monotonic() + 1
    while sender._queue.qsize() and time.monotonic() < deadline:  # worker has taken the first one
        time.sleep(0.01)
    sender.send(_n(flag_id="queued"))
    start = time.monotonic()
    sender.send(_n(flag_id="overflow"))
    assert time.monotonic() - start < 0.05
    results = sender.drain_results()
    assert len(results) == 1
    assert results[0].notification.flag_id == "overflow"
    assert results[0].status == "failed" and results[0].error == "queue full"
    assert results[0].first_failure
    release.set()
    sender.close()


def test_fire_and_forget_uses_wait_false_on_calling_thread():
    backend = FakeBackend()
    seen_threads = []
    sender = NotificationSender(lambda n, wait: (seen_threads.append(threading.current_thread()), backend(n, wait)))
    start = time.monotonic()
    sender.fire_and_forget(_n(flag_id="monitor:exit", tier="critical"))
    assert time.monotonic() - start < 0.05
    assert backend.calls[0][1] is False
    assert seen_threads == [threading.current_thread()]
    assert sender.drain_results() == []
    sender.close()


def test_fire_and_forget_swallows_errors():
    sender = NotificationSender(FakeBackend(fail=True))
    sender.fire_and_forget(_n())
    sender.close()


# ── --notify-test ──

def test_notify_test_flag_parses():
    assert monitor.parse_args(["--notify-test"]).notify_test is True
    assert monitor.parse_args([]).notify_test is False


def test_notify_test_success_exit_0(capsys):
    backend = FakeBackend()
    assert monitor.notify_test(backend) == 0
    assert [(n.tier, monitor.TIER_SOUNDS[n.tier]) for n, _ in backend.calls] == [
        ("warning", "Glass"), ("critical", "Basso")]
    err = capsys.readouterr().err
    assert "warning: sent" in err and "critical: sent" in err


def test_notify_test_one_failure_exit_1(capsys):
    assert monitor.notify_test(FakeBackend(fail_ids={"test:critical"})) == 1
    err = capsys.readouterr().err
    assert "warning: sent" in err and "critical: failed (RuntimeError: boom)" in err


def test_notify_test_pync_unavailable_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "load_pync", lambda: (None, "Exception: no terminal-notifier"))
    assert monitor.notify_test() == 1
    assert "no terminal-notifier" in capsys.readouterr().err


def test_main_routes_notify_test_before_run(monkeypatch):
    monkeypatch.setattr(monitor, "notify_test", lambda: 7)
    monkeypatch.setattr(monitor, "run", lambda args: pytest.fail("run() must not be called"))
    monkeypatch.setattr(monitor, "_sudo_preflight", lambda: pytest.fail("sudo must not be called"))
    assert monitor.main(["--notify-test", "--trace-io"]) == 7
