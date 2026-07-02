"""Offline tests for monitor_loop resilience: supervisor, safe Telegram send,
disk error logging, and heartbeat/health (no network).

Covers the four fixes after the 3-day freeze:
  1. supervisor restarts monitor_loop on any exception (never dies forever)
  2. a failing bot.send_message (Telegram down) does NOT kill the loop
  3. errors land in error_log.jsonl on disk, independent of Telegram
  4. heartbeat/health flags a stale tick (older than the 10-min threshold)

Run: python tests/test_loop_resilience.py
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.monitor as m
import app.error_log as el

PASS = 0
FAIL = 0

TMP = Path(os.environ.get("SCRATCH", "/tmp")) / "loop_resilience_tests"
TMP.mkdir(parents=True, exist_ok=True)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def use_fresh_error_log(tag):
    """Point the disk error log at a fresh temp file and return its Path."""
    p = TMP / f"error_log_{tag}.jsonl"
    if p.exists():
        p.unlink()
    el.ERROR_LOG_PATH = p
    return p


class FakeBot:
    """Duck-typed aiogram Bot. fail=True simulates Telegram being unreachable."""
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("TelegramNetworkError: Cannot connect to api.telegram.org (DNS)")
        self.sent.append(text)


# ── Test 1: safe_send never raises, logs Telegram failure to disk ──────────────
print("\n[Test 1] safe_send: Telegram down -> no raise, cycle survives, disk-logged")
logp = use_fresh_error_log("safesend")
bot_down = FakeBot(fail=True)
ok = asyncio.run(m.safe_send(bot_down, 1, "hello"))
check("safe_send returns False when Telegram fails", ok is False)
check("safe_send did NOT raise (loop would survive)", True)
check("Telegram failure written to error_log.jsonl on disk", logp.exists())
rows = el.read_errors(20)
check("logged row source is monitor.telegram_send",
      any(r.get("source") == "monitor.telegram_send" for r in rows))
bot_ok = FakeBot(fail=False)
ok2 = asyncio.run(m.safe_send(bot_ok, 1, "hi"))
check("safe_send returns True on success and delivers", ok2 is True and bot_ok.sent == ["hi"])


# ── Test 2: a loop body that keeps sending to a dead Telegram keeps running ─────
print("\n[Test 2] loop body with dead Telegram completes all iterations")
use_fresh_error_log("loopbody")
bot_down = FakeBot(fail=True)

async def mini_loop():
    done = 0
    for i in range(3):
        # emulate an except-handler that notifies Telegram (the old killer path)
        await m.safe_send(bot_down, 1, f"error {i}")
        done += 1
    return done

completed = asyncio.run(mini_loop())
check("all 3 iterations completed despite Telegram failing", completed == 3)
check("3 telegram failures recorded on disk", len(el.read_errors(20)) >= 3)


# ── Test 3: supervisor restarts on exception, never dies forever ───────────────
print("\n[Test 3] _supervise restarts crashing run_once, logs each crash")
use_fresh_error_log("supervise")
calls = {"n": 0}

async def failing_run():
    calls["n"] += 1
    raise ValueError(f"boom {calls['n']}")

asyncio.run(m._supervise(failing_run, restart_delay=0, max_iterations=3))
check("run_once was restarted after every crash (3 runs)", calls["n"] == 3)
crashes = [r for r in el.read_errors(20) if r.get("source") == "monitor.loop_crash"]
check("each crash logged to error_log.jsonl on disk", len(crashes) >= 3)


# ── Test 4: on_crash notifier is invoked, its own failure is swallowed ─────────
print("\n[Test 4] _supervise calls on_crash; on_crash raising does not kill supervisor")
use_fresh_error_log("oncrash")
seen = []

async def failing_run2():
    raise RuntimeError("x")

async def flaky_on_crash(exc):
    seen.append(type(exc).__name__)
    raise RuntimeError("notify failed too")  # must be swallowed

asyncio.run(m._supervise(failing_run2, restart_delay=0, on_crash=flaky_on_crash, max_iterations=2))
check("on_crash invoked once per crash", len(seen) == 2)
check("supervisor survived on_crash raising", True)


# ── Test 5: CancelledError (process shutdown) propagates, not swallowed ─────────
print("\n[Test 5] _supervise propagates CancelledError (clean shutdown)")

async def cancel_run():
    raise asyncio.CancelledError()

async def run_cancel():
    try:
        await m._supervise(cancel_run, restart_delay=0, max_iterations=5)
        return "swallowed"
    except asyncio.CancelledError:
        return "propagated"

check("CancelledError propagates for shutdown", asyncio.run(run_cancel()) == "propagated")


# ── Test 6: heartbeat / health flags stale ticks (>10 min) ─────────────────────
print("\n[Test 6] health: stale tick flagged, fresh tick alive")
now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
old = (now - timedelta(minutes=20)).isoformat()
fresh = (now - timedelta(minutes=1)).isoformat()
check("tick age None on empty", m._tick_age_seconds(None, now) is None)
check("tick age ~1200s for 20-min-old", abs(m._tick_age_seconds(old, now) - 1200) < 2)
check("tick age handles naive timestamp", m._tick_age_seconds("2026-07-02T11:40:00", now) is not None)

state = {
    "futures_lab": {"last_tick": old},
    "rotation_lab": {"last_tick": fresh},
    "pro_lab": {"last_tick": None},
}
h = m.build_system_health(state, now=now)
check("stale futures (20 min) flagged NOT alive", h["futures"]["alive"] is False)
check("fresh rotation (1 min) flagged alive", h["rotation"]["alive"] is True)
check("missing pro tick -> not alive, age None", h["pro"]["alive"] is False and h["pro"]["age_seconds"] is None)


# ── Test 7: loop heartbeat + watchdog stale condition ──────────────────────────
print("\n[Test 7] loop heartbeat: _beat fresh, watchdog detects staleness")
m._beat()
age = m._beat_age_seconds()
check("beat age tiny right after _beat()", age is not None and age < 5)
h_live = m.build_system_health(state={}, now=None)
check("health loop alive right after _beat()", h_live["loop"]["alive"] is True)

# simulate a hung loop: push heartbeat past the stale threshold
m._last_beat["monotonic"] = time.monotonic() - (m.HEARTBEAT_STALE_SECONDS + 60)
stale_age = m._beat_age_seconds()
check("watchdog condition true when beat older than threshold",
      stale_age is not None and stale_age > m.HEARTBEAT_STALE_SECONDS)
h_stale = m.build_system_health(state={}, now=None)
check("health loop flagged NOT alive when stale", h_stale["loop"]["alive"] is False)
m._beat()  # restore for good hygiene


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
