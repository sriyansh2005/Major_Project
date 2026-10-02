"""Thorough tests for the Phase 7 habit changes. Run from the project root."""
import contextlib
import io
import json
import random
import shutil
import sqlite3
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


def fresh(db_name, src=ROOT / "data" / "synthetic_events.db"):
    db = SP / db_name
    for suf in ("", "-wal", "-shm"):
        Path(str(db) + suf).unlink(missing_ok=True)
    if src:
        shutil.copy(src, db)
    bl.DB_PATH = db
    bl.init_db()
    pt.PATTERNS_JSON = SP / f"{db_name}.patterns.json"
    pt.PATTERNS_TXT = SP / f"{db_name}.patterns.txt"
    pt.PATTERNS_JSON.unlink(missing_ok=True)
    pt.PATTERNS_TXT.unlink(missing_ok=True)
    return db


def label(mode, seed=1):
    """Fill intent_id: 'truth' = correct, 'noisy' = like a confused 3B model, 'none'."""
    rng = random.Random(seed)
    with bl._conn() as c:
        cmds = c.execute("SELECT id, utterance, slot FROM events WHERE kind='command'").fetchall()
    if mode == "none":
        return
    names = ["cooling", "reduce_airflow", "ambience", "sleep_prep", "arrival", "leaving",
             "bedtime", "chill", "movie_mode", "night_routine"]
    for r in cmds:
        u = r["utterance"].lower()
        truth = ("sleep_prep" if any(w in u for w in ["sleep", "night", "bed", "fan low"]) and "light on" not in u
                 else "leaving" if any(w in u for w in ["out", "leaving", "work", "bye"])
                 else "cooling" if any(w in u for w in ["hot", "melt", "crank", "faster", "higher", "max", "100", "boil", "increase", "full"])
                 else "arrival" if any(w in u for w in ["morning", "wake", "lights on", "light on", "set the room"])
                 else "ambience")
        name = truth if mode == "truth" or rng.random() > 0.45 else rng.choice(names)
        iid = bl.add_intent(name, "test")
        bl.set_intent([r["id"]], iid)


def learn():
    return pt.compute_patterns()


def ids(ps):
    return sorted(p["id"] for p in ps)


def table(ps):
    for p in ps:
        print(f"      {p['id']:42s} {p['window']}  ~{p['typical_time']}  "
              f"{pt.describe_actions(p['actions']):28s} {p['confidence']:.2f}  [{p['intent']}]")


# ---------------------------------------------------------------------------
print("\nT1  synthetic data, correct labels -> the persona's real habits")
fresh("t1.db"); label("truth")
p1 = learn(); table(p1)
expected = {
    "weekday:early:fan-low+led-bright": "fan 30% and light white",
    "weekday:evening:fan-high": "fan 80%",
    "weekday:night:led-calm": None,
    "weekday:night:fan-low+led-off": "fan 30% and light off",
    "weekend:late:fan-off+led-off": "fan off and light off",
    "weekend:morning:fan-medium+led-bright": "fan 50% and light white",
    "weekend:midday:fan-high": None,
    "weekend:evening:led-calm": "light purple",
}
check("exactly the 8 expected habits", set(ids(p1)) == set(expected), ids(p1))
for p in p1:
    want = expected.get(p["id"])
    if want:
        check(f"{p['id']} does '{want}'", pt.describe_actions(p["actions"]) == want,
              pt.describe_actions(p["actions"]))
check("no leaving habit (weekday early all-off skipped)",
      not any(p["slot"] == "early" and pt.describe_actions(p["actions"]) == "fan off and light off" for p in p1))
check("one-offs (red/green) never become habits", not any("vivid" in p["id"] for p in p1))

# ---------------------------------------------------------------------------
print("\nT2  same data, labels scrambled like a confused Qwen (45% wrong, invented names)")
fresh("t2.db"); label("noisy")
p2 = learn()
check("same habits found", ids(p2) == ids(p1), ids(p2))
check("same actions and windows",
      [(p["actions"], p["window"]) for p in p2] == [(p["actions"], p["window"]) for p in p1])

