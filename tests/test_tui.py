import argparse

from rich.console import Console

import monitor


def make_args(**overrides):
    base = dict(
        interval=3.0,
        cpu_threshold=30.0,
        cpu_window=60.0,
        write_threshold=monitor.parse_size("1MB"),
        budget_warn=80.0,
        mem_growth=50.0,
        bundle_growth=monitor.parse_size("1GB"),
        bundle_rate=monitor.parse_size("100MB"),
        trace_io=False,
        include_cli=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def base_sample(elapsed, **overrides):
    sample = {
        "elapsed": elapsed,
        "claude": {"running": True, "main_pid": 111, "coalition_id": 4242, "app_path": "/Applications/Claude.app"},
        "total": {"count": 0, "cpu_pct": None, "footprint": None, "rss": None, "write_rate": None,
                   "read_rate": None, "write_delta": None},
        "roles": {},
        "paths": {},
        "bundle": {"present": False},
        "swap": {"total": None, "used": None, "free": None, "swapins_bytes": None, "swapouts_bytes": None},
        "diag": {"new": [], "historic": []},
        "processes": [],
    }
    sample.update(overrides)
    return sample


def render_to_text(renderer, sample, derived, active_flags, styles=False):
    console = Console(record=True, width=200, force_terminal=True)
    renderer._console = console
    renderable = renderer._build_renderable(sample, derived, active_flags)
    console.print(renderable)
    return console.export_text(styles=styles)


def make_renderer(args=None, notify_state="on"):
    args = args or make_args()
    renderer = monitor.TuiRenderer(args, "logs/test.jsonl", notify_state)
    renderer._start_wall = None
    import datetime
    renderer._start_wall = datetime.datetime.now().astimezone()
    return renderer


# ── formatting helpers ──


def test_fmt_delta_bytes_signed_zero_and_none():
    assert monitor._fmt_delta(None) == "n/a"
    assert monitor._fmt_delta(0) == "·"
    assert monitor._fmt_delta(1024 * 1024) == "+1.00 MiB"
    assert monitor._fmt_delta(-2048) == "−2.00 KiB"


def test_fmt_delta_pct():
    assert monitor._fmt_delta(5.5, "pct") == "+5.5%"
    assert monitor._fmt_delta(-1.2, "pct") == "−1.2%"


def test_fmt_cpu_and_rate_na():
    assert monitor._fmt_cpu(None) == "n/a"
    assert monitor._fmt_cpu(12.34) == "12.3%"
    assert monitor._fmt_rate(None) == "n/a"
    assert monitor._fmt_rate(2 * 2**20) == "2.00 MiB/s"


def test_fmt_baseline_warming():
    assert monitor._fmt_baseline("warming", "cpu") == "…"
    assert monitor._fmt_baseline(None, "mem") == "…"
    assert monitor._fmt_baseline(12.5, "cpu") == "12.5%"
    assert monitor._fmt_baseline(2**20, "mem") == "1.00 MiB"


def test_fmt_vs_baseline_none_is_warming():
    assert monitor._fmt_vs_baseline(None, "cpu") == "…"
    assert monitor._fmt_vs_baseline(5.0, "cpu") == "+5.0%"
    assert monitor._fmt_vs_baseline(0.5, "mem") == "+50.0%"


# ── rendering a synthetic sample ──


def _sample_with_process(role, attribution, pid=222):
    return base_sample(
        elapsed=3.0,
        total={"count": 1, "cpu_pct": 5.0, "footprint": 10 * 2**20, "rss": 10 * 2**20,
               "write_rate": 100.0, "read_rate": 0.0, "write_delta": 300.0},
        roles={role: {"count": 1, "cpu_pct": 5.0, "footprint": 10 * 2**20, "rss": 10 * 2**20,
                      "write_rate": 100.0, "read_rate": 0.0, "write_delta": 300.0}},
        processes=[{
            "pid": pid, "role": role, "exe": "/System/Library/foo/CSExattrCryptoService",
            "attribution": attribution, "cpu_pct": 5.0, "footprint": 10 * 2**20, "rss": 10 * 2**20,
            "disk_read": 0, "disk_written": 300, "read_delta": 0, "write_delta": 300,
        }],
        paths={"cache": {"present": False}},
    )


def test_role_names_and_absent_path_shown():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    sample = _sample_with_process("xpc", "coalition")
    derived, events = analyzer.analyze(sample)
    renderer = make_renderer(args)
    text = render_to_text(renderer, sample, derived, analyzer.active_flags)
    assert "xpc" in text
    assert "total" in text
    assert "absent" in text  # cache path not present


def test_flagged_row_is_styled_red():
    args = make_args(cpu_threshold=1.0, cpu_window=2.0, interval=1.0)
    analyzer = monitor.Analyzer(args)
    renderer = make_renderer(args)
    derived = None
    active = []
    for elapsed in (0.0, 1.0, 2.0, 3.0):
        sample = _sample_with_process("main", "coalition")
        sample["elapsed"] = elapsed
        derived, _events = analyzer.analyze(sample)
        active = analyzer.active_flags
    assert "cpu:total" in active
    styled = render_to_text(renderer, sample, derived, active, styles=True)
    assert "\x1b[31m" in styled  # red ANSI escape present once the flag is raised

    # Without the active flag, the same row is not styled red.
    plain_active = [f for f in active if f != "cpu:total"]
    unstyled = render_to_text(renderer, sample, derived, plain_active, styles=True)
    # crude check: fewer red escapes than the flagged render
    assert unstyled.count("\x1b[31m") < styled.count("\x1b[31m")


def test_per_pid_rows_toggle():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    renderer = make_renderer(args)
    sample = _sample_with_process("xpc", "name?", pid=333)
    derived, _events = analyzer.analyze(sample)
    active = analyzer.active_flags

    hidden_text = render_to_text(renderer, sample, derived, active)
    assert "333" not in hidden_text
    assert "CSExattrCryptoService" not in hidden_text

    renderer._show_pids = True
    shown_text = render_to_text(renderer, sample, derived, active)
    assert "333" in shown_text
    assert "CSExattrCryptoService" in shown_text
    assert "[?]" in shown_text  # attribution "name?" shown as "?"

    renderer._show_pids = False
    hidden_again = render_to_text(renderer, sample, derived, active)
    assert "333" not in hidden_again


# ── header: notify state (Plan 11) ──

def render_header_to_text(renderer, sample, styles=False):
    console = Console(record=True, width=200, force_terminal=True)
    renderer._console = console
    header = renderer._build_header(sample, hidden_rows=0)
    console.print(header)
    return console.export_text(styles=styles)


def test_header_shows_notify_on():
    renderer = make_renderer(notify_state="on")
    text = render_header_to_text(renderer, base_sample(0.0))
    assert "notify: on" in text


def test_header_shows_notify_off():
    renderer = make_renderer(notify_state="off")
    text = render_header_to_text(renderer, base_sample(0.0))
    assert "notify: off" in text
    assert "unavailable" not in text


def test_header_shows_notify_unavailable():
    renderer = make_renderer(notify_state="unavailable")
    text = render_header_to_text(renderer, base_sample(0.0))
    assert "notify: off (unavailable: see log)" in text


def test_header_shows_failing_after_notify_warning():
    renderer = make_renderer(notify_state="on")
    renderer.on_notify_warning("notification failed (boom); monitoring continues, see log")
    text = render_header_to_text(renderer, base_sample(0.0))
    assert "notify: on (failing: see log)" in text
    assert "notification failed (boom); monitoring continues, see log" in text


def test_header_warning_is_styled_yellow():
    renderer = make_renderer(notify_state="on")
    renderer.on_notify_warning("notification failed (boom); monitoring continues, see log")
    styled = render_header_to_text(renderer, base_sample(0.0), styles=True)
    assert "\x1b[33m" in styled  # yellow ANSI escape for the warning line


# ── dead-TTY tolerance (Plan 11, N-16: a closed tmux pane / SIGHUP) ──

class _RaisingLive:
    def update(self, renderable, refresh=True):
        raise OSError(5, "Input/output error")

    def stop(self):
        raise OSError(5, "Input/output error")


def test_render_tolerates_dead_tty_oserror():
    renderer = make_renderer()
    renderer._live = _RaisingLive()
    sample = base_sample(0.0)
    # Must not raise even though the underlying Live/console write fails.
    renderer.render(sample, {}, [])


def test_stop_tolerates_dead_tty_oserror():
    renderer = make_renderer()
    renderer._live = _RaisingLive()
    renderer._is_tty_stdin = False
    # Must not raise even though Live.stop() fails on a dead terminal.
    renderer.stop()
