"""
Agent: natural language -> queued hardware commands, plus proactive
suggestions from learned habits (Phase 7).

    python agent.py                      # chat (with habit suggestions)
    python agent.py "fan to 50"          # one-shot command, no suggestions

How a message is handled:
  1. Clear commands ("start the fan", "light blue") are read by
     system/commands.py and run instantly, no Qwen.
  2. Everything else goes to Qwen, which answers by filling in a JSON form
     {light, fan, feeling, reply}. A 3B model fills a form far more reliably
     than it makes tool calls, so "I'm sad, set the mood" actually changes
     the room instead of just replying "Done."
  3. A background checker suggests habits from patterns.json when they are due.
"""

import json
import re
import sys
import threading
import time
from datetime import datetime

import requests

from system import commands
from system import patterns as pt
from system.tool_schemas import COLOR_NAMES
from system.behaviour_log import (
    init_db,
    enqueue_command,
    get_state,
    log_command,
    log_event,
    now,
    set_feeling,
)

# --- Config ------------------------------------------------------------------
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"
CHECK_EVERY = 30          # seconds between habit checks
REASK_MIN = 5             # ask again after a "no" / no answer (set to 20 after testing)
MAX_ASKS = 3              # per habit per day
PENDING_TTL = REASK_MIN * 60   # an unanswered suggestion expires before the re-ask
MAX_HISTORY = 6           # chat turns kept in the prompt (keeps Qwen fast on the Pi)
QWEN_TIMEOUT = 300        # seconds; the Pi is slow, especially the first call
KEEP_LOADED = "60m"       # keep the model in memory between messages

SYSTEM_PROMPT = """You are a home assistant on a Raspberry Pi controlling a fan and an RGB light.
Answer every message by filling in this JSON form:
  "light":   one of red, green, blue, yellow, cyan, purple, magenta, white, off;
             or null to leave the light as it is
  "fan":     0 (off) to 100 (low=30, half=50, high=100); or null to leave it
  "feeling": the user's feeling if they express one, one lowercase word
             (sad, stressed, tired, happy, hot, cold, ...); else null
  "reply":   one or two short, kind sentences to the user saying what you changed
Rules, in this order:
1. The user's direct request always wins. Set exactly what they ask, now,
   whatever their habits or the time of day. Never refuse or delay.
2. If they mention a feeling or a mood, change the room to suit it. If the
   background lists what they chose before for that feeling, use that.
   Otherwise choose what you think fits and say so; they can correct you.
   The light cannot dim: "warm" means yellow, "cool" means blue or cyan.
3. If they complain that something did not change, set it now.
4. Habits are background only. Use them when they say "as usual", "my
   pattern" or something vague. If there is no habit for right now, ask what
   they would like and leave light and fan null. Never invent a habit.
5. For questions or chat, leave light and fan null and just reply."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "light": {"type": ["string", "null"]},
        "fan": {"type": ["integer", "null"]},
        "feeling": {"type": ["string", "null"]},
        "reply": {"type": "string"},
    },
    "required": ["light", "fan", "feeling", "reply"],
}

LIGHT_ALIAS = {**commands.COLOR_ALIAS, "warm": "yellow", "cool": "blue", "none": "off"}
NUDGE = ('Your reply says you changed something, but "light" and "fan" are both '
         "null, so nothing happened. Fill in the values you meant.")
CLAIMS = re.compile(r"\b(set|setting|turned|turning|switched|switching|changed|changing|made|"
                    r"making|started|starting|i'?ll|i will|i'?ve|done)\b")
DEVICE = re.compile(r"\b(fan|light|led|lamp|colou?r|" + "|".join(COLOR_NAMES) + r")\b")

# Plain answers to a suggestion are handled without Qwen (fast and can't be misread).
YES = {"yes", "yeah", "yea", "yep", "yup", "ya", "ok", "okay", "sure", "do it",
       "go ahead", "please", "yes please", "sounds good", "alright", "fine", "y"}
NO = {"no", "nah", "nope", "not now", "no thanks", "no thank you", "later",
      "skip", "n", "leave it", "don't", "dont"}

ANSWER_RULES = """You just suggested this to the user: "{question}"
(suggested: {actions}). Read their reply.
- If they agree, fill in exactly the suggested values.
- If they want something different, fill in what they want.
- If they decline, leave light and fan null and reply in one short sentence."""

lock = threading.Lock()
pending = None            # the suggestion waiting for an answer


# --- Qwen --------------------------------------------------------------------

def build_system_prompt() -> str:
    # Kept short and identical between calls: the Pi reads every prompt token,
    # and Ollama can reuse a prompt start it has already read. The live facts
    # (time, room, habit now, feelings) come with each message instead.
    return SYSTEM_PROMPT


def ask_qwen(messages: list, schema: dict = None) -> str:
    """One Qwen call via Ollama. With a schema the reply is forced to valid JSON."""
    payload = {"model": MODEL, "messages": messages, "stream": False,
               "keep_alive": KEEP_LOADED,
               "options": {"temperature": 0.2, "num_predict": 160}}
    if schema:
        payload["format"] = schema
    resp = requests.post(OLLAMA_URL, json=payload, timeout=QWEN_TIMEOUT)
    if schema and resp.status_code >= 400:      # Ollama can't use the schema: plain JSON mode
        payload["format"] = "json"
        resp = requests.post(OLLAMA_URL, json=payload, timeout=QWEN_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def parse_decision(text: str) -> dict:
    """Turn Qwen's JSON form into checked actions. Bad values are dropped."""
    data = json.loads(text)
    actions = []
    fan = data.get("fan")
    if isinstance(fan, (int, float)) and not isinstance(fan, bool):
        fan = max(0, min(100, int(fan)))
        actions.append(("set_fan", {"on": False} if fan == 0 else {"on": True, "speed": fan}))
    light = str(data.get("light") or "").strip().lower()
    light = LIGHT_ALIAS.get(light, light)
    if light in COLOR_NAMES:
        actions.append(("set_led", {"color": light}))
    feeling = re.sub(r"[^a-z]", "", str(data.get("feeling") or "").lower())
    return {"actions": actions,
            "feeling": None if feeling in ("", "none", "null", "neutral") else feeling,
            "reply": str(data.get("reply") or "").strip()}


