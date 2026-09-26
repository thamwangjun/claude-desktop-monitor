import argparse
import json
import signal

import pytest

from claude_desktop_monitor import monitor


def make_run_args(tmp_path, **overrides):
    base = dict(
        interval=0.05,
        full_rescan=300.0,
        cpu_threshold=30.0,
        cpu_window=60.0,
        write_threshold=monitor.parse_size("1MB"),
        budget_warn=80.0,
        mem_growth=50.0,
        bundle_growth=monitor.parse_size("1GB"),
        bundle_rate=monitor.parse_size("100MB"),
        log=str(tmp_path / "log.jsonl"),
        no_tui=True,
        include_cli=False,
        trace_io=False,
        support_dir=str(tmp_path / "support"),
        notify_test=False,
        no_notify=False,
        notify_cooldown=300.0,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def read_records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


def _sample_fragment():
    return {
        "claude": {"running": True, "main_pid": 111, "coalition_id": 4242, "app_path": None},
        "total": {"count": 0, "cpu_pct": None, "footprint": None, "rss": None, "write_rate": None,
                   "read_rate": None, "write_delta": None},
        "roles": {},
        "processes": [],
    }


class FakeProcessCollector:
    """Fake §3 collector. `on_first_collect` (if set) runs once, before returning the fragment."""

    def __init__(self, on_first_collect=None, **kw):
        self._on_first_collect = on_first_collect
        self._called = False

    def collect(self, now):
        if not self._called and self._on_first_collect is not None:
            self._on_first_collect()
        self._called = True
        return _sample_fragment()

    def close(self):
        pass


class FakeDiskCollector:
    def __init__(self, **kw):
        pass

    def collect(self, now):
        return {"paths": {}, "bundle": {"present": False}}

    def close(self):
        pass


class FakeSignalsCollector:
    def __init__(self, **kw):
        pass

    def collect(self, now):
        return {
            "swap": {"total": None, "used": None, "free": None, "swapins_bytes": None, "swapouts_bytes": None},
            "diag": {"new": [], "historic": []},
        }

    def close(self):
        pass


class FakeSender:
    def __init__(self):
        self.calls = []

    def send(self, n):
        self.calls.append(("send", n))

    def drain_results(self):
        return []

    def flush(self, timeout):
        self.calls.append(("flush", timeout))

    def fire_and_forget(self, n):
        self.calls.append(("fire_and_forget", n))

    def close(self):
        self.calls.append(("close",))


class FakeRenderer:
    """Stands in for HeadlessRenderer; `key_batches` are returned from poll_keys() in order."""

    def __init__(self, *args, key_batches=(), **kwargs):
        self.stopped = False
        self.notify_warnings = []
        self._batches = list(key_batches)

    def start(self):
        pass

    def render(self, sample, derived, active_flags):
        pass

    def on_flag(self, event):
        pass

    def poll_keys(self, timeout):
        if self._batches:
            return self._batches.pop(0)
        return []

    def stop(self):
        self.stopped = True

    def on_notify_warning(self, message):
        self.notify_warnings.append(message)


@pytest.fixture(autouse=True)
def patch_collectors(monkeypatch):
    monkeypatch.setattr(monitor, "ProcessCollector", FakeProcessCollector)
    monkeypatch.setattr(monitor, "DiskCollector", FakeDiskCollector)
    monkeypatch.setattr(monitor, "SignalsCollector", FakeSignalsCollector)
    monkeypatch.setattr(monitor, "load_pync", lambda: (object(), None))
    monkeypatch.setattr(monitor, "NotificationSender", FakeSender)


# ── _install_signal_handlers ──

def test_signal_handlers_set_distinct_reasons():
    state = monitor._RunState()
    monitor._install_signal_handlers(state)
    try:
        signal.raise_signal(signal.SIGINT)
        assert state.stop_reason == "sigint"
        state.stop_requested = False

        signal.raise_signal(signal.SIGTERM)
        assert state.stop_reason == "sigterm"
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)


def test_sighup_left_alone_when_inherited_as_sig_ignore():
    original = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        state = monitor._RunState()
        monitor._install_signal_handlers(state)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, original)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_sighup_installed_when_not_ignored():
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    try:
        state = monitor._RunState()
        monitor._install_signal_handlers(state)
        assert signal.getsignal(signal.SIGHUP) is not signal.SIG_DFL
        assert signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


# ── build_exit_notification / _session_end_record ──

def test_build_exit_notification_shape():
    n = monitor.build_exit_notification("SIGTERM", "/tmp/logs/monitor.jsonl")
    assert n.flag_id == "monitor:exit"
    assert n.tier == "critical"
    assert n.subtitle == "Monitor stopped unexpectedly"
    lines = n.body.split("\n")
    assert lines[0] == "SIGTERM"
    assert lines[1].startswith("Alerts have stopped. Log: ")
    assert "/tmp/logs/monitor.jsonl" in lines[1]


def test_build_exit_notification_shortens_long_log_path():
    long_path = "/Users/someone/" + ("x" * 100) + "/monitor.jsonl"
    n = monitor.build_exit_notification("RuntimeError: boom", long_path)
    body_line2 = n.body.split("\n")[1]
    assert len(body_line2) <= monitor.BODY_LINE_MAX
    assert "monitor.jsonl" in body_line2


def test_session_end_record_quit():
    assert monitor._session_end_record("quit") == {"type": "session_end", "reason": "quit"}


