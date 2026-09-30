"""
Phase 7: turn the user's labeled commands into habit patterns.

A pattern = (weekday/weekend, time slot, intent), e.g. "weekday night ->
sleep_prep". For each one we store how reliable it is, the usual time, and
the usual actions, so the agent's checker can ask (or act) at the right moment.

Files (project root):
  patterns.json  numbers for code: confidence, usual time, actions, and the
                 runtime fields the agent updates (approvals, rejections,
                 disabled, last_fired).
  patterns.txt   plain-English profile written by Qwen, loaded into the prompt.

Only source='user' commands count, so PIR defaults and auto-actions can never
teach the system a "habit" the user didn't choose.
"""

import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from system.behaviour_log import _conn, day_type_for, slot_for

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_JSON = ROOT / "patterns.json"
PATTERNS_TXT = ROOT / "patterns.txt"

# Learning
RECENT_DAYS = 21          # the last 3 weeks...
RECENT_WEIGHT = 2.0       # ...count double
MIN_COUNT = 3             # floor: seen at least this many times
MIN_CONFIDENCE = 0.6      # floor: happens on at least 60% of matching days
MAX_PER_SLOT = 3          # keep the prompt small so Qwen doesn't get confused

# Acting
AUTO_CONFIDENCE = 0.9     # auto-execute only if all three hold
AUTO_MIN_COUNT = 5
AUTO_MIN_APPROVALS = 3
MUTE_AFTER = 3            # stop asking once rejections - approvals reaches this
LEAD_MIN = 5              # ask up to 5 min before the usual time
WINDOW_MIN = 60           # ...and until 60 min after it

RUNTIME_FIELDS = {"approvals": 0, "rejections": 0, "disabled": False, "last_fired": None}


# --- Learning ----------------------------------------------------------------

def _load_user_commands():
    """Labeled user commands, plus the actions each one caused."""
    with _conn() as c:
        cmds = c.execute(
            """
            SELECT e.id, e.ts, e.day_type, e.slot, i.name AS intent
            FROM events e JOIN intents i ON i.id = e.intent_id
            WHERE e.kind = 'command' AND e.source = 'user'
            """
        ).fetchall()
        acts = c.execute(
            "SELECT parent_id, tool, args FROM events"
            " WHERE kind = 'action' AND parent_id IS NOT NULL"
        ).fetchall()
    by_parent = defaultdict(list)
    for a in acts:
        by_parent[a["parent_id"]].append({"tool": a["tool"], "args": json.loads(a["args"] or "{}")})
    return [dict(r) for r in cmds], by_parent


def _typical_actions(occurrences: list) -> list:
    """The actions the user usually takes for one pattern.

    A device is included only if it was changed in at least half of the
    occurrences. LED -> most common colour. Fan -> off, or on at the median speed.
    """
    n = len(occurrences)
    per_tool = defaultdict(list)
    for acts in occurrences:
        for tool, args in {a["tool"]: a["args"] for a in acts}.items():
            per_tool[tool].append(args)

    result = []
    for tool, arglist in sorted(per_tool.items()):
        if len(arglist) < n / 2:
            continue
        if tool == "set_led":
            color = Counter(a.get("color") for a in arglist).most_common(1)[0][0]
            result.append({"tool": "set_led", "args": {"color": color}})
        elif tool == "set_fan":
            on = [a for a in arglist if a.get("on", True) and a.get("speed", 100) > 0]
            if len(on) >= len(arglist) / 2:
                speed = int(statistics.median(a.get("speed", 100) for a in on))
                result.append({"tool": "set_fan", "args": {"on": True, "speed": speed}})
            else:
                result.append({"tool": "set_fan", "args": {"on": False}})
    return result


