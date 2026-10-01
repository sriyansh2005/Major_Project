"""
Agent: natural language -> Qwen (via Ollama) -> queued hardware commands,
plus proactive suggestions from learned habits (Phase 7).

    python agent.py                      # chat (with habit suggestions)
    python agent.py "fan to 50"          # one-shot command, no suggestions

What it knows about you comes from patterns.txt (loaded into the prompt).
A background checker reads patterns.json every CHECK_EVERY seconds; when one
of your habits is due and someone is in the room, it either asks you in the
chat or, for habits you've approved enough times, does it and tells you.
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
from system.tool_schemas import TOOLS
from system.behaviour_log import (
    init_db,
    enqueue_command,
    get_state,
    log_command,
    log_event,
    now,
)

# --- Config ------------------------------------------------------------------
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen2.5:3b"
CHECK_EVERY = 30          # seconds between habit checks
REASK_MIN = 5             # ask again after a "no" / no answer (set to 20 after testing)
MAX_ASKS = 3              # per habit per day
PENDING_TTL = REASK_MIN * 60   # an unanswered suggestion expires before the re-ask
MAX_HISTORY = 12          # chat turns kept in the prompt (keeps Qwen fast)

# The agent does NOT touch GPIO. Tool calls are queued in the DB and the
# controller applies them (and logs the action linked via parent_id).
TOOL_NAMES = {"set_fan", "set_led"}

SYSTEM_PROMPT = (
    "You are a home assistant on a Raspberry Pi controlling a fan and an RGB LED.\n"
    "Tools: set_fan (on/off, speed 0-100: low=30, half=50, high=100) and "
    "set_led (red, green, blue, yellow, cyan, purple, magenta, white, off).\n"
    "Rules, in this order:\n"
    "1. The user's direct request always wins. If they ask to change the fan or "
    "the light, call the tool and do exactly that, right now, whatever their "
    "habits or the time of day. Never refuse, delay or argue.\n"
    "2. To change anything you MUST call a tool. Never say you changed "
    "something without calling the tool.\n"
    "3. Habits are background only. Use them only when the user asks for 'the "
    "usual', 'as usual', 'my pattern' or is vague ('I'm hot'). If there is no "
    "habit for right now, ask the user what they would like. Never invent a "
    "habit that is not listed.\n"
    "4. Otherwise reply in one or two short, friendly sentences."
)

NUDGE = ("You described a change but did not call a tool, so nothing happened. "
         "If the user wants a change, call set_fan / set_led now. Otherwise just reply.")
CLAIMS_ACTION = re.compile(
    r"\b(i'?ll|i will|i'?ve|i have|setting|turning|switching|starting|now)\b.*\b(fan|light|led)\b"
    r"|\b(fan|light|led)\b.*\b(set to|turned|switched|is now)\b")

# Plain answers to a suggestion are handled without Qwen (fast and can't be misread).
YES = {"yes", "yeah", "yea", "yep", "yup", "ya", "ok", "okay", "sure", "do it",
       "go ahead", "please", "yes please", "sounds good", "alright", "fine", "y"}
NO = {"no", "nah", "nope", "not now", "no thanks", "no thank you", "later",
      "skip", "n", "leave it", "don't", "dont"}

ANSWER_RULES = (
    "You just suggested this to the user: \"{question}\" "
    "(suggested actions: {actions}). Read their reply.\n"
    "- If they agree, call the tools for exactly the suggested actions.\n"
    "- If they want something different, call the tools for what they want.\n"
    "- If they decline, call no tools and reply in one short sentence."
)

lock = threading.Lock()
pending = None            # the suggestion waiting for an answer


# --- Qwen --------------------------------------------------------------------

def build_system_prompt() -> str:
    return f"{SYSTEM_PROMPT}\n\nWhat you know about this user:\n{pt.load_profile()}"


def ask_qwen(messages: list, tools: bool = True) -> dict:
    payload = {"model": MODEL, "messages": messages}
    if tools:
        payload["tools"] = TOOLS
    resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def tool_calls(msg: dict) -> list:
    """[(name, args), ...] from a Qwen reply, ignoring unknown tools."""
    calls = []
    for call in msg.get("tool_calls") or []:
        name = call["function"]["name"]
        args = call["function"]["arguments"]
        args = json.loads(args) if isinstance(args, str) else (args or {})
        if name in TOOL_NAMES:
            calls.append((name, args))
    return calls


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


def ask_with_retry(messages: list) -> tuple:
    """Ask Qwen; if it claims a change but called no tool, remind it once."""
    msg = ask_qwen(messages)
    calls = tool_calls(msg)
    content = (msg.get("content") or "").lower()
    if not calls and CLAIMS_ACTION.search(content):
        msg = ask_qwen(messages + [{"role": "assistant", "content": msg.get("content") or ""},
                                   {"role": "user", "content": NUDGE}])
        calls = tool_calls(msg)
    return msg, calls


def handle(user_text: str, history: list) -> str:
    """One turn: log the user's words, let Qwen pick tools, queue them."""
    state = get_state()
    cmd_id = log_command(user_text, state)
    history.append({"role": "user", "content": user_text})
    trim(history)

    # 1) A clear direct command runs right away, no Qwen needed.
    direct, sure = commands.parse(user_text, state)
    if sure:
        history.append({"role": "assistant", "content": "Done."})
        return "Done: " + "; ".join(queue(direct, cmd_id, "user"))

    # 2) Otherwise Qwen decides (history keeps the plain text; only this call
    #    gets the live context).
    msg, calls = ask_with_retry(history[:-1] + [with_context(user_text)])
    history.append({"role": "assistant", "content": msg.get("content") or ""})

    # 3) Safety net: Qwen did nothing, but part of the message was clear.
    if not calls and direct:
        calls = direct
    if not calls:
        return msg.get("content") or "(no reply)"
    return "Done: " + "; ".join(queue(calls, cmd_id, "user"))


# --- Proactive suggestions ---------------------------------------------------

def phrase_question(p: dict) -> str:
    """Let Qwen word the suggestion like a chat; fall back to a template."""
    fallback = (f"You usually set {pt.describe_actions(p['actions'])} around "
                f"{p['typical_time']} ({p['intent'].replace('_', ' ')}). Want me to do that now?")
    try:
        msg = ask_qwen([
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": (
                "Write ONE short, friendly question offering to do this for the user "
                f"now: {pt.describe_actions(p['actions'])}. Reason: it's {p['day_type']} "
                f"{p['slot']} and they usually do this around {p['typical_time']} "
                f"({p['intent'].replace('_', ' ')}). Only the question.")},
        ], tools=False)
        text = (msg.get("content") or "").strip().strip('"')
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
        msg, calls = {"content": ""}, direct
    else:
        # Anything else ("no, make it cosy") -> let Qwen work out what they want.
        msg, calls = ask_with_retry([
            {"role": "system", "content": build_system_prompt() + "\n\n" + ANSWER_RULES.format(
                question=sug["question"], actions=pt.describe_actions(p["actions"]))},
            with_context(reply),
        ])
        if not calls and direct:
            calls = direct

    if not calls:
        declined()
        feedback("rejected")
        return msg.get("content") or "Okay, leaving it as it is."

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
