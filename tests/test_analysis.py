import argparse

from claude_desktop_monitor import monitor


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
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def base_sample(elapsed, **overrides):
    sample = {
        "elapsed": elapsed,
        "total": {"cpu_pct": None, "footprint": None, "rss": None, "write_rate": None, "read_rate": None,
                   "write_delta": None},
        "roles": {},
        "paths": {},
        "bundle": {"present": False},
        "swap": {"used": None, "swapins_bytes": None, "swapouts_bytes": None},
        "diag": {"new": [], "historic": []},
        "processes": [],
    }
    sample.update(overrides)
    return sample


def raised_ids(events):
    return {e["id"] for e in events if e["state"] == "raised"}


def cleared_ids(events):
    return {e["id"] for e in events if e["state"] == "cleared"}


def by_id(events, flag_id, state="raised"):
    matches = [e for e in events if e["id"] == flag_id and e["state"] == state]
    assert matches, f"no {state} event for {flag_id}"
    return matches[-1]


# ── TimeWindow ──


def test_time_window_full_semantics():
    w = monitor.TimeWindow(seconds=10, interval=2)
    for t in (0, 2, 4, 6):
        w.add(t, 1.0)
    assert w.full is False  # span 6 < 10-2=8
    w.add(8, 1.0)
    assert w.full is True  # span 8 >= 8


def test_time_window_mean_and_eviction():
    w = monitor.TimeWindow(seconds=5, interval=1)
    for t, v in [(0, 10), (1, 10), (2, 10), (6, 20)]:
        w.add(t, v)
    # entries with t - entry_t > 5 are evicted: (0,10) drops, (1,10)/(2,10)/(6,20) remain
    assert w.mean() == (10 + 10 + 20) / 3


# ── CPU: window full / raise / clear hysteresis ──


