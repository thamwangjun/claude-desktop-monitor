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


# ── notification_tier ──

def test_notification_tier_mapping():
    assert monitor.notification_tier("attr:vm?") == "warning"  # not notified, but still a plain warning tier
    for flag_id in ("cpu:total", "cpu:renderer", "mem:total", "wr:total", "swap:streak"):
        assert monitor.notification_tier(flag_id) == "warning"
    for flag_id in ("diag:foo.diag", "budget:2g", "budget:8g", "bundle:growth", "bundle:rate", "bundle:zst",
                    "partial:foo.partial", "monitor:exit"):
        assert monitor.notification_tier(flag_id) == "critical"
    assert monitor.notification_tier("future:unknown") == "warning"


# ── build_notification wording (§5) ──

def _event(flag_id, state="raised", value=1, threshold=1, message="fallback message", details=None):
    event = {"id": flag_id, "state": state, "metric": "m", "value": value, "threshold": threshold,
              "message": message}
    if details is not None:
        event["details"] = details
    return event


def test_build_notification_cpu_warming_and_normal():
    event = _event("cpu:renderer", threshold=30.0,
                    details={"role": "renderer", "window_s": 60.0, "avg": 62.0, "baseline": 8.0})
    n = monitor.build_notification(event)
    assert n.tier == "warning"
    assert n.subtitle == "High CPU — renderer"
    assert n.body == "62% of one core (60s avg) > 30%\nBaseline 8%. Idle climb? See TUI role rows"

    vm_event = _event("cpu:vm", threshold=30.0,
                       details={"role": "vm", "window_s": 60.0, "avg": 62.0, "baseline": None})
    n = monitor.build_notification(vm_event)
    assert n.body == ("62% of one core (60s avg) > 30%\n"
                       "Baseline warming. See ~/Library/Logs/Claude/cowork_vm_swift.log")


def test_build_notification_mem():
    event = _event("mem:total", details={"role": "total", "avg": 600 * 2**20, "baseline": 400 * 2**20,
                                          "growth_pct": 50.0})
    n = monitor.build_notification(event)
    assert n.subtitle == "Memory growth — Total Claude"
    assert n.body == ("5-min avg 600.00 MiB > baseline 400.00 MiB + 50%\n"
                       "Slow-leak pattern; press b in TUI to re-baseline")


def test_build_notification_wr():
    event = _event("wr:renderer", threshold=1_000_000, details={"role": "renderer", "avg": 2_000_000})
    n = monitor.build_notification(event)
    assert n.subtitle == "High disk writes — renderer"
    assert n.body == "1.91 MiB/s (60s avg) > 976.56 KiB/s\nCounts toward macOS 24h budget; --trace-io shows files"


def test_build_notification_swap_streak():
    event = _event("swap:streak", details={"streak": 3, "swap_used": 2**30})
    n = monitor.build_notification(event)
    assert n.tier == "warning"
    assert n.subtitle == "Sustained swapping"
    assert n.body == "Swap-outs up on 3 consecutive polls\nSwap used 1.00 GiB; check memory rows"


def test_build_notification_budget():
    event = _event("budget:2g", details={"tier_label": "2 GiB", "pct": 91.2, "written_24h": 2_000_000_000,
                                          "budget": 2 * 2**30})
    n = monitor.build_notification(event)
    assert n.tier == "critical"
    assert n.subtitle == "Write budget 91% — 2 GiB tier"
    assert n.body.startswith("Total Claude wrote")
    assert n.body.endswith("macOS files a disk-writes .diag report at 100%")


def test_build_notification_bundle_growth_and_rate():
    growth_event = _event("bundle:growth", threshold=2**30, details={"growth": 2 * 2**30})
    n = monitor.build_notification(growth_event)
    assert n.tier == "critical"
    assert n.subtitle == "VM bundle growth"
    assert n.body == "+2.00 GiB since start > 1.00 GiB\nrootfs.img is never trimmed; check sessiondata.img"

    rate_event = _event("bundle:rate", threshold=100 * 2**20, details={"rate": 200 * 2**20})
    n = monitor.build_notification(rate_event)
    assert n.subtitle == "VM bundle growing fast"
    assert n.body == "+200.00 MiB in 10 min > 100.00 MiB\nCheck ~/Library/Logs/Claude/ for downloads"


