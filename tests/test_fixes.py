"""Tests for the overnight fixes: JSON decisions, feelings, accurate profile."""
import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
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


def fresh(name, src=ROOT / "data" / "synthetic_events.db"):
    db = SP / name
    for suf in ("", "-wal", "-shm"):
        Path(str(db) + suf).unlink(missing_ok=True)
    if src:
        shutil.copy(src, db)
    bl.DB_PATH = db
    bl.init_db()
    pt.PATTERNS_JSON = SP / f"{name}.patterns.json"
    pt.PATTERNS_TXT = SP / f"{name}.patterns.txt"
    pt.PATTERNS_JSON.unlink(missing_ok=True)
    pt.PATTERNS_TXT.unlink(missing_ok=True)


def form(light=None, fan=None, feeling=None, reply=""):
    return json.dumps({"light": light, "fan": fan, "feeling": feeling, "reply": reply})


fresh("fx.db")
pt.save_patterns(pt.merge_runtime(pt.compute_patterns(), []), preferences=[])
bl.set_state(True, 80, "white")
bl.set_sim_time(datetime(2026, 10, 6, 18, 40))       # Tue evening, like your test
import agent  # noqa: E402

calls = []


def fake(responses):
    """Qwen stub returning the given replies in order; records what it was sent."""
    it = iter(responses)

    def _q(messages, schema=None):
        calls.append((messages, schema))
        return next(it)
    return _q


def queued():
    with bl._conn() as c:
        rows = [(r["name"], json.loads(r["args"]), r["source"])
                for r in c.execute("SELECT name, args, source FROM commands WHERE status='pending'")]
        c.execute("UPDATE commands SET status='done'")
    return rows


def run(text, responses):
    calls.clear()
    agent.ask_qwen = fake(responses)
    hist = [{"role": "system", "content": agent.build_system_prompt()}]
    with contextlib.redirect_stdout(io.StringIO()):
        out = agent.handle(text, hist)
    return out, queued(), hist


def last_feeling():
    with bl._conn() as c:
        return c.execute("SELECT feeling FROM events WHERE kind='command' ORDER BY id DESC LIMIT 1").fetchone()[0]


# ---------------------------------------------------------------------------
print("\nF1  your screenshot messages, with Qwen answering through the JSON form")
out, q, _ = run("i am feeling sad set the mood of the room according to i",
                [form("purple", 30, "sad", "Sorry you're feeling low, I've set a soft purple light.")])
check("'I am feeling sad...' -> light + fan actually change",
      q == [("set_fan", {"on": True, "speed": 30}, "user"), ("set_led", {"color": "purple"}, "user")], q)
check("the feeling 'sad' is saved on the request", last_feeling() == "sad")
check("Qwen was asked with a JSON schema (forced form)", calls[0][1] == agent.DECISION_SCHEMA)
check("reply shown to the user", out.startswith("Sorry you're feeling low"), out)

out, q, _ = run("Led is white yet", [form("purple", None, None, "Changing it to purple now.")])
check("'Led is white yet' -> not read as 'set white'; Qwen sets purple",
      q == [("set_led", {"color": "purple"}, "user")], q)
check("'Led is white yet' went to Qwen (1 call)", len(calls) == 1)

out, q, _ = run("increase fan speed and make led light warm",
                [form("yellow", 100, None, "Fan up and a warm yellow light.")])
check("'make led light warm' -> yellow (and fan up)",
      sorted(q) == sorted([("set_fan", {"on": True, "speed": 100}, "user"), ("set_led", {"color": "yellow"}, "user")]), q)

out, q, _ = run("I am sad change the led colour to a sad mood", [form("blue", None, "sad", "Here's a calm blue.")])
check("'I am sad change the led colour...' -> light changes", q == [("set_led", {"color": "blue"}, "user")], q)

print("\nF2  Qwen mistakes are caught")
out, q, _ = run("set the room lighting as I am sad",
                [form(None, None, "sad", "Set the LED to purple."), form("purple", None, "sad", "Done, purple.")])
check("says 'Set the LED to purple.' with nulls -> asked again, then it changes",
      len(calls) == 2 and q == [("set_led", {"color": "purple"}, "user")], (len(calls), q))
check("the retry tells Qwen nothing happened", calls[1][0][-1]["content"] == agent.NUDGE)

out, q, _ = run("make it cosy", ["not json at all", form("yellow", 30, None, "Cosy.")])
check("broken JSON -> retried once, then works", len(calls) == 2 and len(q) == 2, (len(calls), q))