def test_baseline_not_set_until_window_full():
    args = make_args(cpu_window=4, interval=1)
    analyzer = monitor.Analyzer(args)
    last_derived = None
    for i in range(3):
        sample = base_sample(elapsed=i, total={"cpu_pct": 5.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        last_derived, _ = analyzer.analyze(sample)
    assert last_derived["cpu:total"]["baseline"] == "warming"


def test_cpu_raise_and_clear_with_hysteresis():
    args = make_args(cpu_window=4, interval=1, cpu_threshold=30.0)
    analyzer = monitor.Analyzer(args)
    t = 0
    all_events = []
    # fill the window with a low value first so it becomes "full"
    for _ in range(5):
        sample = base_sample(elapsed=t, total={"cpu_pct": 5.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" not in raised_ids(all_events)

    # push the average above threshold
    for _ in range(6):
        sample = base_sample(elapsed=t, total={"cpu_pct": 50.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" in raised_ids(all_events)
    details = by_id(all_events, "cpu:total")["details"]
    assert details["role"] == "total" and details["window_s"] == 4
    assert details["avg"] > 30.0 and details["baseline"] == 5.0

    # no flap: a single sample at 29% (just under the 30% threshold) must not clear
    # since clear only happens below 0.9*30=27
    sample = base_sample(elapsed=t, total={"cpu_pct": 29.0, "footprint": None, "rss": None,
                                             "write_rate": None, "read_rate": None, "write_delta": None})
    _, events = analyzer.analyze(sample)
    assert "cpu:total" not in cleared_ids(events)
    t += 1

    # drive the average down below the clear hysteresis
    for _ in range(8):
        sample = base_sample(elapsed=t, total={"cpu_pct": 1.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" in cleared_ids(all_events)


def test_cpu_default_threshold_raise_and_clear():
    threshold = monitor.parse_args([]).cpu_threshold
    args = make_args(cpu_window=4, interval=1, cpu_threshold=threshold)
    analyzer = monitor.Analyzer(args)
    t = 0
    all_events = []
    # fill the window with a low value first so it becomes "full"
    for _ in range(5):
        sample = base_sample(elapsed=t, total={"cpu_pct": 5.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" not in raised_ids(all_events)

    # steady 89%: below the 90% default, must not raise
    for _ in range(6):
        sample = base_sample(elapsed=t, total={"cpu_pct": 89.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" not in raised_ids(all_events)

    # steady 91%: above the 90% default, must raise
    for _ in range(6):
        sample = base_sample(elapsed=t, total={"cpu_pct": 91.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" in raised_ids(all_events)
    details = by_id(all_events, "cpu:total")["details"]
    assert details["avg"] > 90.0

    # steady 82% for a full window and more: average stays above 81, must not clear
    for _ in range(6):
        sample = base_sample(elapsed=t, total={"cpu_pct": 82.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" not in cleared_ids(all_events)

    # drive the average down below the clear hysteresis (81)
    for _ in range(8):
        sample = base_sample(elapsed=t, total={"cpu_pct": 80.0, "footprint": None, "rss": None,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        all_events += events
        t += 1
    assert "cpu:total" in cleared_ids(all_events)


# ── Memory growth with 32 MiB floor ──


def test_memory_growth_flag_with_baseline_floor():
    args = make_args(interval=50.0, mem_growth=50.0)  # window fixed at 300s; large interval fills it fast
    analyzer = monitor.Analyzer(args)
    t = 0.0
    small = 1 * 2**20  # 1 MiB baseline: below the 32 MiB floor
    events_all = []
    for _ in range(7):
        sample = base_sample(elapsed=t, total={"cpu_pct": None, "footprint": small, "rss": small,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        events_all += events
        t += args.interval
    # even though a later huge spike would exceed 50% growth, the floor should suppress it
    sample = base_sample(elapsed=t, total={"cpu_pct": None, "footprint": small * 10, "rss": small * 10,
                                             "write_rate": None, "read_rate": None, "write_delta": None})
    _, events = analyzer.analyze(sample)
    assert "mem:total" not in raised_ids(events)


def test_memory_growth_flag_above_floor():
    args = make_args(interval=50.0, mem_growth=50.0)
    analyzer = monitor.Analyzer(args)
    t = 0.0
    baseline_val = 100 * 2**20  # 100 MiB, above the 32 MiB floor
    events_all = []
    for _ in range(7):
        sample = base_sample(elapsed=t, total={"cpu_pct": None, "footprint": baseline_val, "rss": baseline_val,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        events_all += events
        t += args.interval
    grown = baseline_val * 2  # +100% > 50% threshold
    for _ in range(7):
        sample = base_sample(elapsed=t, total={"cpu_pct": None, "footprint": grown, "rss": grown,
                                                 "write_rate": None, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        events_all += events
        t += args.interval
    assert "mem:total" in raised_ids(events_all)
    details = by_id(events_all, "mem:total")["details"]
    assert details["role"] == "total" and details["baseline"] == baseline_val
    assert details["avg"] > baseline_val * 1.5 and details["growth_pct"] == 50.0


# ── Write-rate flag ──


def test_write_rate_flag():
    args = make_args(interval=20.0, write_threshold=monitor.parse_size("1MB"))
    analyzer = monitor.Analyzer(args)
    t = 0.0
    events_all = []
    for _ in range(4):
        sample = base_sample(elapsed=t, total={"cpu_pct": None, "footprint": None, "rss": None,
                                                 "write_rate": 5 * 10**6, "read_rate": None, "write_delta": None})
        _, events = analyzer.analyze(sample)
        events_all += events
        t += args.interval
    assert "wr:total" in raised_ids(events_all)
    details = by_id(events_all, "wr:total")["details"]
    assert details == {"role": "total", "avg": 5 * 10**6}


# ── 24h write budget ──


def test_write_budget_percentages_across_buckets():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    # one poll per (simulated) minute, each writing 1 MiB, for 10 minutes
    derived = None
    for minute in range(10):
        sample = base_sample(elapsed=minute * 60.0,
                              total={"cpu_pct": None, "footprint": None, "rss": None,
                                     "write_rate": None, "read_rate": None, "write_delta": 2**20})
        derived, _ = analyzer.analyze(sample)
    budget = derived["write_budget"]
    assert budget["bytes"] == 10 * 2**20
    assert budget["pct_2g"] == (10 * 2**20) / (2 * 2**30) * 100
    assert budget["pct_8g"] == (10 * 2**20) / (8 * 2**30) * 100
    assert budget["window_s"] == 9 * 60.0  # capped at session length


def test_write_budget_evicts_entries_past_24h():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    sample = base_sample(elapsed=0.0, total={"cpu_pct": None, "footprint": None, "rss": None,
                                               "write_rate": None, "read_rate": None, "write_delta": 5 * 2**30})
    analyzer.analyze(sample)
    sample = base_sample(elapsed=90000.0, total={"cpu_pct": None, "footprint": None, "rss": None,
                                                   "write_rate": None, "read_rate": None, "write_delta": 0})
    derived, _ = analyzer.analyze(sample)
    assert derived["write_budget"]["bytes"] == 0
    assert derived["write_budget"]["window_s"] == 86400.0


def test_budget_flag_raises_and_clears():
    args = make_args(budget_warn=80.0)
    analyzer = monitor.Analyzer(args)
    big = int(0.85 * 2 * 2**30)
    sample = base_sample(elapsed=0.0, total={"cpu_pct": None, "footprint": None, "rss": None,
                                               "write_rate": None, "read_rate": None, "write_delta": big})
    _, events = analyzer.analyze(sample)
    assert "budget:2g" in raised_ids(events)
    details = by_id(events, "budget:2g")["details"]
    assert details["tier_label"] == "2 GiB"
    assert details["written_24h"] == big and details["budget"] == 2 * 2**30
    assert details["pct"] == big / (2 * 2**30) * 100

    sample = base_sample(elapsed=90000.0, total={"cpu_pct": None, "footprint": None, "rss": None,
                                                   "write_rate": None, "read_rate": None, "write_delta": 0})
    _, events = analyzer.analyze(sample)
    assert "budget:2g" in cleared_ids(events)


# ── Bundle growth / rate / zst / partials ──


def _bundle(total_alloc, zst_present=True, partials=None, total_app=None):
    return {
        "present": True,
        "rows": {
            "rootfs.img": {"allocated": total_alloc, "apparent": total_app or total_alloc},
            "sessiondata.img": {"allocated": 0, "apparent": 0},
            "other": {"allocated": 0, "apparent": 0},
            "total": {"allocated": total_alloc, "apparent": total_app or total_alloc},
        },
        "zst_present": zst_present,
        "partials": partials or [],
    }


def test_bundle_growth_from_absent_start():
    args = make_args(bundle_growth=1_000_000)
    analyzer = monitor.Analyzer(args)
    # bundle absent on the very first poll
    sample = base_sample(elapsed=0.0, bundle={"present": False})
    _, events = analyzer.analyze(sample)
    assert "bundle:growth" not in raised_ids(events)

    # provisioning: bundle appears with a large allocation -> growth measured from 0
    sample = base_sample(elapsed=3.0, bundle=_bundle(5_000_000))
    _, events = analyzer.analyze(sample)
    assert "bundle:growth" in raised_ids(events)
    assert by_id(events, "bundle:growth")["details"] == {"growth": 5_000_000.0}


def test_bundle_growth_present_at_start_uses_first_value_as_baseline():
    args = make_args(bundle_growth=1_000_000)
    analyzer = monitor.Analyzer(args)
    sample = base_sample(elapsed=0.0, bundle=_bundle(10_000_000))
    _, events = analyzer.analyze(sample)
    assert "bundle:growth" not in raised_ids(events)  # no growth yet, this *is* the baseline

    sample = base_sample(elapsed=3.0, bundle=_bundle(10_500_000))
    _, events = analyzer.analyze(sample)
    assert "bundle:growth" not in raised_ids(events)  # 500KB growth < 1MB threshold


def test_bundle_rate_over_window():
    args = make_args(bundle_rate=1_000_000, interval=100.0)
    analyzer = monitor.Analyzer(args)
    sample = base_sample(elapsed=0.0, bundle=_bundle(1_000_000))
    analyzer.analyze(sample)
    sample = base_sample(elapsed=100.0, bundle=_bundle(3_000_000))
    _, events = analyzer.analyze(sample)
    assert "bundle:rate" in raised_ids(events)
    assert by_id(events, "bundle:rate")["details"] == {"rate": 2_000_000.0}


def test_bundle_zst_toggle_event():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    sample = base_sample(elapsed=0.0, bundle=_bundle(1_000_000, zst_present=True))
    _, events = analyzer.analyze(sample)
    assert not any(e["id"] == "bundle:zst" for e in events)  # first sighting: no event

    sample = base_sample(elapsed=3.0, bundle=_bundle(1_000_000, zst_present=False))
    _, events = analyzer.analyze(sample)
    zst_events = [e for e in events if e["id"] == "bundle:zst"]
    assert len(zst_events) == 1
    assert zst_events[0]["state"] == "event"
    assert zst_events[0]["details"] == {"present": False}


def test_partial_ageing_raises_and_clears():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    partials = [{"path": "vm_bundles/foo.img.partial", "age_s": 400.0, "apparent": 1, "allocated": 1}]
    sample = base_sample(elapsed=0.0, bundle=_bundle(1_000_000, partials=partials))
    _, events = analyzer.analyze(sample)
    assert "partial:vm_bundles/foo.img.partial" in raised_ids(events)
    details = by_id(events, "partial:vm_bundles/foo.img.partial")["details"]
    assert details == {"path": "vm_bundles/foo.img.partial", "age_s": 400.0}

    sample = base_sample(elapsed=3.0, bundle=_bundle(1_000_000, partials=[]))
    _, events = analyzer.analyze(sample)
    assert "partial:vm_bundles/foo.img.partial" in cleared_ids(events)


# ── Swap streak ──


def test_swap_streak_raises_after_three_increases_and_clears_on_flat_poll():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    events_all = []
    values = [100, 200, 300, 400]  # three consecutive increases
    for i, v in enumerate(values):
        sample = base_sample(elapsed=float(i), swap={"used": 42, "swapins_bytes": None, "swapouts_bytes": v})
        _, events = analyzer.analyze(sample)
        events_all += events
    assert "swap:streak" in raised_ids(events_all)
    details = by_id(events_all, "swap:streak")["details"]
    assert details == {"streak": 3, "swap_used": 42}

    sample = base_sample(elapsed=4.0, swap={"used": None, "swapins_bytes": None, "swapouts_bytes": 400})
    _, events = analyzer.analyze(sample)
    assert "swap:streak" in cleared_ids(events)


# ── VM attribution flag ──


def test_attribution_unresolved_flag():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    sample = base_sample(elapsed=0.0, processes=[{"pid": 1, "attribution": "name?"}])
    _, events = analyzer.analyze(sample)
    assert "attr:vm?" in raised_ids(events)

    sample = base_sample(elapsed=3.0, processes=[{"pid": 1, "attribution": "coalition"}])
    _, events = analyzer.analyze(sample)
    assert "attr:vm?" in cleared_ids(events)


# ── Diagnostic report event ──


def test_diag_new_report_emits_event():
    args = make_args()
    analyzer = monitor.Analyzer(args)
    report = {
        "file": "/Library/Logs/DiagnosticReports/Claude_2026-09-25-213433_nu.diag",
        "event": "disk writes", "action": "none", "writes_mb": 2147.48,
        "writes_over_s": 1059.0, "limit_kbps": 24.86,
    }
    sample = base_sample(elapsed=0.0, diag={"new": [report], "historic": []})
    _, events = analyzer.analyze(sample)
    diag_events = [e for e in events if e["id"].startswith("diag:")]
    assert len(diag_events) == 1
    assert diag_events[0]["state"] == "event"
    assert diag_events[0]["id"] == "diag:Claude_2026-09-25-213433_nu.diag"
    details = diag_events[0]["details"]
    assert details == {
        "file": "/Library/Logs/DiagnosticReports/Claude_2026-09-25-213433_nu.diag",
        "event": "disk writes", "writes_mb": 2147.48, "writes_over_s": 1059.0,
        "limit_kbps": 24.86, "action": "none",
    }


# ── Role appearing late ──


def test_role_appearing_late_starts_d_start_from_first_sighting():
    args = make_args(interval=1.0, cpu_window=4.0)
    analyzer = monitor.Analyzer(args)
    for i in range(3):
        sample = base_sample(elapsed=float(i))
        derived, _ = analyzer.analyze(sample)
    assert "cpu:renderer" not in derived  # role hasn't appeared yet

    sample = base_sample(elapsed=3.0, roles={"renderer": {"cpu_pct": 12.0, "footprint": None, "rss": None,
                                                            "write_rate": None, "read_rate": None}})
    derived, _ = analyzer.analyze(sample)
    assert derived["cpu:renderer"]["d_start"] == 0.0
    assert derived["cpu:renderer"]["cur"] == 12.0

    sample = base_sample(elapsed=4.0, roles={"renderer": {"cpu_pct": 20.0, "footprint": None, "rss": None,
                                                            "write_rate": None, "read_rate": None}})
    derived, _ = analyzer.analyze(sample)
    assert derived["cpu:renderer"]["d_start"] == 8.0
    assert derived["cpu:renderer"]["d_poll"] == 8.0


# ── reset() ──


def test_reset_clears_baseline_and_peak_but_not_d_start_or_flags():
    # a high threshold keeps the cpu:* flag out of this test entirely
    args = make_args(cpu_window=4.0, interval=1.0, cpu_threshold=1000.0)
    analyzer = monitor.Analyzer(args)
    t = 0

    def cpu_sample(value):
        return base_sample(elapsed=t, total={"cpu_pct": value, "footprint": None, "rss": None,
                                               "write_rate": None, "read_rate": None, "write_delta": None},
                            processes=[{"pid": 1, "attribution": "name?"}])

    # fill the window with a low value so the rolling-min baseline settles low
    derived = None
    for _ in range(5):
        derived, _ = analyzer.analyze(cpu_sample(10.0))
        t += 1
    assert derived["cpu:total"]["baseline"] == 10.0
    assert "attr:vm?" in analyzer.active_flags  # unrelated state flag, raised independently of cpu

    # push the average up; the rolling-min baseline stays pinned at the old low value
    for _ in range(5):
        derived, _ = analyzer.analyze(cpu_sample(90.0))
        t += 1
    assert derived["cpu:total"]["baseline"] == 10.0
    assert derived["cpu:total"]["peak"] == 90.0
    assert derived["cpu:total"]["d_start"] == 80.0

    analyzer.reset()

    # active flags and d_start references survive the reset
    assert "attr:vm?" in analyzer.active_flags

    derived, _ = analyzer.analyze(cpu_sample(90.0))
    assert derived["cpu:total"]["d_start"] == 80.0  # first_value unaffected by reset
    assert derived["cpu:total"]["baseline"] == 90.0  # rolling-min baseline forgotten, rebuilt fresh
    assert derived["cpu:total"]["peak"] == 90.0  # peak rebuilt from post-reset values