def test_build_notification_bundle_zst():
    disappeared = monitor.build_notification(_event("bundle:zst", state="event", details={"present": False}))
    assert disappeared.subtitle == "VM download cache removed"
    assert disappeared.body == "rootfs.img.zst disappeared\nReappeared = re-download; see ~/Library/Logs/Claude/"

    reappeared = monitor.build_notification(_event("bundle:zst", state="event", details={"present": True}))
    assert reappeared.subtitle == "VM download cache reappeared"
    assert reappeared.body.startswith("rootfs.img.zst reappeared")


def test_build_notification_partial():
    event = _event("partial:vm_bundles/foo.img.partial",
                    details={"path": "vm_bundles/foo.img.partial", "age_s": 400.0})
    n = monitor.build_notification(event)
    assert n.tier == "critical"
    assert n.subtitle == "Stalled VM download"
    assert n.body.startswith("foo.img.partial is")
    assert n.body.endswith("Possible re-download loop; see ~/Library/Logs/Claude/")


def test_build_notification_diag_disk_writes_and_other_event():
    disk_writes = _event("diag:Claude_2026-09-25-213433_nu.diag", state="event", details={
        "file": "/Library/Logs/DiagnosticReports/Claude_2026-09-25-213433_nu.diag",
        "event": "disk writes", "writes_mb": 2147.48, "writes_over_s": 1059.0,
        "limit_kbps": 24.86, "action": "none",
    })
    n = monitor.build_notification(disk_writes)
    assert n.tier == "critical"
    assert n.subtitle == "macOS diagnostic: disk writes"
    assert n.body.startswith("2147MB in 1059s (limit 24.86KB/s)")
    assert n.body.endswith("Claude_2026-09-25-213433_nu.diag")
    assert "See /Library/Logs/D" in n.body

    other = _event("diag:foo.diag", state="event", details={
        "file": "/Library/Logs/DiagnosticReports/foo.diag", "event": "some event",
        "writes_mb": None, "writes_over_s": None, "limit_kbps": None, "action": "kill",
    })
    n = monitor.build_notification(other)
    assert n.body.startswith("some event, action: kill")


def test_build_notification_falls_back_to_message_when_details_missing():
    event = _event("cpu:renderer", message="fallback text here")
    n = monitor.build_notification(event)
    assert n.subtitle == "cpu:renderer"
    assert n.body == "fallback text here"

    incomplete = _event("mem:total", message="incomplete mem message", details={"role": "total"})
    n = monitor.build_notification(incomplete)
    assert n.subtitle == "mem:total"
    assert n.body == "incomplete mem message"


def test_notification_record_shape():
    n = _n(flag_id="cpu:renderer", tier="warning")
    record = monitor.notification_record(n, "sent")
    assert record == {
        "type": "notification", "id": "cpu:renderer", "tier": "warning", "title": "Claude Monitor",
        "subtitle": "High CPU — renderer", "body": "line 1\nline 2", "status": "sent",
    }
    failed = monitor.notification_record(n, "failed", error="boom")
    assert failed["status"] == "failed" and failed["error"] == "boom"


# ── NotificationPolicy ──

class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, n):
        self.sent.append(n)


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_policy_cleared_and_vm_attr_not_sent():
    sender = FakeSender()
    policy = monitor.NotificationPolicy(sender, cooldown_s=300, clock=FakeClock())
    assert policy.handle(_event("cpu:total", state="cleared")) is None
    assert policy.handle(_event("attr:vm?", state="raised")) is None
    assert sender.sent == []