print("\nT3  no labels at all (update_intents.py never ran)")
fresh("t3.db"); label("none")
p3 = learn()
check("same habits found", ids(p3) == ids(p1), ids(p3))
check("intent falls back to 'habit'", all(p["intent"] == "habit" for p in p3))

# ---------------------------------------------------------------------------
print("\nT4  grouping similar actions")
def fan(vals): return [len(g) for g in pt._fan_groups([{"value": v} for v in vals])]
check("fan 30/40/50 -> one group", fan([30, 40, 50, 30, 40, 50]) == [6], fan([30, 40, 50, 30, 40, 50]))
check("fan 30 vs 90 -> two groups", fan([30, 30, 90, 90]) == [2, 2], fan([30, 30, 90, 90]))
check("fan 30..90 in steps of 20 -> split (no chaining)", len(fan([30, 50, 70, 90])) == 2, fan([30, 50, 70, 90]))
check("fan off is its own group", fan([0, 0, 30]) == [2, 1], fan([0, 0, 30]))
leds = pt._led_groups([{"value": c} for c in ["blue", "purple", "cyan", "off", "white"]])
check("blue/purple/cyan -> one 'calm' group", sorted(len(g) for g in leds) == [1, 1, 3])

print("\nT5  your bedtime example: blue/purple + fan 30/40/50 on varying nights")
db = fresh("t5.db", src=None)
rng = random.Random(7)
start = datetime(2026, 9, 7)  # Monday
for d in range(21):
    day = start + timedelta(days=d)
    if day.weekday() >= 5 or rng.random() < 0.1:
        continue
    ts = day + timedelta(hours=22, minutes=50 + rng.randint(-20, 20))
    cid = bl.log_event("command", "user", utterance=rng.choice(["sleep", "dim it, fan low", "bed"]),
                       before={}, ts=ts)
    bl.log_event("action", "user", tool="set_led", args={"color": rng.choice(["blue", "purple"])},
                 parent_id=cid, ts=ts)
    bl.log_event("action", "user", tool="set_fan", args={"on": True, "speed": rng.choice([30, 40, 50])},
                 parent_id=cid, ts=ts)
p5 = learn(); table(p5)
check("one habit, not split by colour or speed", len(p5) == 1 and p5[0]["id"] == "weekday:night:fan-low+led-calm",
      ids(p5))
check("suggests a calm colour and a speed in 30-50",
      p5 and p5[0]["actions"][1]["args"]["color"] in ("blue", "purple")
      and 30 <= p5[0]["actions"][0]["args"]["speed"] <= 50)

# ---------------------------------------------------------------------------
print("\nT6  habits cover their whole slot")
fresh("t6.db"); label("truth"); ps = pt.merge_runtime(learn(), [])
relax = next(p for p in ps if p["id"] == "weekday:night:led-calm")
bed = next(p for p in ps if p["id"] == "weekday:night:fan-low+led-off")
check("relax starts at 21:00", relax["valid_from"] == 21 * 60, relax["window"])
check("bed ends at 23:59", bed["valid_to"] == 23 * 60 + 59, bed["window"])
check("no gap and no overlap between them", bed["valid_from"] == relax["valid_to"] + 1,
      f"{relax['window']} / {bed['window']}")
wed = datetime(2026, 9, 30)
def due_at(h, m, day=wed): return [p["id"] for p in pt.due_patterns(ps, day.replace(hour=h, minute=m))]
check("21:05 Wed -> relax", due_at(21, 5) == ["weekday:night:led-calm"], due_at(21, 5))
check("22:50 Wed -> bed", due_at(22, 50) == ["weekday:night:fan-low+led-off"], due_at(22, 50))
check("23:58 Wed -> bed", due_at(23, 58) == ["weekday:night:fan-low+led-off"], due_at(23, 58))
check("17:01 Wed -> evening cooling (start of slot)", due_at(17, 1) == ["weekday:evening:fan-high"], due_at(17, 1))
check("12:00 Wed -> nothing", due_at(12, 0) == [], due_at(12, 0))
pt.save_patterns(pt.merge_runtime(ps, []))
pt.update_pattern("weekday:night:led-calm", done_date="2026-09-30")
check("already accepted today -> not again",
      [p["id"] for p in pt.due_patterns(pt.load_patterns(), wed.replace(hour=21, minute=30))] == [])

