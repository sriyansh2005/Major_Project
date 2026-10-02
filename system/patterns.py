"""
Phase 7: turn the user's commands into habits.

How a habit is found
  1. Take every user command (source='user'), with what it did to each device.
     Skip commands followed by the PIR reporting the room empty within
     LEAVE_WINDOW_MIN: that was the user leaving, which the PIR handles.
  2. Per weekday/weekend + slot + device, group similar results:
       fan  -> off, or speeds close to each other (30/40/50 = one group)
       LED  -> colour family (blue/purple = "calm")
     Counting what the devices did (not Qwen's intent label) means a wrong
     label can't split one habit into pieces that each fall under the floor.
  3. Keep a group if it happened on >= MIN_CONFIDENCE of matching days
     (last RECENT_DAYS count double) and >= MIN_COUNT times.
  4. Groups in the same slot whose usual times are within MERGE_MIN become one
     habit ("light off + fan 30%"). Max MAX_PER_SLOT habits per slot.
  5. A habit is valid for its whole slot. If a slot has several, the slot is
     split at the midpoints between their usual times.

Qwen's intent label is kept only for wording ("sleep_prep").

Files (project root):
  patterns.json  habits for code, plus runtime fields the agent updates
                 (approvals, rejections, disabled, and today's asks).
  patterns.txt   plain-English profile written by Qwen, loaded into the prompt.
"""

import bisect
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from system.behaviour_log import SLOTS, _conn, day_type_for, slot_for

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_JSON = ROOT / "patterns.json"
PATTERNS_TXT = ROOT / "patterns.txt"

# Learning
RECENT_DAYS = 21          # the last 3 weeks...
RECENT_WEIGHT = 2.0       # ...count double
MIN_COUNT = 3             # floor: seen at least this many times
MIN_CONFIDENCE = 0.6      # floor: happened on at least 60% of matching days
MAX_PER_SLOT = 3          # keep the prompt small so Qwen doesn't get confused
MERGE_MIN = 30            # device groups this close in time = one habit
LEAVE_WINDOW_MIN = 10     # command + room empty within 10 min = leaving
FEELING_WINDOW_MIN = 10   # changes within 10 min of a "feeling" request = what they chose
MIN_FEELING_COUNT = 2     # a feeling preference needs this many occurrences
NEXT_HABIT_MIN = 60       # only tell Qwen about the next habit if it starts this soon
FAN_GAP = 25              # fan speeds further apart than this = different group
FAN_SPAN = 40             # ...and one group never spans more than this

# Colour families: colours in the same family count as the same habit.
LED_FAMILY = {
    "off": "off",
    "blue": "calm", "purple": "calm", "cyan": "calm", "magenta": "calm",
    "white": "bright", "yellow": "bright",
    "red": "vivid", "green": "vivid",
}

# Acting
AUTO_CONFIDENCE = 0.9     # auto-execute only if all three hold
AUTO_MIN_COUNT = 5
AUTO_MIN_APPROVALS = 3
MUTE_AFTER = 3            # stop asking once rejections - approvals reaches this

RUNTIME_FIELDS = {
    "approvals": 0, "rejections": 0, "disabled": False,
    "ask_date": None,       # the day the counters below belong to
    "asks": 0,              # questions asked on ask_date
    "last_asked": None,     # when the last question was asked (system clock)
    "done_date": None,      # day it was accepted / auto-run / corrected: stop asking
    "rejected_date": None,  # day a "no" was already counted (max one per day)
}
SLOT_RANGE = {name: (lo * 60, hi * 60 + 59) for name, lo, hi in SLOTS}


def hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def fan_band(speed: int) -> str:
    if speed <= 0:
        return "off"
    return "low" if speed <= 40 else "medium" if speed <= 70 else "high"


# --- Learning ----------------------------------------------------------------

