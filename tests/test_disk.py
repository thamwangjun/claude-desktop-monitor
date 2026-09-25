import os
import time

import monitor


def _poll_until(predicate, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def _drain_fsevents_backlog(collector, timeout=8.0):
    """Starting a watch on directories created moments earlier can replay a
    historical burst of structural events (macOS FSEvents "since now" has
    some slack for very recent history). Poll until two consecutive, quiet
    collects produce no dirty units, so a later targeted write isn't
    confused with this startup noise."""
    deadline = time.monotonic() + timeout
    quiet_streak = 0
    while time.monotonic() < deadline and quiet_streak < 2:
        collector.collect(time.monotonic())
        time.sleep(0.3)
        with collector._lock:
            dirty = bool(collector._dirty)
        quiet_streak = 0 if dirty else quiet_streak + 1
    collector.collect(time.monotonic())


# ── walk_size ──


def test_walk_size_sparse_file(tmp_path):
    path = tmp_path / "sparse.bin"
    with open(path, "wb") as fh:
        fh.truncate(1024**3)
    with open(path, "r+b") as fh:
        fh.write(b"x" * (1024**2))

    result = monitor.walk_size(tmp_path)

    assert result["present"] is True
    assert result["apparent"] == 1024**3
    assert 1024**2 <= result["allocated"] < 4 * 1024**2


def test_walk_size_hard_link_counted_once(tmp_path):
    original = tmp_path / "a.bin"
    original.write_bytes(b"x" * 4096)
    linked = tmp_path / "b.bin"
    os.link(original, linked)

    result = monitor.walk_size(tmp_path)

    assert result["files"] == 1
    assert result["apparent"] == 4096


def test_walk_size_absent_root(tmp_path):
    result = monitor.walk_size(tmp_path / "does-not-exist")
    assert result == {"present": False}


# ── DiskCollector: absence and appearance (D-6, V-3) ──


def test_disk_collector_absent_support_dir(tmp_path):
    support = tmp_path / "cdm-test"
    collector = monitor.DiskCollector(support_dir=str(support), full_rescan=300.0)
    try:
        sample = collector.collect(time.monotonic())
        assert sample["bundle"] == {"present": False}
        for entry in sample["paths"].values():
            assert entry == {"present": False}
    finally:
        collector.close()


def test_disk_collector_support_dir_appears(tmp_path):
    support = tmp_path / "cdm-test"
    collector = monitor.DiskCollector(support_dir=str(support), full_rescan=300.0)
    try:
        collector.collect(time.monotonic())

        bundle_dir = support / "vm_bundles" / "claudevm.bundle"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "rootfs.img").write_bytes(b"x" * 2048)

        def has_bundle():
            sample = collector.collect(time.monotonic())
            return sample["bundle"].get("present") is True

        assert _poll_until(has_bundle)
    finally:
        collector.close()


# ── DiskCollector: incremental re-walk only touches dirty units (D-5, T-2, T-3) ──


def test_disk_collector_only_dirty_unit_rewalked(tmp_path):
    support = tmp_path / "Claude"
    (support / "Cache").mkdir(parents=True)
    (support / "Cache" / "a").write_bytes(b"x" * 1024)
    (support / "IndexedDB").mkdir(parents=True)
    (support / "IndexedDB" / "b").write_bytes(b"y" * 1024)

    collector = monitor.DiskCollector(support_dir=str(support), full_rescan=300.0)
    try:
        collector.collect(time.monotonic())  # first poll: everything is new, walks both
        _drain_fsevents_backlog(collector)

        (support / "Cache" / "c").write_bytes(b"z" * 4096)

        def cache_refreshed():
            sample = collector.collect(time.monotonic())
            cache = sample["paths"]["cache"]
            return cache["present"] and cache["last_scan_elapsed"] < 0.5

        assert _poll_until(cache_refreshed)

        sample = collector.collect(time.monotonic())
        indexeddb = sample["paths"]["indexeddb"]
        assert indexeddb["present"] is True
        assert indexeddb["apparent"] == 1024
        assert indexeddb["last_scan_elapsed"] > 0.5
    finally:
        collector.close()


# ── VM bundle rows and partials (D-2, D-3, D-8) ──


def test_bundle_rows_and_other_sum(tmp_path):
    support = tmp_path / "Claude"
    bundle = support / "vm_bundles" / "claudevm.bundle"
    bundle.mkdir(parents=True)
    (bundle / "rootfs.img").write_bytes(b"r" * 10_000)
    (bundle / "sessiondata.img").write_bytes(b"s" * 3_000)
    (bundle / "initrd").write_bytes(b"i" * 500)
    (bundle / "vmlinuz").write_bytes(b"v" * 700)

    collector = monitor.DiskCollector(support_dir=str(support), full_rescan=300.0)
    try:
        sample = collector.collect(time.monotonic())
        rows = sample["bundle"]["rows"]
        assert rows["rootfs.img"]["apparent"] == 10_000
        assert rows["sessiondata.img"]["apparent"] == 3_000
        assert rows["other"]["apparent"] == 1_200
        assert rows["total"]["apparent"] == 14_200
    finally:
        collector.close()


def test_bundle_partial_age_reported(tmp_path):
    support = tmp_path / "Claude"
    bundle = support / "vm_bundles" / "claudevm.bundle"
    bundle.mkdir(parents=True)
    partial = bundle / "vmlinuz.zst.abc123.partial"
    partial.write_bytes(b"p" * 100)
    old_time = time.time() - 600
    os.utime(partial, (old_time, old_time))

    collector = monitor.DiskCollector(support_dir=str(support), full_rescan=300.0)
    try:
        sample = collector.collect(time.monotonic())
        partials = sample["bundle"]["partials"]
        assert len(partials) == 1
        assert partials[0]["age_s"] >= 590
        assert partials[0]["apparent"] == 100
    finally:
        collector.close()


def test_zst_present_toggles(tmp_path):
    support = tmp_path / "Claude"
    bundle = support / "vm_bundles" / "claudevm.bundle"
    bundle.mkdir(parents=True)
    zst = bundle / "rootfs.img.zst"
    zst.write_bytes(b"z" * 100)

    collector = monitor.DiskCollector(support_dir=str(support), full_rescan=300.0)
    try:
        sample = collector.collect(time.monotonic())
        assert sample["bundle"]["zst_present"] is True

        zst.unlink()
        sample = collector.collect(time.monotonic())
        assert sample["bundle"]["zst_present"] is False
    finally:
        collector.close()