out, q, _ = run("make it cosy", ["{", "{"])
check("broken JSON twice -> nothing changes, no crash", q == [] and out == "(no reply)", (out, q))

out, q, _ = run("make it lovely", [form("lavender", 150, None, "Okay.")])
check("bad values: unknown colour dropped, fan 150 capped to 100",
      q == [("set_fan", {"on": True, "speed": 100}, "user")], q)

out, q, _ = run("make it nice", [form("warm", 0, None, "Okay.")])
check("'warm' -> yellow, fan 0 -> off",
      sorted(q) == sorted([("set_fan", {"on": False}, "user"), ("set_led", {"color": "yellow"}, "user")]), q)

print("\nF3  no change when no change is wanted")
out, q, _ = run("is the fan on?", [form(None, None, None, "Yes, it's at 80%.")])
check("question -> just a reply, nothing changes", q == [] and out == "Yes, it's at 80%.", (out, q))
out, q, _ = run("as usual", [form(None, None, None, "There's no habit for right now. What would you like?")])
check("'as usual' with no habit -> asks, nothing changes", q == [] and "What would you like" in out, (out, q))
out, q, _ = run("fan 60 and make it cosy", [form(None, None, None, "Sure.")])
check("Qwen does nothing -> clear part (fan 60) still runs", q == [("set_fan", {"on": True, "speed": 60}, "user")], q)

print("\nF4  direct commands still skip Qwen")
for text, want in [("u are not calling the tool start the fan", True), ("the fan is still off", False),
                   ("what is the fan speed", False), ("Led is white yet", False)]:
    check(f"parser: '{text}' -> {'command' if want else 'Qwen'}", commands.parse(text, {"fan_on": False})[1] == want)
out, q, _ = run("make it red", [])
check("'make it red' -> instant, no Qwen", q == [("set_led", {"color": "red"}, "user")] and not calls, q)
out, q, _ = run("change colour of led to red and decrease speed of the fan to 50 percent", [])
check("long clear command -> instant, no Qwen", len(q) == 2 and not calls, q)

# ---------------------------------------------------------------------------
print("\nF5  feeling preferences learned from your own choices")
fresh("pref.db", src=None)
t = datetime(2026, 10, 1, 21, 0)


def say(ts, words, feeling, actions):
    cid = bl.log_event("command", "user", utterance=words, before={}, ts=ts)
    if feeling:
        bl.set_feeling(cid, feeling)
    for i, (tool, args) in enumerate(actions):
        bl.log_event("action", "user", tool=tool, args=args, parent_id=cid, ts=ts + timedelta(seconds=i + 1))


# Sad #1: Qwen picked purple, user corrected to blue 2 min later.
say(t, "i'm sad", "sad", [("set_led", {"color": "purple"}), ("set_fan", {"on": True, "speed": 30})])
say(t + timedelta(minutes=2), "no make it blue", None, [("set_led", {"color": "blue"})])
# Sad #2: same thing a day later.
t2 = t + timedelta(days=1)
say(t2, "feeling low today", "sad", [("set_led", {"color": "purple"}), ("set_fan", {"on": True, "speed": 30})])
say(t2 + timedelta(minutes=3), "blue please", None, [("set_led", {"color": "blue"})])
# Sad #3: Qwen now picks blue (it saw the history), user keeps it.
t3 = t + timedelta(days=2)
say(t3, "bad day", "sad", [("set_led", {"color": "blue"}), ("set_fan", {"on": True, "speed": 40})])
# A correction 30 minutes later must NOT count (outside the 10 min window).
say(t3 + timedelta(minutes=30), "make it red", None, [("set_led", {"color": "red"})])
# Stressed once: not enough to be a preference yet.
say(t + timedelta(days=3), "so stressed", "stressed", [("set_led", {"color": "cyan"})])

prefs = pt.compute_preferences()
print("      " + json.dumps(prefs))
sad = next((x for x in prefs if x["feeling"] == "sad"), None)
check("sad -> light blue (your corrections win over Qwen's purple)",
      sad and {"tool": "set_led", "args": {"color": "blue"}} in sad["actions"], sad)
check("sad -> fan 30% (median of 30/30/40)",
      sad and {"tool": "set_fan", "args": {"on": True, "speed": 30}} in sad["actions"], sad)
check("sad counted 3 times", sad and sad["count"] == 3)
check("a change 30 min later is not counted", sad and sad["count"] == 3
      and {"tool": "set_led", "args": {"color": "red"}} not in sad["actions"])
