import monitor

APP_PREFIX = "/Applications/Claude.app/"
MAIN_EXE = "/Applications/Claude.app/Contents/MacOS/Claude"


# ── Role classification ──


def test_classify_role_main():
    assert monitor.classify_role(MAIN_EXE, [MAIN_EXE], MAIN_EXE, APP_PREFIX) == "main"


def test_classify_role_vm():
    exe = (
        "/System/Library/Frameworks/Virtualization.framework/Versions/A/XPCServices/"
        "com.apple.Virtualization.VirtualMachine.xpc/Contents/MacOS/com.apple.Virtualization.VirtualMachine"
    )
    assert monitor.classify_role(exe, [], MAIN_EXE, APP_PREFIX) == "vm"


def test_classify_role_crashpad():
    exe = f"{APP_PREFIX}Contents/Frameworks/Claude Framework.framework/chrome_crashpad_handler"
    assert monitor.classify_role(exe, [exe], MAIN_EXE, APP_PREFIX) == "crashpad"


def test_classify_role_renderer():
    exe = f"{APP_PREFIX}Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)"
    cmdline = [exe, "--type=renderer", "--no-sandbox"]
    assert monitor.classify_role(exe, cmdline, MAIN_EXE, APP_PREFIX) == "renderer"


def test_classify_role_gpu():
    exe = f"{APP_PREFIX}Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"
    cmdline = [exe, "--type=gpu-process"]
    assert monitor.classify_role(exe, cmdline, MAIN_EXE, APP_PREFIX) == "gpu"


def test_classify_role_utility_network():
    exe = f"{APP_PREFIX}Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"
    cmdline = [exe, "--type=utility", "--utility-sub-type=network.mojom.NetworkService"]
    assert monitor.classify_role(exe, cmdline, MAIN_EXE, APP_PREFIX) == "utility:network"


def test_classify_role_utility_node():
    exe = f"{APP_PREFIX}Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"
    cmdline = [exe, "--type=utility", "--utility-sub-type=node.mojom.NodeService"]
    assert monitor.classify_role(exe, cmdline, MAIN_EXE, APP_PREFIX) == "utility:node"


def test_classify_role_utility_unknown_without_subtype():
    exe = f"{APP_PREFIX}Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"
    cmdline = [exe, "--type=utility"]
    assert monitor.classify_role(exe, cmdline, MAIN_EXE, APP_PREFIX) == "utility:unknown"


def test_classify_role_xpc():
    exe = "/System/Library/CoreServices/MTLCompilerService"
    assert monitor.classify_role(exe, [exe], MAIN_EXE, APP_PREFIX) == "xpc"


def test_classify_role_xpc_under_usr():
    exe = "/usr/libexec/some_random_helper"
    assert monitor.classify_role(exe, [exe], MAIN_EXE, APP_PREFIX) == "xpc"


def test_classify_role_other_outside_known_paths():
    exe = "/opt/homebrew/bin/something"
    assert monitor.classify_role(exe, [exe], MAIN_EXE, APP_PREFIX) == "other"


# ── CLI group exclusion (P-2) ──


def test_is_cli_process_matches_claude_cli():
    assert monitor.is_cli_process("/opt/homebrew/bin/claude") is True


def test_is_cli_process_matches_mise_shim_exe():
    # mise/npm global installs on this machine shim the CLI as claude.exe.
    assert monitor.is_cli_process("/Users/x/.local/share/mise/installs/node/24/lib/node_modules/claude/bin/claude.exe") is True


def test_is_cli_process_excludes_zed_agent_sdk():
    exe = "/Users/dev/Library/Application Support/Zed/claude-agent-sdk/bin/claude"
    assert monitor.is_cli_process(exe) is False


def test_is_cli_process_excludes_non_matching_names():
    assert monitor.is_cli_process("/Applications/Claude Usage.app/Contents/MacOS/Claude Usage") is False
    assert monitor.is_cli_process(None) is False


# ── I/O delta rules ──


def test_io_delta_new_process_after_session_start_gets_full_counter():
    read_delta, write_delta = monitor.compute_io_deltas(
        None, None, 1000, 2000, create_time=100.0, session_start_epoch=50.0, first_seen=True,
    )
    assert (read_delta, write_delta) == (1000, 2000)


def test_io_delta_pre_existing_process_starts_at_zero():
    read_delta, write_delta = monitor.compute_io_deltas(
        None, None, 11_900_000_000, 500, create_time=10.0, session_start_epoch=50.0, first_seen=True,
    )
    assert (read_delta, write_delta) == (0, 0)


