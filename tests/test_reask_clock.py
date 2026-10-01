"""Tests for re-asking (agent.py) and the interactive clock (controller.py)."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

SP = Path(tempfile.mkdtemp(prefix='ha_test_'))   # scratch DBs live here
FIX = Path(__file__).parent
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from system import behaviour_log as bl  # noqa: E402
from system import patterns as pt  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


db = SP / "ra.db"
for suf in ("", "-wal", "-shm"):
    Path(str(db) + suf).unlink(missing_ok=True)
shutil.copy(ROOT / "data" / "synthetic_events.db", db)
bl.DB_PATH = db
bl.init_db()
pt.PATTERNS_JSON = SP / "ra.patterns.json"
pt.PATTERNS_TXT = SP / "ra.patterns.txt"
pt.save_patterns(pt.merge_runtime(pt.compute_patterns(), []))

import agent  # noqa: E402
agent.ask_qwen = lambda m, schema=None: (_ for _ in ()).throw(requests.ConnectionError())
MOVIE = "weekend:evening:led-calm"
SAT = datetime(2026, 10, 3)


def get(pid=MOVIE):
    return next(p for p in pt.load_patterns() if p["id"] == pid)


def due(h, m, day=SAT):
    return [p["id"] for p in pt.due_patterns(pt.load_patterns(), day.replace(hour=h, minute=m),
                                             agent.REASK_MIN, agent.MAX_ASKS)]


def ask(h, m, day=SAT):
    """Run the checker at h:m; return the question (or None)."""
    agent.pending = None
    with contextlib.redirect_stdout(io.StringIO()) as out:
        agent.tick(day.replace(hour=h, minute=m))
    return agent.pending


def answer(text, h, m, day=SAT):
    bl.set_sim_time(day.replace(hour=h, minute=m))
    s = agent.pending
    agent.pending = None
    with contextlib.redirect_stdout(io.StringIO()):
        return agent.answer_suggestion(text, s, [{"role": "system", "content": ""}])


bl.set_presence(True)
print(f"\nR1  re-ask every {agent.REASK_MIN} min, max {agent.MAX_ASKS} a day (Sat evening movie habit)")
check("17:05 asks", ask(17, 5) and agent.pending["pattern"]["id"] == MOVIE)
print("      reply to 'no':", answer("no", 17, 6))
check(f"17:08 too soon ({agent.REASK_MIN} min gap)", MOVIE not in due(17, 8), due(17, 8))
check("17:10 asks again", ask(17, 10) is not None)
print("      reply to 'nah':", answer("nah", 17, 11))
check("17:15 asks a third time", ask(17, 15) is not None)
third = answer("not now", 17, 16)
print("      reply to 'not now':", third)
check("third reply says it won't ask again today", "won't ask again today" in third, third)
check("17:30 stops: 3 asks used", MOVIE not in due(17, 30) and ask(17, 30) is None, due(17, 30))
p = get()
check("three 'no's in one day count as ONE rejection", p["rejections"] == 1, p["rejections"])
check("not muted after a picky evening", not pt.is_muted(p))

print("\nR2  'yes' stops asking for the day; next weekend day starts fresh")
SUN = datetime(2026, 10, 4)
check("Sunday 17:05 asks again (new day)", ask(17, 5, SUN) is not None)
print("      reply to 'yeah':", answer("yeah", 17, 6, SUN))
check("after yes: nothing more today", MOVIE not in due(17, 30, SUN), due(17, 30, SUN))
check("approval counted", get()["approvals"] == 1)

print("\nR3  a correction ('no, make it red') also stops it for the day")
agent.ask_qwen = lambda m, schema=None: (
    json.dumps({"light": "red", "fan": None, "feeling": None, "reply": ""}) if schema
    else (_ for _ in ()).throw(requests.ConnectionError()))
NEXT_SAT = datetime(2026, 10, 10)
check("next Saturday asks", ask(17, 5, NEXT_SAT) is not None)
print("      reply:", answer("no, make it red", 17, 6, NEXT_SAT))
check("after correction: nothing more today", MOVIE not in due(18, 0, NEXT_SAT))

print("\nR4  'no' only counts once per day, across many days it still mutes")
p0 = get()["rejections"]
saturdays = 0
for d in range(5):
    day = datetime(2026, 10, 17) + timedelta(days=7 * d)      # more Saturdays
    if ask(17, 5, day) is None:
        break
    answer("no", 17, 6, day)
    saturdays += 1
g = get()
check("muted once rejections - approvals reaches 3", pt.is_muted(g) and g["rejections"] - g["approvals"] == 3,
      f"rej={g['rejections']} app={g['approvals']}")
check("took 2 more Saturdays (1 yes, 2 earlier no-days already counted)", saturdays == 2, saturdays)
check("muted habit is never asked again", ask(17, 5, datetime(2026, 11, 28)) is None)

print("\nR5  interactive clock (controller.py helpers)")
import types  # noqa: E402
sys.modules.setdefault("gpiozero", types.SimpleNamespace(DigitalInputDevice=None))
devs = types.ModuleType("devices.fan_control"); devs.set_fan = devs.get_fan = None
leds = types.ModuleType("devices.led_control"); leds.set_led = leds.get_led = None
sys.modules["devices.fan_control"] = devs
sys.modules["devices.led_control"] = leds
import controller  # noqa: E402
ref = datetime(2026, 9, 30, 12, 0)                      # a Wednesday
check("'2026-10-03 23:00'", controller.parse_when("2026-10-03 23:00", ref) == datetime(2026, 10, 3, 23, 0))
check("'sat 23:00' -> next Saturday", controller.parse_when("sat 23:00", ref) == datetime(2026, 10, 3, 23, 0))
check("'Saturday 23:00' works too", controller.parse_when("Saturday 23:00", ref) == datetime(2026, 10, 3, 23, 0))
check("'wed 22:30' -> same day (Wednesday)", controller.parse_when("wed 22:30", ref) == datetime(2026, 9, 30, 22, 30))
check("'tue 07:00' -> next Tuesday", controller.parse_when("tue 07:00", ref) == datetime(2026, 10, 6, 7, 0))
check("Enter -> real clock", controller.parse_when("", ref) is None)
for bad in ["tomorrow", "sat", "25:99", "2026-13-01 10:00"]:
    try:
        controller.parse_when(bad, ref)
        check(f"rejects '{bad}'", False)
    except ValueError:
        check(f"rejects '{bad}'", True)

with contextlib.redirect_stdout(io.StringIO()) as out:
    controller.set_clock("sat 23:00")
check("set_clock moves the shared clock", bl.now().strftime("%a %H:%M") == "Sat 23:00", out.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    controller.set_clock("")
check("Enter goes back to the real clock",
      abs((bl.now() - datetime.now()).total_seconds()) < 2)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