check("stressed (only once) is not a preference yet", not any(x["feeling"] == "stressed" for x in prefs))

print("\nF6  preferences reach Qwen and survive runtime updates")
pt.save_patterns([], preferences=prefs)
ctx = pt.now_context([], datetime(2026, 10, 5, 20, 0), {"fan_on": False, "fan_speed": 0, "led": "off", "present": True})
check("context tells Qwen: sad -> fan 30% and light blue",
      "sad -> fan 30% and light blue (3 times)" in ctx, ctx)
pt.update_pattern("nothing", disabled=True)
check("agent saving a habit answer keeps the preferences", pt.load_preferences() == prefs)

print("\nF7  update_patterns.py writes an accurate patterns.txt")
fresh("up.db")
for ev in [("sad", "blue"), ("sad", "blue")]:
    pass
import update_patterns  # noqa: E402
with contextlib.redirect_stdout(io.StringIO()) as out:
    sys.argv = ["update_patterns.py"]
    update_patterns.main()
txt = pt.PATTERNS_TXT.read_text()
print("      " + txt.replace("\n", "\n      "))
check("lists weekday bedtime correctly (fan 30% and light off, 22:20-23:59)",
      "22:20-23:59 (usually ~23:04): fan 30% and light off" in txt, txt)
check("lists weekday relax correctly (21:00-22:19, light purple)",
      "21:00-22:19 (usually ~21:35): light purple" in txt)
check("no Qwen call without --summary", "Skipped Qwen summary" in out.getvalue())
check("patterns.json has a preferences list", "preferences" in json.loads(pt.PATTERNS_JSON.read_text()))

print("\nF8  update_intents.py records feelings")
fresh("ui.db", src=None)
bl.log_event("command", "user", utterance="i'm so sad today", before={}, ts=datetime(2026, 10, 1, 20, 0))
bl.log_event("command", "user", utterance="fan 30", before={}, ts=datetime(2026, 10, 1, 20, 5))
import update_intents  # noqa: E402
update_intents.ask = lambda items, intents: {"labels": [
    {"n": i + 1, "intent": "ambience", "feeling": "Sad" if "sad" in it["utterance"] else "none"}
    for i, it in enumerate(items)], "new_intents": []}
with contextlib.redirect_stdout(io.StringIO()):
    update_intents.main()
with bl._conn() as c:
    rows = dict(c.execute("SELECT utterance, feeling FROM events").fetchall())
check("'i'm so sad today' -> feeling sad", rows["i'm so sad today"] == "sad", rows)
check("'fan 30' -> no feeling ('none' ignored)", rows["fan 30"] is None, rows)

print("\nF9  a phase-7 database upgrades in place")
old = SP / "old7.db"
for suf in ("", "-wal", "-shm"):
    Path(str(old) + suf).unlink(missing_ok=True)
con = sqlite3.connect(old)
con.executescript((FIX / "phase7_schema.sql").read_text())
con.execute("INSERT INTO events (ts, weekday, day_type, hour, slot, kind, source, utterance)"
            " VALUES ('2026-10-01T20:00:00', 3, 'weekday', 20, 'evening', 'command', 'user', 'hi')")
con.commit()
con.close()
bl.DB_PATH = old
bl.init_db()
with bl._conn() as c:
    cols = [r["name"] for r in c.execute("PRAGMA table_info(events)")]
    kept = c.execute("SELECT utterance FROM events").fetchone()[0]
check("feeling column added", "feeling" in cols, cols)
check("existing rows kept", kept == "hi")

print("\nF10 a suggestion reply with a feeling")
fresh("sg.db")
pt.save_patterns(pt.merge_runtime(pt.compute_patterns(), []), preferences=[])
bl.set_sim_time(datetime(2026, 9, 30, 22, 40))
bl.set_presence(True)
agent.pending = None
agent.ask_qwen = lambda m, schema=None: "Bedtime?"
with contextlib.redirect_stdout(io.StringIO()):
    agent.tick(datetime(2026, 9, 30, 22, 40))
sug = agent.pending
agent.pending = None
agent.ask_qwen = fake([form("blue", None, "sad", "Okay, a calm blue instead.")])
with contextlib.redirect_stdout(io.StringIO()):
    out = agent.answer_suggestion("not really, i'm a bit sad, something calm", sug, [{"role": "system", "content": ""}])
q = queued()
check("vague reply -> Qwen's choice runs as your own command", q == [("set_led", {"color": "blue"}, "user")], (out, q))
check("feeling saved on that correction", last_feeling() == "sad")

bl.set_sim_time(None)
print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