def test_io_delta_normal_increase():
    read_delta, write_delta = monitor.compute_io_deltas(
        1000, 2000, 1500, 2100, create_time=10.0, session_start_epoch=50.0, first_seen=False,
    )
    assert (read_delta, write_delta) == (500, 100)


def test_io_delta_counter_decrease_yields_zero():
    read_delta, write_delta = monitor.compute_io_deltas(
        1000, 2000, 900, 1800, create_time=10.0, session_start_epoch=50.0, first_seen=False,
    )
    assert (read_delta, write_delta) == (0, 0)


# ── Aggregation ──


def test_aggregate_processes_sums_fields():
    processes = [
        {"cpu_pct": 10.0, "footprint": 100, "rss": 200, "read_delta": 10, "write_delta": 20},
        {"cpu_pct": 5.0, "footprint": 50, "rss": 60, "read_delta": 5, "write_delta": 10},
    ]
    agg = monitor.aggregate_processes(processes, dt=2.0)
    assert agg["count"] == 2
    assert agg["cpu_pct"] == 15.0
    assert agg["footprint"] == 150
    assert agg["rss"] == 260
    assert agg["read_delta"] == 15
    assert agg["write_delta"] == 30
    assert agg["read_rate"] == 7.5
    assert agg["write_rate"] == 15.0


def test_aggregate_processes_ignores_none_values():
    processes = [
        {"cpu_pct": None, "footprint": None, "rss": 100, "read_delta": None, "write_delta": None},
    ]
    agg = monitor.aggregate_processes(processes, dt=1.0)
    assert agg["cpu_pct"] is None
    assert agg["footprint"] is None
    assert agg["rss"] == 100
    assert agg["read_rate"] is None
    assert agg["write_rate"] is None


def test_aggregate_processes_empty_list():
    agg = monitor.aggregate_processes([], dt=1.0)
    assert agg["count"] == 0
    assert agg["cpu_pct"] is None
    assert agg["read_rate"] is None


def test_aggregate_processes_no_dt_omits_rates():
    processes = [{"cpu_pct": 1.0, "footprint": 1, "rss": 1, "read_delta": 10, "write_delta": 10}]
    agg = monitor.aggregate_processes(processes, dt=None)
    assert agg["read_rate"] is None
    assert agg["write_rate"] is None


# ── fs_usage line parser (I-5) ──


def test_parse_fs_usage_line_write_call():
    line = (
        "13:45:01.123456  write        F=3   B=0x1000        "
        "/Users/x/Library/Application Support/Claude/vm_bundles/claudevm.bundle/sessiondata.img"
        "             0.000012 W  coworkd.1234\n"
    )
    result = monitor.parse_fs_usage_line(line)
    assert result is not None
    path, num_bytes = result
    assert path.endswith("sessiondata.img")
    assert num_bytes == 0x1000


def test_parse_fs_usage_line_wrdata_call():
    line = "13:45:02.000000  WrData[AT64]  D=0x2000  B=0x4000  /var/vm/swapfile0  0.000050 W  kernel_task.0\n"
    result = monitor.parse_fs_usage_line(line)
    assert result is not None
    assert result == ("/var/vm/swapfile0", 0x4000)


def test_parse_fs_usage_line_ignores_non_write_calls():
    line = "13:45:03.000000  open  F=5  /etc/hosts  0.000003  Claude.1234\n"
    assert monitor.parse_fs_usage_line(line) is None


def test_parse_fs_usage_line_unparsable_write_call():
    line = "13:45:04.000000  write   this line has no byte count or path\n"
    assert monitor.parse_fs_usage_line(line) is None


# ── FsUsageTracer window/aggregation (no live sudo/subprocess) ──


def test_fs_usage_tracer_snapshot_windows_and_ranks(monkeypatch):
    tracer = monitor.FsUsageTracer(window=60.0, top_n=2)
    tracer._events.extend(
        [
            (900.0, "/old", 999999),  # older than the window, dropped
            (1000.0, "/a", 100),
            (1000.0, "/b", 500),
            (1000.0, "/c", 10),
        ]
    )
    tracer._unparsed = 3
    snapshot = tracer.snapshot(now=1010.0)
    assert snapshot["unparsed"] == 3
    assert [row["path"] for row in snapshot["top"]] == ["/b", "/a"]
    assert snapshot["active"] is False