def _load_occurrences() -> list:
    """One entry per (user command, device it changed), leaving commands removed."""
    with _conn() as c:
        cmds = c.execute(
            """
            SELECT e.id, e.ts, e.day_type, e.slot, i.name AS intent
            FROM events e LEFT JOIN intents i ON i.id = e.intent_id
            WHERE e.kind = 'command' AND e.source = 'user'
            """
        ).fetchall()
        acts = c.execute(
            "SELECT parent_id, tool, args FROM events"
            " WHERE kind = 'action' AND parent_id IS NOT NULL ORDER BY id"
        ).fetchall()
        absent = sorted(
            datetime.fromisoformat(r["ts"]) for r in c.execute(
                "SELECT ts FROM events WHERE kind = 'presence' AND utterance = 'absent'")
        )

    by_parent = defaultdict(dict)                 # last change per device wins
    for a in acts:
        by_parent[a["parent_id"]][a["tool"]] = json.loads(a["args"] or "{}")

    occ = []
    for r in cmds:
        ts = datetime.fromisoformat(r["ts"])
        i = bisect.bisect_right(absent, ts)
        if i < len(absent) and absent[i] - ts <= timedelta(minutes=LEAVE_WINDOW_MIN):
            continue                              # user left; the PIR handles that
        base = {"date": ts.date(), "minute": ts.hour * 60 + ts.minute,
                "day_type": r["day_type"], "slot": r["slot"], "intent": r["intent"]}
        for tool, args in by_parent.get(r["id"], {}).items():
            if tool == "set_fan":
                on = args.get("on", True)
                speed = int(args.get("speed", 100)) if on else 0
                occ.append({**base, "device": "fan", "value": speed})
            elif tool == "set_led" and args.get("color") in LED_FAMILY:
                occ.append({**base, "device": "led", "value": args["color"]})
    return occ


def _fan_groups(occs: list) -> list:
    """Split fan results into off + clusters of close speeds."""
    groups = []
    off = [o for o in occs if o["value"] == 0]
    if off:
        groups.append(off)
    cur = []
    for o in sorted((o for o in occs if o["value"] > 0), key=lambda o: o["value"]):
        if cur and (o["value"] - cur[-1]["value"] > FAN_GAP
                    or o["value"] - cur[0]["value"] > FAN_SPAN):
            groups.append(cur)
            cur = []
        cur.append(o)
    if cur:
        groups.append(cur)
    return groups


def _led_groups(occs: list) -> list:
    fam = defaultdict(list)
    for o in occs:
        fam[LED_FAMILY[o["value"]]].append(o)
    return list(fam.values())