def test_session_end_record_signals():
    assert monitor._session_end_record("sigint") == {
        "type": "session_end", "reason": "signal", "signal": "SIGINT",
    }
    assert monitor._session_end_record("sigterm") == {
        "type": "session_end", "reason": "signal", "signal": "SIGTERM",
    }
    assert monitor._session_end_record("sighup") == {
        "type": "session_end", "reason": "signal", "signal": "SIGHUP",
    }


# ── run(): unexpected exit paths ──

def test_run_sigterm_sends_exit_notification_before_flush(tmp_path, monkeypatch):
    args = make_run_args(tmp_path)

    def _send_sigterm():
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(
        monitor, "ProcessCollector",
        lambda **kw: FakeProcessCollector(on_first_collect=_send_sigterm),
    )
    monitor.run(args)

    records = read_records(args.log)
    exit_note = [r for r in records if r["type"] == "notification" and r["id"] == "monitor:exit"]
    assert len(exit_note) == 1
    assert exit_note[0]["status"] == "dispatched"
    session_end = [r for r in records if r["type"] == "session_end"][0]
    assert session_end["reason"] == "signal" and session_end["signal"] == "SIGTERM"


def test_run_sigterm_fire_and_forget_called_before_flush(tmp_path, monkeypatch):
    args = make_run_args(tmp_path)
    senders = []

    class _TrackingFakeSender(FakeSender):
        def __init__(self):
            super().__init__()
            senders.append(self)

    monkeypatch.setattr(monitor, "NotificationSender", _TrackingFakeSender)

    def _send_sigterm():
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(
        monitor, "ProcessCollector",
        lambda **kw: FakeProcessCollector(on_first_collect=_send_sigterm),
    )
    monitor.run(args)

    sender = senders[-1]
    call_names = [c[0] for c in sender.calls]
    assert call_names.count("fire_and_forget") == 1
    assert call_names.index("fire_and_forget") < call_names.index("flush")


def test_run_sighup_sends_exit_notification(tmp_path, monkeypatch):
    args = make_run_args(tmp_path)

    def _send_sighup():
        signal.raise_signal(signal.SIGHUP)

    monkeypatch.setattr(
        monitor, "ProcessCollector",
        lambda **kw: FakeProcessCollector(on_first_collect=_send_sighup),
    )
    monitor.run(args)

    records = read_records(args.log)
    exit_note = [r for r in records if r["type"] == "notification" and r["id"] == "monitor:exit"]
    assert len(exit_note) == 1
    session_end = [r for r in records if r["type"] == "session_end"][0]
    assert session_end["reason"] == "signal" and session_end["signal"] == "SIGHUP"


def test_run_injected_exception_sends_exit_notification_and_propagates(tmp_path, monkeypatch):
    args = make_run_args(tmp_path)

    class RaisingProcessCollector(FakeProcessCollector):
        def collect(self, now):
            raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "ProcessCollector", lambda **kw: RaisingProcessCollector())
    with pytest.raises(RuntimeError, match="boom"):
        monitor.run(args)

    records = read_records(args.log)
    exit_note = [r for r in records if r["type"] == "notification" and r["id"] == "monitor:exit"]
    assert len(exit_note) == 1
    assert "RuntimeError: boom" in exit_note[0]["body"]
    session_end = [r for r in records if r["type"] == "session_end"][0]
    assert session_end["reason"] == "error"
    assert "traceback" in session_end


def test_run_no_notify_suppresses_exit_notification_on_exception(tmp_path, monkeypatch):
    args = make_run_args(tmp_path, no_notify=True)

    class RaisingProcessCollector(FakeProcessCollector):
        def collect(self, now):
            raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "ProcessCollector", lambda **kw: RaisingProcessCollector())
    with pytest.raises(RuntimeError, match="boom"):
        monitor.run(args)

    records = read_records(args.log)
    exit_note = [r for r in records if r["type"] == "notification" and r["id"] == "monitor:exit"]
    assert exit_note == []


# ── run(): clean exits do not notify ──

def test_run_quit_key_no_exit_notification(tmp_path, monkeypatch):
    args = make_run_args(tmp_path)
    monkeypatch.setattr(monitor, "HeadlessRenderer", lambda *a, **kw: FakeRenderer(key_batches=[["q"]]))
    monitor.run(args)

    records = read_records(args.log)
    exit_note = [r for r in records if r["type"] == "notification" and r["id"] == "monitor:exit"]
    assert exit_note == []
    session_end = [r for r in records if r["type"] == "session_end"][0]
    assert session_end["reason"] == "quit" and "signal" not in session_end


def test_run_sigint_no_exit_notification(tmp_path, monkeypatch):
    args = make_run_args(tmp_path)

    def _send_sigint():
        signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(
        monitor, "ProcessCollector",
        lambda **kw: FakeProcessCollector(on_first_collect=_send_sigint),
    )
    monitor.run(args)

    records = read_records(args.log)
    exit_note = [r for r in records if r["type"] == "notification" and r["id"] == "monitor:exit"]
    assert exit_note == []
    session_end = [r for r in records if r["type"] == "session_end"][0]
    assert session_end["reason"] == "signal" and session_end["signal"] == "SIGINT"


# ── HeadlessRenderer.on_notify_warning ──

def test_headless_on_notify_warning_prints_one_line(capsys):
    renderer = monitor.HeadlessRenderer("logs/test.jsonl", 3.0)
    renderer.on_notify_warning("notification failed (boom); monitoring continues, see log")
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 1
    assert "NOTIFY-WARN" in lines[0]
    assert "notification failed (boom); monitoring continues, see log" in lines[0]
