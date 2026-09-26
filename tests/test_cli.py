import pytest

from claude_desktop_monitor import monitor


def test_parse_size_plain_bytes():
    assert monitor.parse_size("1024") == 1024


def test_parse_size_decimal_suffixes():
    assert monitor.parse_size("1KB") == 1_000
    assert monitor.parse_size("1MB") == 1_000_000
    assert monitor.parse_size("1GB") == 1_000_000_000


def test_parse_size_binary_suffixes():
    assert monitor.parse_size("1KiB") == 1024
    assert monitor.parse_size("1MiB") == 1024**2
    assert monitor.parse_size("1GiB") == 1024**3


def test_parse_size_case_insensitive():
    assert monitor.parse_size("1mib") == 1024**2
    assert monitor.parse_size("1Gb") == 1_000_000_000


def test_parse_size_rate_suffix():
    assert monitor.parse_size("1MB/s") == 1_000_000
    assert monitor.parse_size("100MiB/s") == 100 * 1024**2


def test_parse_size_fractional():
    assert monitor.parse_size("1.5GB") == 1_500_000_000


def test_parse_size_invalid():
    with pytest.raises(ValueError):
        monitor.parse_size("not a size")
    with pytest.raises(ValueError):
        monitor.parse_size("5XB")


def test_interval_default():
    args = monitor.parse_args(["--no-tui"])
    assert args.interval == 3.0


def test_interval_within_bounds():
    args = monitor.parse_args(["--interval", "2"])
    assert args.interval == 2.0
    args = monitor.parse_args(["--interval", "5"])
    assert args.interval == 5.0


def test_interval_below_bounds_rejected():
    with pytest.raises(SystemExit):
        monitor.parse_args(["--interval", "1"])


def test_interval_above_bounds_rejected():
    with pytest.raises(SystemExit):
        monitor.parse_args(["--interval", "6"])


def test_default_log_path_pattern():
    args = monitor.parse_args([])
    assert args.log.startswith("logs/monitor-")
    assert args.log.endswith(".jsonl")


def test_custom_log_path():
    args = monitor.parse_args(["--log", "/tmp/custom.jsonl"])
    assert args.log == "/tmp/custom.jsonl"


def test_defaults():
    args = monitor.parse_args([])
    assert args.full_rescan == 300.0
    assert args.cpu_threshold == 30.0
    assert args.cpu_window == 60.0
    assert args.write_threshold == monitor.parse_size("1MB")
    assert args.budget_warn == 80.0
    assert args.mem_growth == 50.0
    assert args.bundle_growth == monitor.parse_size("1GB")
    assert args.bundle_rate == monitor.parse_size("100MB")
    assert args.no_tui is False
    assert args.include_cli is False
    assert args.trace_io is False
    assert args.support_dir == monitor.os.path.expanduser(monitor.DEFAULT_SUPPORT_DIR)


def test_cpu_window_must_be_at_least_interval():
    with pytest.raises(SystemExit):
        monitor.parse_args(["--interval", "5", "--cpu-window", "3"])


def test_notify_defaults():
    args = monitor.parse_args([])
    assert args.no_notify is False
    assert args.notify_cooldown == 300.0


def test_no_notify_flag_parses():
    args = monitor.parse_args(["--no-notify"])
    assert args.no_notify is True


def test_notify_cooldown_custom_value():
    args = monitor.parse_args(["--notify-cooldown", "0"])
    assert args.notify_cooldown == 0.0


def test_notify_cooldown_negative_rejected():
    with pytest.raises(SystemExit):
        monitor.parse_args(["--notify-cooldown", "-1"])