def compute_patterns() -> list:
    """Build the habit list from events.db (does not touch runtime fields)."""
    occ = _load_occurrences()
    if not occ:
        return []

    first = min(o["date"] for o in occ)
    ref = max(o["date"] for o in occ)

    def weight(day):
        return RECENT_WEIGHT if (ref - day).days < RECENT_DAYS else 1.0

    covered = defaultdict(float)                  # weighted weekdays / weekend days
    day = first
    while day <= ref:
        covered[day_type_for(day.weekday())] += weight(day)
        day += timedelta(days=1)

    by_key = defaultdict(list)
    for o in occ:
        by_key[(o["day_type"], o["slot"], o["device"])].append(o)

    # Step 2 + 3: device groups that pass the floor.
    parts = []
    for (day_type, slot, device), occs in by_key.items():
        groups = _fan_groups(occs) if device == "fan" else _led_groups(occs)
        for g in groups:
            days = {o["date"] for o in g}
            if len(g) < MIN_COUNT:
                continue
            confidence = min(1.0, sum(weight(d) for d in days) / covered[day_type])
            if confidence < MIN_CONFIDENCE:
                continue
            if device == "fan":
                speed = int(statistics.median(o["value"] for o in g))
                action = ({"tool": "set_fan", "args": {"on": True, "speed": speed}}
                          if speed > 0 else {"tool": "set_fan", "args": {"on": False}})
                label = fan_band(speed)
            else:
                recent = [o["value"] for o in g if weight(o["date"]) > 1] or [o["value"] for o in g]
                color = Counter(recent).most_common(1)[0][0]
                action = {"tool": "set_led", "args": {"color": color}}
                label = LED_FAMILY[color]
            parts.append({
                "day_type": day_type, "slot": slot, "device": device, "label": label,
                "minute": int(statistics.median(o["minute"] for o in g)),
                "action": action, "confidence": confidence, "count": len(g),
                "days": len(days), "last": max(o["date"] for o in g),
                "intents": [o["intent"] for o in g if o["intent"]],
            })

    # Step 4: merge device groups used together, then cap per slot.
    by_slot = defaultdict(list)
    for p in parts:
        by_slot[(p["day_type"], p["slot"])].append(p)

    habits = []
    for (day_type, slot), lst in by_slot.items():
        merged = []
        for p in sorted(lst, key=lambda p: p["minute"]):
            for m in merged:
                if (p["minute"] - m[0]["minute"] <= MERGE_MIN
                        and p["device"] not in {x["device"] for x in m}):
                    m.append(p)
                    break
            else:
                merged.append([p])

        slot_habits = []
        for m in merged:
            m.sort(key=lambda p: p["device"])
            intents = Counter(i for p in m for i in p["intents"])
            minute = int(statistics.median(p["minute"] for p in m))
            slot_habits.append({
                "id": f"{day_type}:{slot}:" + "+".join(f"{p['device']}-{p['label']}" for p in m),
                "day_type": day_type,
                "slot": slot,
                "intent": intents.most_common(1)[0][0] if intents else "habit",
                "typical_minute": minute,
                "typical_time": hhmm(minute),
                "actions": [p["action"] for p in m],
                "confidence": round(min(p["confidence"] for p in m), 2),
                "count": min(p["count"] for p in m),
                "days_seen": min(p["days"] for p in m),
                "last_seen": max(p["last"] for p in m).isoformat(),
            })
        slot_habits = sorted(slot_habits, key=lambda h: -h["confidence"])[:MAX_PER_SLOT]

        # Step 5: whole slot, split at midpoints when the slot has several habits.
        slot_habits.sort(key=lambda h: h["typical_minute"])
        lo, hi = SLOT_RANGE[slot]
        for i, h in enumerate(slot_habits):
            start = lo if i == 0 else (slot_habits[i - 1]["typical_minute"] + h["typical_minute"]) // 2 + 1
            end = hi if i == len(slot_habits) - 1 else (h["typical_minute"] + slot_habits[i + 1]["typical_minute"]) // 2
            h.update(valid_from=start, valid_to=end, window=f"{hhmm(start)}-{hhmm(end)}")
        habits += slot_habits

    order = {name: i for i, (name, _, _) in enumerate(SLOTS)}
    return sorted(habits, key=lambda h: (h["day_type"], order[h["slot"]], h["valid_from"]))


def compute_preferences() -> list:
    """What the user ends up choosing when they mention a feeling.

    For each request with a feeling (e.g. "I'm sad"), take the room state the
    user settled on in the next FEELING_WINDOW_MIN minutes, i.e. Qwen's choice
    plus any correction ("no, make it blue"). Then, per feeling, keep the most
    common outcome. Learned from the user's own choices, not a fixed table.
    """
    with _conn() as c:
        cmds = c.execute(
            "SELECT id, ts, feeling FROM events WHERE kind = 'command'"
            " AND source = 'user' AND feeling IS NOT NULL ORDER BY ts"
        ).fetchall()
        acts = c.execute(
            "SELECT ts, parent_id, tool, args FROM events"
            " WHERE kind = 'action' AND source = 'user' ORDER BY ts, id"
        ).fetchall()
    acts = [(datetime.fromisoformat(a["ts"]), a["parent_id"], a["tool"], json.loads(a["args"] or "{}"))
            for a in acts]

    outcomes = defaultdict(list)                  # feeling -> [{device: value}]
    for cmd in cmds:
        start = datetime.fromisoformat(cmd["ts"])
        end = start + timedelta(minutes=FEELING_WINDOW_MIN)
        final = {}
        for ts, parent, tool, args in acts:
            if parent == cmd["id"] or start < ts <= end:
                if tool == "set_fan":
                    final["fan"] = int(args.get("speed", 100)) if args.get("on", True) else 0
                elif tool == "set_led":
                    final["led"] = args.get("color")
        if final:
            outcomes[cmd["feeling"]].append(final)

    prefs = []
    for feeling, outs in outcomes.items():
        if len(outs) < MIN_FEELING_COUNT:
            continue
        actions = []
        fans = [o["fan"] for o in outs if "fan" in o]
        if len(fans) >= len(outs) / 2:
            on = [f for f in fans if f > 0]
            actions.append({"tool": "set_fan", "args": {"on": True, "speed": int(statistics.median(on))}}
                           if len(on) >= len(fans) / 2 else {"tool": "set_fan", "args": {"on": False}})
        leds = [o["led"] for o in outs if "led" in o]
        if len(leds) >= len(outs) / 2:
            actions.append({"tool": "set_led", "args": {"color": Counter(leds).most_common(1)[0][0]}})
        if actions:
            prefs.append({"feeling": feeling, "actions": actions, "count": len(outs)})
    return sorted(prefs, key=lambda p: -p["count"])


