"""
Check how the real Qwen handles tricky messages. Run on the Pi:

    python tools/qwen_check.py

- Uses a temporary copy of the database and habit files, so your real
  events.db / patterns.json are never changed.
- Does NOT touch the fan or LED (controller.py doesn't need to be running).
- Needs Ollama running with qwen2.5:3b. Takes a few minutes on the Pi.

Prints PASS/FAIL per message with what Qwen decided and how long it took.
"""

import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from system import behaviour_log as bl  # noqa: E402
from system import patterns as pt  # noqa: E402

# --- Work on copies only -----------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="qwen_check_"))
bl.DB_PATH = tmp / "events.db"
src = ROOT / "data" / "synthetic_events.db"
if (ROOT / "events.db").exists():
    shutil.copy(ROOT / "events.db", bl.DB_PATH)
    try:
        bl.init_db()
        src = ROOT / "events.db"
    except RuntimeError:                         # very old events.db: use synthetic data
        pass
if src.name == "synthetic_events.db":
    bl.DB_PATH.unlink(missing_ok=True)
    shutil.copy(src, bl.DB_PATH)
    bl.init_db()
pt.PATTERNS_JSON = tmp / "patterns.json"
pt.PATTERNS_TXT = tmp / "patterns.txt"
pt.save_patterns(pt.merge_runtime(pt.compute_patterns(), []), preferences=[])
pt.PATTERNS_TXT.write_text("Learned habits:\n" + pt.profile_text(pt.load_patterns(), []) + "\n")

import agent  # noqa: E402  (after the paths are redirected)


def queued():
    with bl._conn() as c:
        rows = [(r["name"], json.loads(r["args"]))
                for r in c.execute("SELECT name, args FROM commands WHERE status='pending'")]
        c.execute("UPDATE commands SET status='done'")
    return rows


def led(rows):
    return next((a["color"] for n, a in rows if n == "set_led"), None)


def fan(rows):
    a = next((a for n, a in rows if n == "set_fan"), None)
    return None if a is None else (a.get("speed", 0) if a.get("on", True) else 0)


def feeling():
    with bl._conn() as c:
        return c.execute("SELECT feeling FROM events WHERE kind='command' ORDER BY id DESC LIMIT 1").fetchone()[0]


CASES = [
    # (clock, room state (fan_on, speed, led), message, expectation text, check)
    (datetime(2026, 10, 6, 18, 40), (True, 80, "white"), "i am feeling sad set the mood of the room",
     "light or fan changes, feeling = sad", lambda r: bool(r) and feeling() == "sad"),
    (datetime(2026, 10, 6, 18, 40), (True, 80, "white"), "Led is white yet",
     "light changes to something other than white", lambda r: led(r) not in (None, "white")),
    (datetime(2026, 10, 6, 18, 40), (True, 80, "blue"), "make led light warm",
     "light yellow", lambda r: led(r) == "yellow"),
    (datetime(2026, 10, 6, 18, 40), (True, 80, "white"), "I am sad change the led colour to a sad mood",
     "light changes", lambda r: led(r) is not None),
    (datetime(2026, 10, 6, 18, 40), (True, 30, "white"), "I'm really hot",
     "fan goes above 30%", lambda r: (fan(r) or 0) > 30),
    (datetime(2026, 10, 6, 18, 40), (True, 80, "white"), "is the fan on?",
     "nothing changes (just a reply)", lambda r: r == []),
    (datetime(2026, 10, 5, 12, 50), (False, 0, "off"), "do according to the pattern",
     "Mon 12:50 has no habit: nothing changes, it asks", lambda r: r == []),
    (datetime(2026, 10, 6, 18, 40), (False, 0, "off"), "do it as usual",
     "Tue evening habit: fan around 80%", lambda r: 60 <= (fan(r) or 0) <= 100),
    (datetime(2026, 10, 6, 18, 40), (False, 0, "off"), "start the fan",
     "fan on (direct command, no Qwen)", lambda r: (fan(r) or 0) > 0),
]

PREF_CASE = (datetime(2026, 10, 7, 20, 0), (True, 50, "white"), "feeling sad again today",
             "uses your learned preference: light blue", lambda r: led(r) == "blue")


def run(clock, state, text, expect, check):
    bl.set_sim_time(clock)
    bl.set_state(*state)
    bl.set_presence(True)
    hist = [{"role": "system", "content": agent.build_system_prompt()}]
    t0 = time.monotonic()
    try:
        reply = agent.handle(text, hist)
    except Exception as e:                       # report, keep going
        reply = f"ERROR: {e}"
    secs = time.monotonic() - t0
    rows = queued()
    ok = check(rows)
    print(f"\n{'PASS' if ok else 'FAIL'}  \"{text}\"  ({secs:.0f}s)")
    print(f"      expected: {expect}")
    print(f"      changed : {rows or 'nothing'}")
    print(f"      reply   : {reply.splitlines()[0] if reply else ''}")
    return ok


def main():
    print(f"Testing real Qwen ({agent.MODEL}) on a copy of {src.name} in {tmp}")
    results = [run(*c) for c in CASES]

    # Teach a preference (sad -> blue, twice), then see if Qwen follows it.
    for day in (1, 2):
        t = datetime(2026, 10, day, 21, 0)
        cid = bl.log_event("command", "user", utterance="i'm sad", before={}, ts=t)
        bl.set_feeling(cid, "sad")
        bl.log_event("action", "user", tool="set_led", args={"color": "blue"}, parent_id=cid, ts=t)
    pt.save_patterns(pt.load_patterns(), preferences=pt.compute_preferences())
    print(f"\nLearned preferences for the next test: {pt.load_preferences()}")
    results.append(run(*PREF_CASE))

    bl.set_sim_time(None)
    print(f"\n{sum(results)}/{len(results)} passed. Temporary files: {tmp}")


if __name__ == "__main__":
    main()