def decide(messages: list) -> dict:
    """Ask Qwen to fill in the form. Retries once on broken JSON, and once if
    the reply claims a change while light and fan are both null."""
    for _ in range(2):
        try:
            d = parse_decision(ask_qwen(messages, DECISION_SCHEMA))
            break
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
    else:
        return {"actions": [], "feeling": None, "reply": ""}

    reply = d["reply"].lower()
    if not d["actions"] and CLAIMS.search(reply) and DEVICE.search(reply):
        try:
            retry = parse_decision(ask_qwen(
                messages + [{"role": "assistant", "content": json.dumps(
                    {"light": None, "fan": None, "feeling": d["feeling"], "reply": d["reply"]})},
                            {"role": "user", "content": NUDGE}], DECISION_SCHEMA))
            if retry["actions"]:
                retry["feeling"] = retry["feeling"] or d["feeling"]
                d = retry
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return d


def warm_up():
    """Load Qwen and read the system prompt once at start, so the user's
    first message doesn't wait (or time out) on that."""
    t0 = time.monotonic()
    print("Loading Qwen (first time can take a minute on the Pi)...", flush=True)
    try:
        ask_qwen([{"role": "system", "content": build_system_prompt()},
                  {"role": "user", "content": "User says: hello"}], DECISION_SCHEMA)
        print(f"Qwen ready ({time.monotonic() - t0:.0f}s).")
    except requests.RequestException as e:
        print(f"Qwen didn't answer ({e}). Clear commands still work; is Ollama running?")


def drop_noops(calls: list, state: dict) -> list:
    """Remove 'changes' that set what the device already has (fan 80 -> 80)."""
    out = []
    for name, args in calls:
        if name == "set_fan":
            speed = args.get("speed", 100) if args.get("on", True) else 0
            current = state["fan_speed"] if state["fan_on"] else 0
            if speed == current:
                continue
        if name == "set_led" and args.get("color") == state["led"]:
            continue
        out.append((name, args))
    return out


def queue(calls: list, parent_id: int, source: str) -> list:
    done = []
    for name, args in calls:
        enqueue_command(name, args, parent_id=parent_id, source=source)
        print(f"  [{source}] {name}({args}) queued")
        done.append(f"{name} {args}")
    return done


def trim(history: list):
    """Keep the system prompt + the last MAX_HISTORY messages."""
    if len(history) > MAX_HISTORY + 1:
        del history[1:len(history) - MAX_HISTORY]


# --- Normal commands ---------------------------------------------------------

def with_context(text: str) -> dict:
    """The user's message, prefixed with what's true right now."""
    ctx = pt.now_context(pt.load_patterns(), now(), get_state())
    return {"role": "user", "content": (
        "Background (habits only matter for 'as usual' requests; a direct "
        f"request always wins):\n{ctx}\n\nUser says: {text}")}