def merge_runtime(new: list, old: list) -> list:
    """Carry approvals / rejections / disabled / today's asks over from the old file."""
    old_by_id = {p["id"]: p for p in old}
    for p in new:
        prev = old_by_id.get(p["id"], {})
        for field, default in RUNTIME_FIELDS.items():
            p[field] = prev.get(field, default)
    return new


# --- Files -------------------------------------------------------------------

def load_patterns(path=None) -> list:
    """Habits from patterns.json. Entries from the old format are ignored."""
    path = Path(path or PATTERNS_JSON)
    if not path.exists():
        return []
    return [p for p in json.loads(path.read_text())["patterns"] if "valid_from" in p]


def load_preferences(path=None) -> list:
    """Feeling preferences from patterns.json ([] if none yet)."""
    path = Path(path or PATTERNS_JSON)
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("preferences", [])


def save_patterns(patterns: list, path=None, preferences: list = None):
    """Atomic write: a crash mid-write can't leave a half-written file.
    preferences=None keeps the ones already in the file."""
    path = Path(path or PATTERNS_JSON)
    if preferences is None:
        preferences = load_preferences(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"updated": datetime.now().isoformat(timespec="seconds"),
         "patterns": patterns, "preferences": preferences},
        indent=2,
    ))
    os.replace(tmp, path)


def update_pattern(pattern_id: str, **changes):
    """Change runtime fields of one habit (used by the agent)."""
    patterns = load_patterns()
    for p in patterns:
        if p["id"] == pattern_id:
            p.update(changes)
    save_patterns(patterns)


def load_profile() -> str:
    if PATTERNS_TXT.exists():
        return PATTERNS_TXT.read_text().strip()
    return "No learned habits yet."


# --- Acting ------------------------------------------------------------------

def is_muted(p: dict) -> bool:
    return p["disabled"] or (p["rejections"] - p["approvals"]) >= MUTE_AFTER


def can_auto(p: dict) -> bool:
    """Auto-execute only with high confidence, enough history AND past approvals."""
    return (not is_muted(p)
            and p["confidence"] >= AUTO_CONFIDENCE
            and p["count"] >= AUTO_MIN_COUNT
            and p["approvals"] >= AUTO_MIN_APPROVALS)


