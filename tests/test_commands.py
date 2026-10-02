"""Direct commands always win: parser + agent paths."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SP = Path(tempfile.mkdtemp(prefix='ha_test_'))   # scratch DBs live here
FIX = Path(__file__).parent
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from system import behaviour_log as bl  # noqa: E402
from system import commands  # noqa: E402
from system import patterns as pt  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


OFF_ROOM = {"fan_on": False, "fan_speed": 0, "led": "off"}
FAN50 = {"fan_on": True, "fan_speed": 50, "led": "white"}
FAN = lambda s: ("set_fan", {"on": True, "speed": s})  # noqa: E731
FAN_OFF = ("set_fan", {"on": False})
LED = lambda c: ("set_led", {"color": c})  # noqa: E731

print("\nC1  clear commands are understood exactly (no Qwen)")
cases = [
    ("start the fan", OFF_ROOM, [FAN(50)]),
    ("u are not calling the tool start the fan", OFF_ROOM, [FAN(50)]),
    ("turn on the fan", OFF_ROOM, [FAN(50)]),
    ("fan on", FAN50, [FAN(50)]),
    ("set the fan to 80", OFF_ROOM, [FAN(80)]),
    ("fan 60%", OFF_ROOM, [FAN(60)]),
    ("fan to 0", FAN50, [FAN_OFF]),
    ("turn off the fan", FAN50, [FAN_OFF]),
    ("stop the fan", FAN50, [FAN_OFF]),
    ("fan full speed", OFF_ROOM, [FAN(100)]),
    ("fan on low", OFF_ROOM, [FAN(30)]),
    ("make the fan faster", FAN50, [FAN(70)]),
    ("fan slower", FAN50, [FAN(30)]),
    ("make the light blue", OFF_ROOM, [LED("blue")]),
    ("light purple please", OFF_ROOM, [LED("purple")]),
    ("make it red", OFF_ROOM, [LED("red")]),
    ("turn off the light", FAN50, [LED("off")]),
    ("lights off", FAN50, [LED("off")]),
    ("turn on the led", OFF_ROOM, [LED("white")]),
    ("turn off the light and start the fan", FAN50, [LED("off"), FAN(50)]),
    ("set light to purple and fan 30", OFF_ROOM, [LED("purple"), FAN(30)]),
    ("hi, fan 40", OFF_ROOM, [FAN(40)]),
    ("\"start the fan\"", OFF_ROOM, [FAN(50)]),
]
for text, state, want in cases:
    got, sure = commands.parse(text, state)
    check(f"'{text}'", sure and sorted(got) == sorted(want), f"got {got} sure={sure}")

print("\nC2  vague or tricky messages are left to Qwen")
for text in ["the action didn't happen", "I'm hot", "as usual", "do according to the pattern",
             "hi turn on led light give it a cool colour", "make it cosy", "is the fan on?",
             "don't start the fan", "do not turn off the light", "make the light warm",
             "dim the light", "what can you do"]:
    got, sure = commands.parse(text, OFF_ROOM)
    check(f"'{text}' -> Qwen", not sure, f"got {got}")
got, sure = commands.parse("fan 60 and make it cosy", OFF_ROOM)
check("'fan 60 and make it cosy' -> Qwen, but fan 60 kept as safety net",
      not sure and got == [FAN(60)], f"{got} {sure}")

# ---------------------------------------------------------------------------
db = SP / "cmd.db"
for suf in ("", "-wal", "-shm"):
    Path(str(db) + suf).unlink(missing_ok=True)
shutil.copy(ROOT / "data" / "synthetic_events.db", db)
bl.DB_PATH = db
bl.init_db()
pt.PATTERNS_JSON = SP / "cmd.patterns.json"
pt.PATTERNS_TXT = SP / "cmd.patterns.txt"
pt.save_patterns(pt.merge_runtime(pt.compute_patterns(), []))
bl.set_state(False, 0, "off")
bl.set_sim_time(datetime(2026, 10, 5, 12, 50))        # Monday 12:50, like your test
import agent  # noqa: E402

qwen_calls = []


def queued():
    with bl._conn() as c:
        rows = [(r["name"], json.loads(r["args"]), r["source"])
                for r in c.execute("SELECT name, args, source FROM commands WHERE status='pending'")]
        c.execute("UPDATE commands SET status='done'")
    return rows


def run(text, qwen):
    qwen_calls.clear()
    agent.ask_qwen = qwen
    with contextlib.redirect_stdout(io.StringIO()):
        out = agent.handle(text, [{"role": "system", "content": agent.build_system_prompt()}])
    return out, queued()


def never(messages, schema=None):
    qwen_calls.append(messages)
    raise AssertionError("Qwen should not be needed")


print("\nC3  your screenshot, Monday 12:50, no habit for midday")
out, q = run("start the fan", never)
check("'start the fan' -> fan on immediately, no Qwen", q == [("set_fan", {"on": True, "speed": 50}, "user")], (out, q))
out, q = run("u are not calling the tool start the fan", never)
check("'u are not calling the tool start the fan' -> fan on", q == [("set_fan", {"on": True, "speed": 50}, "user")], q)


def refuses(messages, schema=None):
    """Qwen claims an action in text, then (after the nudge) calls the tool."""
    qwen_calls.append(messages)
    if messages[-1]["content"] == agent.NUDGE:
        return json.dumps({"light": None, "fan": 80, "feeling": None, "reply": "Fan at 80%."})
    return json.dumps({"light": None, "fan": None, "feeling": None,
                       "reply": "Despite the fan having been off, I'll set the fan to 80% to mimic their routine."})


out, q = run("the action didn't happen", refuses)
check("Qwen says 'I'll set the fan' without a tool -> reminded once, then it acts",
      len(qwen_calls) == 2 and q == [("set_fan", {"on": True, "speed": 80}, "user")], (len(qwen_calls), q))


def stubborn(messages, schema=None):
    qwen_calls.append(messages)
    return json.dumps({"light": None, "fan": None, "feeling": None,
                       "reply": "I cannot start the fan during the midday slot."})


out, q = run("fan 60 and make it cosy", stubborn)
check("Qwen refuses -> the clear part (fan 60) still runs", q == [("set_fan", {"on": True, "speed": 60}, "user")], (out, q))

seen = {}


def capture(messages, schema=None):
    seen["m"] = messages
    return json.dumps({"light": None, "fan": None, "feeling": None,
                       "reply": "There's no habit for right now. What would you like?"})


out, q = run("what should we do now", capture)
system = seen["m"][0]["content"]
user = seen["m"][-1]["content"]
check("prompt says a direct request always wins", "direct request always wins" in system)
check("prompt says: no habit now -> ask the user", "no habit for right now, ask what" in system)
check("Qwen is told it's Monday 12:50 with no habit now",
      "Monday 2026-10-05 12:50" in user and "habit for right now: none" in user, user)
check("nothing changes when there's no habit and Qwen asks", q == [] and "What would you like" in out, (out, q))

print("\nC4  a direct command while a suggestion is waiting")
bl.set_presence(True)
bl.set_sim_time(datetime(2026, 9, 30, 22, 40))
agent.pending = None
agent.ask_qwen = lambda m, schema=None: "fallback question"
with contextlib.redirect_stdout(io.StringIO()):
    agent.tick(datetime(2026, 9, 30, 22, 40))
sug = agent.pending
agent.pending = None
agent.ask_qwen = never
qwen_calls.clear()
with contextlib.redirect_stdout(io.StringIO()):
    out = agent.answer_suggestion("no, fan 60", sug, [{"role": "system", "content": ""}])
q = queued()
check("'no, fan 60' -> fan 60 exactly, no Qwen, as your own command",
      not qwen_calls and q == [("set_fan", {"on": True, "speed": 60}, "user")], (out, q))
bed = next(p for p in pt.load_patterns() if p["id"] == sug["pattern"]["id"])
check("counted as a correction (stop asking today)", bed["done_date"] == "2026-09-30")

print("\nC5  controller accepts the date/time with quotes")
import types  # noqa: E402
sys.modules.setdefault("gpiozero", types.SimpleNamespace(DigitalInputDevice=None))
for name in ("devices.fan_control", "devices.led_control"):
    m = types.ModuleType(name)
    m.set_fan = m.get_fan = m.set_led = m.get_led = None
    sys.modules[name] = m
import controller  # noqa: E402
ref = datetime(2026, 9, 30, 12, 0)
check("'\"sat 23:00\"' works", controller.parse_when('"sat 23:00"', ref) == datetime(2026, 10, 3, 23, 0))
check("\"'2026-10-03 23:00'\" works", controller.parse_when("'2026-10-03 23:00'", ref) == datetime(2026, 10, 3, 23, 0))
bl.set_sim_time(None)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
