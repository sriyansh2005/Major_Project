"""'As usual' in code, no-op changes dropped, near-only next habit, warm-up."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

SP = Path(tempfile.mkdtemp(prefix="ha_test_"))
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


db = SP / "u.db"
shutil.copy(ROOT / "data" / "synthetic_events.db", db)
bl.DB_PATH = db
bl.init_db()
pt.PATTERNS_JSON = SP / "u.patterns.json"
pt.PATTERNS_TXT = SP / "u.patterns.txt"
pt.save_patterns(pt.merge_runtime(pt.compute_patterns(), []), preferences=[])
import agent  # noqa: E402

calls = []


def no_qwen(messages, schema=None):
    calls.append(messages)
    raise AssertionError("Qwen should not be called")


def queued():
    with bl._conn() as c:
        rows = [(r["name"], json.loads(r["args"]))
                for r in c.execute("SELECT name, args FROM commands WHERE status='pending'")]
        c.execute("UPDATE commands SET status='done'")
    return rows


def run(text, clock, state, qwen=no_qwen):
    calls.clear()
    bl.set_sim_time(clock)
    bl.set_state(*state)
    agent.ask_qwen = qwen
    with contextlib.redirect_stdout(io.StringIO()):
        out = agent.handle(text, [{"role": "system", "content": agent.build_system_prompt()}])
    return out, queued()


MON_1250 = datetime(2026, 10, 5, 12, 50)
TUE_1840 = datetime(2026, 10, 6, 18, 40)

print("\nU1  'as usual' / 'my pattern' handled in code")
for text, want in [("do according to the pattern", True), ("do it as usual", True), ("the usual please", True),
                   ("my routine", True), ("as usual but blue light", False), ("fan as usual", False),
                   ("start the fan", False)]:
    check(f"is_usual('{text}') = {want}", commands.is_usual(text) == want)

out, q = run("do according to the pattern", MON_1250, (False, 0, "off"))
print(f"      Mon 12:50 reply: {out}")
check("Mon 12:50 (no habit): nothing changes, no Qwen", q == [] and not calls, (q, len(calls)))
check("...and it asks, naming the next habit", "What would you like?" in out and "fan 80%" in out and "17:00" in out, out)

out, q = run("do it as usual", TUE_1840, (False, 0, "off"))
print(f"      Tue 18:40 reply: {out}")
check("Tue 18:40: evening habit (fan 80%) applied, no Qwen", q == [("set_fan", {"on": True, "speed": 80})] and not calls, q)

out, q = run("as usual", TUE_1840, (True, 80, "white"))
check("habit already in place -> says so, nothing changes", q == [] and "already" in out, (out, q))

print("\nU2  values that are already set are dropped")
same = lambda m, schema=None: json.dumps({"light": "white", "fan": 80, "feeling": None,  # noqa: E731
                                          "reply": "The fan is on at 80%."})
out, q = run("is the fan on?", TUE_1840, (True, 80, "white"), same)
check("'is the fan on?' with Qwen echoing fan 80 + white -> nothing changes", q == [] and out == "The fan is on at 80%.", (out, q))
mixed = lambda m, schema=None: json.dumps({"light": "white", "fan": 100, "feeling": "hot",  # noqa: E731
                                           "reply": "Fan up."})
out, q = run("I'm boiling", TUE_1840, (True, 80, "white"), mixed)
check("only the real change (fan 100) is queued, light white dropped", q == [("set_fan", {"on": True, "speed": 100})], q)
check("drop_noops: fan off when already off is dropped",
      agent.drop_noops([("set_fan", {"on": False})], {"fan_on": False, "fan_speed": 0, "led": "off"}) == [])

print("\nU3  next habit is only mentioned when it's close")
ps = pt.load_patterns()
room = {"fan_on": False, "fan_speed": 0, "led": "off", "present": True}
ctx = pt.now_context(ps, MON_1250, room)
check("Mon 12:50: evening habit (4 h away) NOT mentioned", "Next habit" not in ctx, ctx)
check("Mon 12:50: told not to apply any habit", "none (do not apply any habit)" in ctx)
ctx = pt.now_context(ps, datetime(2026, 10, 5, 16, 30), room)
check("Mon 16:30: evening habit at 17:00 IS mentioned", "Next habit: from Monday 17:00" in ctx, ctx)
ctx = pt.now_context(ps, datetime(2026, 10, 3, 23, 0), room)
check("Sat 23:00: weekend bedtime at 00:00 IS mentioned", "Next habit: from Sunday 00:00" in ctx, ctx)

print("\nU4  speed settings")
check("system prompt has no habit list (short, identical every call)",
      agent.build_system_prompt() == agent.SYSTEM_PROMPT and "Weekdays:" not in agent.build_system_prompt())
sent = {}


class Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": "{}"}}


def post(url, json=None, timeout=None):
    sent.update(json=json, timeout=timeout)
    return Resp()


real_post = agent.requests.post
agent.requests.post = post
import importlib  # noqa: E402
importlib.reload(agent)
agent.requests.post = post
agent.ask_qwen([{"role": "user", "content": "x"}], agent.DECISION_SCHEMA)
check("model kept loaded between messages (keep_alive)", sent["json"].get("keep_alive") == agent.KEEP_LOADED)
check("time limit raised to 300 s", sent["timeout"] == 300)
check("reply length capped (num_predict)", sent["json"]["options"].get("num_predict") == 160)


def down(*a, **k):
    raise requests.ConnectionError("no ollama")


agent.requests.post = down
with contextlib.redirect_stdout(io.StringIO()) as out:
    agent.warm_up()
check("warm-up without Ollama prints a hint instead of crashing", "Clear commands still work" in out.getvalue())
agent.requests.post = real_post
bl.set_sim_time(None)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