def handle(user_text: str, history: list) -> str:
    """One turn: log the user's words, decide what to change, queue it."""
    state = get_state()
    cmd_id = log_command(user_text, state)
    history.append({"role": "user", "content": user_text})
    trim(history)

    # 1) A clear direct command runs right away, no Qwen needed.
    direct, sure = commands.parse(user_text, state)
    if sure:
        history.append({"role": "assistant", "content": "Done."})
        return "Done: " + "; ".join(queue(direct, cmd_id, "user"))

    # 2) "As usual" / "my pattern": done from the learned habits, no Qwen.
    if commands.is_usual(user_text):
        reply = do_usual(cmd_id, state)
        history.append({"role": "assistant", "content": reply})
        return reply

    # 3) Otherwise Qwen fills in the form (only this call gets the live context).
    d = decide(history[:-1] + [with_context(user_text)])
    if d["feeling"]:
        set_feeling(cmd_id, d["feeling"])
    history.append({"role": "assistant", "content": d["reply"] or "Okay."})

    # 4) Drop values that are already set; if Qwen changed nothing but part of
    #    the message was clear, do that part.
    calls = drop_noops(d["actions"], state) or direct
    if not calls:
        return d["reply"] or "(no reply)"
    done = "Done: " + "; ".join(queue(calls, cmd_id, "user"))
    return f"{d['reply']}\n{done}" if d["reply"] else done


def do_usual(cmd_id: int, state: dict) -> str:
    """Apply the habit for right now, or say there isn't one and ask."""
    patterns, clock = pt.load_patterns(), now()
    habit = pt.habit_now(patterns, clock)
    if habit:
        calls = drop_noops([(a["tool"], a["args"]) for a in habit["actions"]], state)
        if not calls:
            return f"That's already your usual for now ({pt.describe_actions(habit['actions'])})."
        queue(calls, cmd_id, "user")
        return f"Your usual for now: {pt.describe_actions(habit['actions'])}."
    nxt = pt.next_habit(patterns, clock)
    later = (f" Your next one is {pt.describe_actions(nxt[1]['actions'])} from "
             f"{nxt[0]:%A %H:%M}." if nxt else "")
    return f"You don't have a usual setting for right now.{later} What would you like?"


# --- Proactive suggestions ---------------------------------------------------

def phrase_question(p: dict) -> str:
    """Let Qwen word the suggestion like a chat; fall back to a template."""
    fallback = (f"You usually set {pt.describe_actions(p['actions'])} around "
                f"{p['typical_time']} ({p['intent'].replace('_', ' ')}). Want me to do that now?")
    try:
        text = ask_qwen([
            {"role": "user", "content": (
                "You are a friendly home assistant. Write ONE short question offering "
                f"to do this for the user now: {pt.describe_actions(p['actions'])}. "
                f"Reason: it's {p['day_type']} {p['slot']} and they usually do this "
                f"around {p['typical_time']} ({p['intent'].replace('_', ' ')}). "
                "Only the question, nothing else.")},
        ]).strip().strip('"')
        return text if 0 < len(text) < 200 else fallback
    except requests.RequestException:
        return fallback


def tick(now: datetime):
    """Fire at most one due habit: ask about it, or do it if it's trusted."""
    global pending
    with lock:
        if pending and time.monotonic() - pending["at"] > PENDING_TTL:
            pending = None                      # expired, nobody answered
        if pending:
            return

    if not get_state()["present"]:
        return                                  # nobody here to ask
    due = pt.due_patterns(pt.load_patterns(), now, REASK_MIN, MAX_ASKS)
    if not due:
        return
    p = due[0]

    if pt.can_auto(p):
        pt.update_pattern(p["id"], done_date=now.date().isoformat())
        eid = log_event("suggestion", "auto", utterance=f"auto: {p['id']}",
                        args={"pattern": p["id"], "mode": "auto"})
        queue([(a["tool"], a["args"]) for a in p["actions"]], eid, "auto")
        print(f"\n[auto] Set {pt.describe_actions(p['actions'])}, like you usually do "
              f"around {p['typical_time']}. Just tell me if you want something else.\n> ",
              end="", flush=True)
        return

    pt.record_ask(p, now)
    question = phrase_question(p)
    eid = log_event("suggestion", "auto", utterance=question,
                    args={"pattern": p["id"], "mode": "ask"})
    with lock:
        pending = {"pattern": p, "question": question, "event_id": eid, "at": time.monotonic()}
    print(f"\n[suggestion] {question}\n> ", end="", flush=True)