def compute_patterns() -> list:
    """Build the pattern list from events.db (does not touch runtime fields)."""
    cmds, by_parent = _load_user_commands()
    if not cmds:
        return []

    for r in cmds:
        r["dt"] = datetime.fromisoformat(r["ts"])
    first = min(r["dt"] for r in cmds).date()
    ref = max(r["dt"] for r in cmds).date()

    def weight(day):
        return RECENT_WEIGHT if (ref - day).days < RECENT_DAYS else 1.0

    # How many (weighted) weekdays / weekend days the data covers.
    covered = defaultdict(float)
    day = first
    while day <= ref:
        covered[day_type_for(day.weekday())] += weight(day)
        day += timedelta(days=1)

    groups = defaultdict(list)
    for r in cmds:
        groups[(r["day_type"], r["slot"], r["intent"])].append(r)

    found = []
    for (day_type, slot, intent), rows in groups.items():
        if len(rows) < MIN_COUNT:
            continue
        days = {r["dt"].date() for r in rows}
        confidence = min(1.0, sum(weight(d) for d in days) / covered[day_type])
        if confidence < MIN_CONFIDENCE:
            continue
        actions = _typical_actions([by_parent[r["id"]] for r in rows])
        if not actions:
            continue
        minute = int(statistics.median(r["dt"].hour * 60 + r["dt"].minute for r in rows))
        found.append({
            "id": f"{day_type}:{slot}:{intent}",
            "day_type": day_type,
            "slot": slot,
            "intent": intent,
            "typical_minute": minute,
            "typical_time": f"{minute // 60:02d}:{minute % 60:02d}",
            "actions": actions,
            "confidence": round(confidence, 2),
            "count": len(rows),
            "days_seen": len(days),
            "last_seen": max(r["dt"] for r in rows).date().isoformat(),
        })

    # At most MAX_PER_SLOT patterns per (day type, slot), strongest first.
    by_slot = defaultdict(list)
    for p in found:
        by_slot[(p["day_type"], p["slot"])].append(p)
    kept = []
    for lst in by_slot.values():
        kept += sorted(lst, key=lambda p: -p["confidence"])[:MAX_PER_SLOT]
    return sorted(kept, key=lambda p: (p["day_type"], p["typical_minute"]))


def merge_runtime(new: list, old: list) -> list:
    """Carry approvals / rejections / disabled / last_fired over from the old file."""
    old_by_id = {p["id"]: p for p in old}
    for p in new:
        prev = old_by_id.get(p["id"], {})
        for field, default in RUNTIME_FIELDS.items():
            p[field] = prev.get(field, default)
    return new


# --- Files -------------------------------------------------------------------

def load_patterns(path=None) -> list:
    path = Path(path or PATTERNS_JSON)
    if not path.exists():
        return []
    return json.loads(path.read_text())["patterns"]


def save_patterns(patterns: list, path=None):
    """Atomic write: a crash mid-write can't leave a half-written file."""
    path = Path(path or PATTERNS_JSON)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"updated": datetime.now().isoformat(timespec="seconds"), "patterns": patterns},
        indent=2,
    ))
    os.replace(tmp, path)


def update_pattern(pattern_id: str, **changes):
    """Change runtime fields of one pattern (used by the agent)."""
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


def due_patterns(patterns: list, now: datetime) -> list:
    """Patterns that should fire right now, strongest first."""
    day_type, slot = day_type_for(now.weekday()), slot_for(now.hour)
    minute, today = now.hour * 60 + now.minute, now.date().isoformat()
    due = [
        p for p in patterns
        if p["day_type"] == day_type and p["slot"] == slot
        and not is_muted(p) and p["last_fired"] != today
        and p["typical_minute"] - LEAD_MIN <= minute <= p["typical_minute"] + WINDOW_MIN
    ]
    return sorted(due, key=lambda p: -p["confidence"])


def describe_actions(actions: list) -> str:
    parts = []
    for a in actions:
        if a["tool"] == "set_led":
            c = a["args"]["color"]
            parts.append("light off" if c == "off" else f"light {c}")
        else:
            parts.append(f"fan {a['args']['speed']}%" if a["args"].get("on") else "fan off")
    return " and ".join(parts)


def pattern_lines(patterns: list) -> str:
    """Plain summary of the patterns (fallback profile, and Qwen's input)."""
    return "\n".join(
        f"- {p['day_type']} {p['slot']} around {p['typical_time']}: {p['intent']} "
        f"-> {describe_actions(p['actions'])} "
        f"(on {p['confidence']:.0%} of {p['day_type']}s, {p['count']} times)"
        for p in patterns
    )
