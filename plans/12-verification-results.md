# Plan 12: v2 verification results

Machine: Apple M5 (Mac17,4), 10 CPUs, 32 GB RAM, macOS 26.7 (25G229). Claude Desktop 2.9939.2. Homebrew `terminal-notifier` 3.1.0 on PATH. Date: 2026-09-26/27 (V-12 crossed midnight).

| Check | Result |
|---|---|
| V-7 | **Pass** — `uv run pytest`: 171 passed, 1 warning (unrelated pre-existing `os.fork()` `DeprecationWarning` in `test_native.py`), 3.38s. |
| V-8 | **Pass** — see below. |
| V-9 | **Pass** — see below. |
| V-10 | **Pass** — see below. |
| V-11 | **Pass** — see below. |
| V-12 | **Pass** — see below. |

## V-8 — `--notify-test`

```
uv run monitor.py --notify-test
```

```
warning: sent
critical: sent
A successful send cannot prove the banner was shown. If nothing appeared, check System Settings → Notifications → terminal-notifier, and any Focus mode.
```

Exit code 0. User confirmed both banners appeared with the Claude-logo thumbnail: warning tier played Glass, critical tier played Basso. **Pass.**

## V-9 — live induced flag via CPU spike, wording, cooldown

Idle Total CPU measured over 24 s headless (`/tmp/idle-check.jsonl`): avg 24.1%, max 35.9% (noisier than the historical 0.6%/2.4% baseline from Plan 10, likely transient UI activity — not investigated further, out of scope). Used max-observed-idle + 5 ≈ 41% as the threshold to avoid false positives:

```
uv run monitor.py --no-tui --log /tmp/v9.jsonl --cpu-threshold 41 --cpu-window 6 --notify-cooldown 120
```

User spiked Claude Desktop's CPU three times. Log (`flag`/`notification` records only):

```json
{"type": "flag", "id": "cpu:total", "state": "raised", "value": 45.2, "threshold": 41.0, "details": {"role": "total", "window_s": 6.0, "avg": 45.2, "baseline": 16.35}, "elapsed": 18.146}
{"type": "notification", "id": "cpu:total", "tier": "warning", "subtitle": "High CPU — Total Claude", "body": "45% of one core (6s avg) > 41%\nBaseline 16%. Idle climb? See TUI role rows", "status": "sent", "elapsed": 21.136}
{"type": "flag", "id": "cpu:total", "state": "cleared", "elapsed": 24.144}
{"type": "flag", "id": "cpu:total", "state": "raised", "value": 51.4, "elapsed": 138.151}
{"type": "notification", "id": "cpu:total", "status": "sent", "elapsed": 141.15}
{"type": "flag", "id": "cpu:total", "state": "cleared", "elapsed": 144.133}
{"type": "flag", "id": "cpu:total", "state": "raised", "value": 59.0, "elapsed": 174.156}
{"type": "notification", "id": "cpu:total", "status": "suppressed_cooldown", "elapsed": 174.156}
{"type": "flag", "id": "cpu:renderer", "state": "raised", "value": 55.8, "elapsed": 180.149}
{"type": "notification", "id": "cpu:renderer", "status": "sent", "elapsed": 183.139}
{"type": "flag", "id": "cpu:total", "state": "cleared", "elapsed": 186.135}
{"type": "flag", "id": "cpu:renderer", "state": "cleared", "elapsed": 186.136}
```

Wording matched §5 exactly (subtitle, avg/window/threshold, baseline, hint). The second `cpu:total` raise landed 120.005 s after the first — outside the 120 s cooldown — and correctly sent again rather than being suppressed. The third raise, 33 s later, was correctly `suppressed_cooldown` (no banner). `cpu:renderer`, a different flag ID raised in the same window, notified independently of `cpu:total`'s cooldown (N-7). The user confirmed exactly 3 banners were seen, matching the 3 `sent` records, and no banner for the suppressed one. **Pass.**

## V-10 — unexpected exit vs. clean stop