# ---------------------------------------------------------------------------
print("\nT7  Saturday 23:00 (your test): what Qwen is told")
ps = pt.load_patterns()
ctx = pt.now_context(ps, datetime(2026, 10, 3, 23, 0),
                     {"fan_on": True, "fan_speed": 90, "led": "yellow", "present": True})
print("      " + ctx.replace("\n", "\n      "))
check("knows it's Saturday night", "Saturday" in ctx and "weekend, night slot" in ctx)
check("says there's no habit right now", "habit for right now: none" in ctx)
check("next habit = fan off and light off from Sunday 00:00",
      "from Sunday 00:00" in ctx and "fan off and light off" in ctx)
ctx2 = pt.now_context(ps, datetime(2026, 9, 30, 22, 40),
                      {"fan_on": False, "fan_speed": 0, "led": "blue", "present": True})
check("Wed 22:40 -> habit for now is bedtime", "right now: fan 30% and light off" in ctx2, ctx2)

# ---------------------------------------------------------------------------
print("\nT8  agent: context sent, yes/no without Qwen, corrections via Qwen")
import agent  # noqa: E402
calls = []
def no_qwen(messages, schema=None):
    calls.append(messages)
    raise AssertionError("Qwen must not be called for a plain yes/no")
agent.ask_qwen = no_qwen
bl.set_sim_time(datetime(2026, 9, 30, 22, 40))
bl.set_presence(True)
bed = next(p for p in pt.load_patterns() if p["id"] == "weekday:night:fan-low+led-off")
def sug(): return {"pattern": bed, "question": "Bedtime?", "event_id": 1, "at": 0}
def pending_cmds():
    with bl._conn() as c:
        rows = [tuple(r) for r in c.execute("SELECT name, args, source FROM commands WHERE status='pending'")]
        c.execute("UPDATE commands SET status='done'")
    return rows
def get(pid): return next(p for p in pt.load_patterns() if p["id"] == pid)
H = [{"role": "system", "content": ""}]
for word in ["yeah", "Yes please!", "ok", "sure."]:
    out = agent.answer_suggestion(word, sug(), list(H))
q = pending_cmds()
check("'yeah'/'Yes please!'/'ok'/'sure.' approve without Qwen", not calls and get(bed["id"])["approvals"] == 4,
      f"calls={len(calls)} approvals={get(bed['id'])['approvals']}")
check("approval runs exactly the habit, as source=auto",
      q[:2] == [("set_fan", '{"on": true, "speed": 30}', "auto"), ("set_led", '{"color": "off"}', "auto")], q[:2])
for word in ["no", "nah", "not now"]:
    agent.answer_suggestion(word, sug(), list(H))
check("'no'/'nah'/'not now' decline without Qwen, counted once per day",
      not calls and get(bed["id"])["rejections"] == 1, get(bed["id"])["rejections"])
check("nothing queued on decline", pending_cmds() == [])

seen = {}
def qwen_blue(messages, schema=None):
    seen["msgs"] = messages
    return json.dumps({"light": "blue", "fan": None, "feeling": None, "reply": ""})
agent.ask_qwen = qwen_blue
out = agent.answer_suggestion("no, make it blue instead", sug(), list(H))
check("mixed reply goes to Qwen and does what you asked", "blue" in out and pending_cmds() == [("set_led", '{"color": "blue"}', "user")], out)
check("clear correction ran without Qwen", "msgs" not in seen)
agent.answer_suggestion("no, make it cosy", sug(), list(H))
pending_cmds()
check("vague correction: Qwen saw the current time + habits",
      "Current time: Wednesday 2026-09-30 22:40" in seen["msgs"][-1]["content"])