def test_policy_sends_raised_and_event_once_each():
    sender = FakeSender()
    policy = monitor.NotificationPolicy(sender, cooldown_s=300, clock=FakeClock())
    assert policy.handle(_event("cpu:total", state="raised")) is None
    assert policy.handle(_event("bundle:zst", state="event", details={"present": True})) is None
    assert len(sender.sent) == 2
    assert {n.flag_id for n in sender.sent} == {"cpu:total", "bundle:zst"}


def test_policy_two_different_flags_same_poll_both_send():
    sender = FakeSender()
    policy = monitor.NotificationPolicy(sender, cooldown_s=300, clock=FakeClock())
    policy.handle(_event("cpu:renderer", state="raised"))
    policy.handle(_event("cpu:total", state="raised"))
    assert {n.flag_id for n in sender.sent} == {"cpu:renderer", "cpu:total"}


def test_policy_cooldown_suppresses_then_allows_after_window():
    sender = FakeSender()
    clock = FakeClock(0.0)
    policy = monitor.NotificationPolicy(sender, cooldown_s=300, clock=clock)
    assert policy.handle(_event("cpu:total", state="raised")) is None
    assert len(sender.sent) == 1

    clock.t = 100.0
    record = policy.handle(_event("cpu:total", state="raised"))
    assert record is not None
    assert record["status"] == "suppressed_cooldown"
    assert record["id"] == "cpu:total"
    assert len(sender.sent) == 1

    clock.t = 301.0
    assert policy.handle(_event("cpu:total", state="raised")) is None
    assert len(sender.sent) == 2


def test_policy_diag_ids_are_independent_of_each_other():
    sender = FakeSender()
    clock = FakeClock(0.0)
    policy = monitor.NotificationPolicy(sender, cooldown_s=300, clock=clock)
    policy.handle(_event("diag:a.diag", state="event"))
    clock.t = 1.0
    assert policy.handle(_event("diag:b.diag", state="event")) is None
    assert len(sender.sent) == 2


def test_policy_zero_cooldown_sends_every_time():
    sender = FakeSender()
    clock = FakeClock(0.0)
    policy = monitor.NotificationPolicy(sender, cooldown_s=0, clock=clock)
    policy.handle(_event("cpu:total", state="raised"))
    clock.t = 0.001
    policy.handle(_event("cpu:total", state="raised"))
    assert len(sender.sent) == 2


def test_policy_reset_does_not_reset_cooldown():
    sender = FakeSender()
    clock = FakeClock(0.0)
    policy = monitor.NotificationPolicy(sender, cooldown_s=300, clock=clock)
    policy.handle(_event("cpu:total", state="raised"))
    # analyzer.reset() has no interaction with the policy object at all
    clock.t = 100.0
    record = policy.handle(_event("cpu:total", state="raised"))
    assert record["status"] == "suppressed_cooldown"


# ── startup diag reports don't notify (N-9) ──

def test_historic_diag_reports_never_become_events():
    args_ns = types.SimpleNamespace(
        interval=3.0, cpu_threshold=30.0, cpu_window=60.0, write_threshold=monitor.parse_size("1MB"),
        budget_warn=80.0, mem_growth=50.0, bundle_growth=monitor.parse_size("1GB"),
        bundle_rate=monitor.parse_size("100MB"),
    )
    analyzer = monitor.Analyzer(args_ns)
    sample = {
        "elapsed": 0.0, "total": {"cpu_pct": None, "footprint": None, "rss": None, "write_rate": None,
                                    "read_rate": None, "write_delta": None},
        "roles": {}, "paths": {}, "bundle": {"present": False},
        "swap": {"used": None, "swapins_bytes": None, "swapouts_bytes": None},
        "diag": {"new": [], "historic": [{"file": "/x/Claude_hist.diag", "event": "disk writes"}]},
        "processes": [],
    }
    _, events = analyzer.analyze(sample)
    assert not any(e["id"].startswith("diag:") for e in events)