**Clean stop (Ctrl-C / SIGINT)**, ending the V-9 run above:

```json
{"type": "session_end", "reason": "signal", "signal": "SIGINT", "elapsed": 321.019}
```

No `notification` record, no banner (implicit — none of the `notification` records above have id `monitor:exit`). **Pass.**

**SIGTERM**, headless:

```
nohup uv run monitor.py --no-tui --log /tmp/v10-sigterm.jsonl & 
kill -TERM <pid>   # after 5 s
```

```json
{"type": "notification", "id": "monitor:exit", "tier": "critical", "subtitle": "Monitor stopped unexpectedly", "body": "SIGTERM\nAlerts have stopped. Log: /tmp/v10-sigterm.jsonl", "status": "dispatched", "elapsed": 6.023}
{"type": "session_end", "reason": "signal", "signal": "SIGTERM", "elapsed": 6.023}
```

User confirmed the critical (Basso) banner appeared. Log complete (no truncation). **Pass.**

**SIGHUP**, TUI in a tmux pane, pane killed:

```
tmux new-session -d -s v10hup ... uv run monitor.py --log /tmp/v10-sighup2.jsonl
tmux kill-session -t v10hup2   # after ~6 s
```

```json
{"type": "notification", "id": "monitor:exit", "tier": "critical", "body": "SIGHUP\nAlerts have stopped. Log: /tmp/v10-sighup2.jsonl", "status": "dispatched", "elapsed": 11.202}
{"type": "session_end", "reason": "signal", "signal": "SIGHUP", "elapsed": 11.202}
```

Run twice at the user's request (once to confirm the timing, once to re-observe). Both times the user confirmed the critical banner appeared and the terminal was left undamaged after the pane closed. **Pass.**

## V-11 — `--no-notify`

```
uv run monitor.py --no-tui --log /tmp/v11.jsonl --no-notify --cpu-threshold 41 --cpu-window 6
```

`session_start.config.notify` was `false`. User spiked CPU; the flag raised and cleared normally:

```json
{"type": "flag", "id": "cpu:total", "state": "raised", "value": 52.4, "elapsed": 375.133}
{"type": "flag", "id": "cpu:renderer", "state": "raised", "value": 60.75, "elapsed": 381.152}
{"type": "flag", "id": "cpu:total", "state": "cleared", "elapsed": 399.122}
{"type": "flag", "id": "cpu:renderer", "state": "cleared", "elapsed": 399.122}
```

Zero `notification` records in the whole log. User confirmed no banner appeared. Separately started the TUI with `--no-notify` in a tmux pane; header showed `notify: off`. **Pass.**

## V-12 — cadence unaffected by sending (N-3)

```
uv run monitor.py --no-tui --log /tmp/v12.jsonl --cpu-threshold 41 --cpu-window 6 --notify-cooldown 0
```

Run for 675 s (11.25 min, exceeding the 10-min minimum) while the user repeatedly spiked and released Claude Desktop's CPU, then stopped with SIGINT. Checked with a short Python script over the log:

```python
import json
samples, notifs = [], []
for line in open("/tmp/v12.jsonl"):
    r = json.loads(line)
    if r["type"] == "sample":
        samples.append(r["elapsed"])
    elif r["type"] == "notification":
        notifs.append(r)
bad = [(a, b, b - a) for a, b in zip(samples, samples[1:]) if abs((b - a) - 3.0) > 0.5]
print(len(samples), "samples,", len(bad), "bad steps")
print(len(notifs), "notifications,", sum(1 for n in notifs if n["status"] == "sent"), "sent")
```

Result: 225 samples, **0** poll steps outside `3.0 s ± 0.5 s`; 20 `notification` records, all `sent` (≥ 10 required). Clean shutdown: `{"type": "session_end", "reason": "signal", "signal": "SIGINT"}`. **Pass.**

## Defects found

None. All of plans 09–11's implementation held up under live verification with no code changes required in this plan.