def same_actions(calls: list, actions: list) -> bool:
    """Did Qwen do what was suggested? (fan speed within 10% counts as same)"""
    got = {name: args for name, args in calls}
    want = {a["tool"]: a["args"] for a in actions}
    if set(got) != set(want):
        return False
    for tool, w in want.items():
        g = got[tool]
        if tool == "set_led" and g.get("color") != w.get("color"):
            return False
        if tool == "set_fan":
            if bool(g.get("on", True)) != bool(w.get("on", True)):
                return False
            if w.get("on") and abs(g.get("speed", 100) - w.get("speed", 100)) > 10:
                return False
    return True


def answer_suggestion(reply: str, sug: dict, history: list) -> str:
    """Treat the user's reply as an answer: approve, decline, correct, or mute."""
    p = sug["pattern"]
    history += [{"role": "assistant", "content": sug["question"]},
                {"role": "user", "content": reply}]
    trim(history)

    def feedback(verdict):
        log_event("feedback", "user", utterance=reply, parent_id=sug["event_id"],
                  args={"pattern": p["id"], "verdict": verdict})

    if any(k in reply.lower() for k in ("never", "don't ask", "dont ask", "stop asking")):
        pt.update_pattern(p["id"], disabled=True)
        feedback("never")
        return "Got it, I won't suggest that again."

    current = next((x for x in pt.load_patterns() if x["id"] == p["id"]), p)
    today = now().date().isoformat()

    def declined():
        # Counted once per day: "not yet" at 17:05 isn't "I never want this".
        if current.get("rejected_date") != today:
            pt.update_pattern(p["id"], rejections=current["rejections"] + 1,
                              rejected_date=today)

    def accepted():
        pt.update_pattern(p["id"], approvals=current["approvals"] + 1, done_date=today)

    plain = reply.lower().strip(" .!?,")
    suggested = [(a["tool"], a["args"]) for a in p["actions"]]

    if plain in YES:
        accepted()
        feedback("approved")
        return "Done: " + "; ".join(queue(suggested, sug["event_id"], "auto"))
    if plain in NO:
        declined()
        feedback("rejected")
        if current.get("asks", 0) < MAX_ASKS:
            return f"Okay, leaving it. I'll ask again in {REASK_MIN} min."
        return "Okay, leaving it. I won't ask again today."

    # A clear direct command ("no, fan 60") is done exactly, no Qwen needed.
    direct, sure = commands.parse(reply, get_state())
    if sure:
        d = {"actions": direct, "feeling": None, "reply": ""}
    else:
        # Anything else ("no, make it cosy") -> let Qwen work out what they want.
        d = decide([
            {"role": "system", "content": build_system_prompt() + "\n\n" + ANSWER_RULES.format(
                question=sug["question"], actions=pt.describe_actions(p["actions"]))},
            with_context(reply),
        ])
    calls = d["actions"] or direct

    if not calls:
        declined()
        feedback("rejected")
        return d["reply"] or "Okay, leaving it as it is."

    if same_actions(calls, p["actions"]):
        # Approved: done as auto (it was the system's idea), counted as an approval.
        accepted()
        feedback("approved")
        return "Done: " + "; ".join(queue(calls, sug["event_id"], "auto"))

    # Corrected: the user wanted something else. That IS the user's own choice,
    # so it's logged as a user command and will be learned from next time.
    declined()
    pt.update_pattern(p["id"], done_date=today)      # they chose; stop asking today
    feedback("corrected")
    cmd_id = log_command(reply, get_state())
    if d["feeling"]:
        set_feeling(cmd_id, d["feeling"])
    return "Okay, instead: " + "; ".join(queue(calls, cmd_id, "user"))


def checker(stop: threading.Event):
    while not stop.wait(CHECK_EVERY):
        try:
            tick(now())
        except Exception as e:                  # never let the checker die
            print(f"\n[checker] error: {e}\n> ", end="", flush=True)


# --- Main --------------------------------------------------------------------

def main():
    global pending
    init_db()
    history = [{"role": "system", "content": build_system_prompt()}]

    if len(sys.argv) > 1:                       # one-shot, no suggestions
        print(handle(" ".join(sys.argv[1:]), history))
        return

    warm_up()
    stop = threading.Event()
    threading.Thread(target=checker, args=(stop,), daemon=True).start()
    n = len(pt.load_patterns())
    print(f"Home assistant ready ({n} learned habits). Type a command ('quit' to exit).")

    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        with lock:
            sug, pending = pending, None
        try:
            print(answer_suggestion(text, sug, history) if sug else handle(text, history))
        except requests.RequestException as e:
            print(f"Qwen didn't answer ({e}). Is Ollama running?")
    stop.set()


if __name__ == "__main__":
    main()