with bl._conn() as c:
    r = c.execute("SELECT source FROM events WHERE kind='command' AND utterance LIKE 'no, make it blue%'").fetchone()
check("correction saved as your own command (learned next time)", r and r["source"] == "user")

def qwen_cmd(messages, schema=None):
    seen["msgs"] = messages
    return json.dumps({"light": None, "fan": 0, "feeling": None, "reply": ""})
agent.ask_qwen = qwen_cmd
bl.set_sim_time(datetime(2026, 10, 3, 23, 0))
hist = [{"role": "system", "content": "sys"}]
agent.handle("set things up for me", hist)
last = seen["msgs"][-1]["content"]
check("normal command: Qwen gets Saturday 23:00 + next habit", "Saturday" in last and "Next habit" in last and last.endswith("User says: set things up for me"))
check("history keeps your plain words (no context bloat)", hist[1]["content"] == "set things up for me")
pending_cmds()

agent.ask_qwen = no_qwen
agent.answer_suggestion("never ask me that", sug(), list(H))
check("'never ask me that' disables the habit", get(bed["id"])["disabled"] is True)

# ---------------------------------------------------------------------------
print("\nT9  re-learning keeps your answers")
before = {p["id"]: (p["approvals"], p["rejections"], p["disabled"]) for p in pt.load_patterns()}
with contextlib.redirect_stdout(io.StringIO()):
    import update_patterns
    update_patterns.main()
after = {p["id"]: (p["approvals"], p["rejections"], p["disabled"]) for p in pt.load_patterns()}
check("approvals/rejections/disabled carried over", after[bed["id"]] == before[bed["id"]],
      f"{before[bed['id']]} -> {after[bed['id']]}")
check("patterns.txt written from the data", pt.PATTERNS_TXT.exists() and "Weekdays:" in pt.PATTERNS_TXT.read_text())

print("\nT10 your current patterns.json (old format) doesn't crash anything")
fresh("t10.db"); label("truth")
pt.PATTERNS_JSON.write_text(json.dumps({"updated": "x", "patterns": [{
    "id": "weekday:night:sleep_prep", "day_type": "weekday", "slot": "night", "intent": "sleep_prep",
    "typical_minute": 1384, "typical_time": "23:04", "actions": [{"tool": "set_led", "args": {"color": "off"}}],
    "confidence": 0.69, "count": 14, "approvals": 0, "rejections": 0, "disabled": False, "last_fired": None}]}))
agent.ask_qwen = lambda m, schema=None: (_ for _ in ()).throw(requests.ConnectionError())
check("old entries ignored", pt.load_patterns() == [])
bl.set_presence(True)
agent.pending = None
agent.tick(datetime(2026, 9, 30, 22, 40))
check("checker runs, no suggestion, no crash", agent.pending is None)
with contextlib.redirect_stdout(io.StringIO()):
    update_patterns.main()
check("after learn.py the new format is in place", len(pt.load_patterns()) == 8)

print("\nT11 checker end to end at Wed 22:40, someone home")
bl.set_presence(True)
agent.pending = None
agent.ask_qwen = lambda m, schema=None: (_ for _ in ()).throw(requests.ConnectionError())
with contextlib.redirect_stdout(io.StringIO()) as out:
    agent.tick(datetime(2026, 9, 30, 22, 40))
check("asks about bedtime", agent.pending and agent.pending["pattern"]["id"] == "weekday:night:fan-low+led-off",
      out.getvalue().strip())
print("      " + out.getvalue().strip())
bl.set_presence(False); agent.pending = None
agent.tick(datetime(2026, 9, 30, 21, 10))
check("nobody home -> no question", agent.pending is None)
bl.set_sim_time(None)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