def _valid_at(p: dict, now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return (p["day_type"] == day_type_for(now.weekday()) and p["slot"] == slot_for(now.hour)
            and p["valid_from"] <= minute <= p["valid_to"])


def habit_now(patterns: list, now: datetime):
    """The habit that applies right now (even if already suggested today)."""
    return next((p for p in patterns if not p["disabled"] and _valid_at(p, now)), None)


def due_patterns(patterns: list, now: datetime, reask_min: int = 20, max_asks: int = 3) -> list:
    """Habits that should be suggested right now, strongest first.

    A habit is asked at most `max_asks` times a day, at least `reask_min`
    minutes apart, and not again once it was done or corrected that day.
    """
    today = now.date().isoformat()
    due = []
    for p in patterns:
        if not _valid_at(p, now) or is_muted(p) or p.get("done_date") == today:
            continue
        if p.get("ask_date") == today:
            if p.get("asks", 0) >= max_asks:
                continue
            last = datetime.fromisoformat(p["last_asked"])
            if now - last < timedelta(minutes=reask_min):
                continue
        due.append(p)
    return sorted(due, key=lambda p: -p["confidence"])


def record_ask(p: dict, now: datetime):
    """Note that `p` was just asked about."""
    today = now.date().isoformat()
    asks = p.get("asks", 0) + 1 if p.get("ask_date") == today else 1
    update_pattern(p["id"], ask_date=today, asks=asks,
                   last_asked=now.isoformat(timespec="seconds"))


def next_habit(patterns: list, now: datetime):
    """The next habit to start after `now` (today or tomorrow): (start, habit)."""
    best = None
    for days in (0, 1, 2):
        day = (now + timedelta(days=days)).date()
        midnight = datetime.combine(day, datetime.min.time())
        for p in patterns:
            if p["disabled"] or p["day_type"] != day_type_for(day.weekday()):
                continue
            start = midnight + timedelta(minutes=p["valid_from"])
            if start > now and (best is None or start < best[0]):
                best = (start, p)
        if best:
            return best
    return None


def describe_actions(actions: list) -> str:
    parts = []
    for a in actions:
        if a["tool"] == "set_led":
            c = a["args"]["color"]
            parts.append("light off" if c == "off" else f"light {c}")
        else:
            parts.append(f"fan {a['args']['speed']}%" if a["args"].get("on") else "fan off")
    return " and ".join(parts)


def describe(p: dict) -> str:
    return f"{describe_actions(p['actions'])} ({p['intent'].replace('_', ' ')})"


def now_context(patterns: list, now: datetime, state: dict) -> str:
    """What Qwen needs to know about this moment, sent with every message."""
    led = "off" if state["led"] == "off" else state["led"]
    fan = f"{state['fan_speed']}%" if state["fan_on"] else "off"
    lines = [
        f"Current time: {now:%A %Y-%m-%d %H:%M} "
        f"({day_type_for(now.weekday())}, {slot_for(now.hour)} slot).",
        f"Room now: fan {fan}, light {led}, "
        f"{'someone is home' if state.get('present') else 'nobody detected'}.",
    ]
    cur = habit_now(patterns, now)
    lines.append(f"User's habit for right now: {describe(cur)}." if cur
                 else "User's habit for right now: none (do not apply any habit).")
    nxt = next_habit(patterns, now)
    # Only mention the next habit if it is close, so a habit hours away isn't
    # mistaken for "now" by the small model.
    if nxt and nxt[0] - now <= timedelta(minutes=NEXT_HABIT_MIN):
        start, p = nxt
        lines.append(f"Next habit: from {start:%A %H:%M} (usually around {p['typical_time']}): "
                     f"{describe(p)}.")
    prefs = load_preferences()
    if prefs:
        lines.append("What the user chose before when feeling: " + "; ".join(
            f"{x['feeling']} -> {describe_actions(x['actions'])} ({x['count']} times)" for x in prefs) + ".")
    else:
        lines.append("What the user chose before when feeling: nothing recorded yet.")
    return "\n".join(lines)


def profile_text(patterns: list, prefs: list) -> str:
    """Accurate plain-English profile for patterns.txt, built from the data
    (no LLM, so it can't contain made-up facts)."""
    out = []
    for day_type, title in (("weekday", "Weekdays"), ("weekend", "Weekends")):
        rows = [p for p in patterns if p["day_type"] == day_type]
        if rows:
            out.append(f"{title}:")
            for p in rows:
                why = f"{p['intent'].replace('_', ' ')}, " if p["intent"] != "habit" else ""
                out.append(f"- {p['window']} (usually ~{p['typical_time']}): "
                           f"{describe_actions(p['actions'])} [{why}{p['confidence']:.0%} of {day_type}s]")
    if prefs:
        out.append("When the user mentions a feeling, they usually choose:")
        out += [f"- {x['feeling']}: {describe_actions(x['actions'])} ({x['count']} times)" for x in prefs]
    return "\n".join(out) if out else "No learned habits yet."


def pattern_lines(patterns: list) -> str:
    """Plain summary of the habits (fallback profile, and Qwen's input)."""
    return "\n".join(
        f"- {p['day_type']} {p['slot']} ({p['window']}, usually ~{p['typical_time']}): "
        f"{describe(p)}, on {p['confidence']:.0%} of {p['day_type']}s"
        for p in patterns
    )
